#!/usr/bin/python3
"""Scenes without a screen: the same scene boards (scene.py) the overlay draws, hosted for another surface.

The Teach app shows chalk's scenes in its workspace (a stage the user drags sliders and presses Next on) without a
second renderer: this process runs scene.Scene exactly as the overlay does and streams its frames, and the
app sends back their pointer. One process serves many scenes. JSON lines on stdin:

  {"op": "open", "id": "s1", "spec": {...scene...}, "gates": [2]}   gates: steps Next can't enter until
                                                                    the host app calls "release" (predict-then-
                                                                    reveal: the user commits a prediction first)
  {"op": "replace", "id": "s1", "spec": {...}}        the tutor changed the scene (scene.Scene.replace)
  {"op": "pointer", "id": "s1", "t": "down|move|up|leave", "x": 120, "y": 80}   layout px on the card
  {"op": "key", "id": "s1", "name": "Right", "char": ""}
  {"op": "act", "id": "s1", "action": "next" | "back" | ["toggle", "on"]}
  {"op": "release", "id": "s1", "step": 2}            lift a gate (and go on to that step)
  {"op": "refresh", "id": "s1"}                       send the current frame again (a page just opened)
  {"op": "close", "id": "s1"}

and JSON lines on stdout:

  {"t": "frame", "id": "s1", "seq": 7, "w": 636, "h": 584, "scale": 2, "png": "<base64>", "ack": 12}
      w, h in layout px; the PNG is scale x that. ack = the last input seq the frame includes.
  {"t": "state", "id": "s1", "step": 1, "steps": 3, "values": {"f": 5000.0}, "caption": "..."}
  {"t": "gate", "id": "s1", "step": 2}                the user pressed Next into a gated step
  {"t": "closed", "id": "s1", "result": {...}}       the user closed the card
  {"t": "error", "id": "s1", "error": "..."}

Frames go out only when something changed (their input, a step, motion that depends on time) and at most FPS a
second; a frame being encoded when new input lands is followed by one more, so a drag never ends stale.
PNG at compress level 1 through PIL: ~20 ms a frame at a 1920x1280 screen's size, lossless, so TeX stays
crisp (cairo's own write_to_png took 34 ms).
"""
import base64, io, json, os, sys, threading, time, traceback

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import chalkconf  # noqa: E402
chalkconf.load()
_GI = os.path.expanduser(os.environ.get("CHALK_GI_SITE", ""))   # optional: a gi with cairo support, put first on sys.path
if _GI and os.path.isdir(_GI):
    sys.path.insert(0, _GI)

import scene  # noqa: E402
from PIL import Image  # noqa: E402

FPS = 30
lock = threading.Lock()
out_lock = threading.Lock()
wake = threading.Event()


def emit(m):
    with out_lock:
        sys.stdout.write(json.dumps(m, separators=(",", ":")) + "\n")
        sys.stdout.flush()


class Hosted:
    def __init__(self, sid, spec, gates):
        self.id = sid
        self.sc = scene.Scene(spec)
        self.gates = set(int(g) for g in gates or [])
        self.dirty = True
        self.seq = 0
        self.ack = 0
        self.dragging = None
        self.last_state = None
        self.last_frame = 0.0
        self.closed = False

    # the overlay's pointer handling (overlay.py on_press / on_motion / on_release), for one card
    def pointer(self, t, x, y):
        sc = self.sc
        if t == "move":
            if self.dragging:
                self.apply(sc.drag_to(self.dragging, x, y))
            else:
                a = sc.hit(x, y)
                if sc.hover != a:
                    sc.hover = a
                    self.dirty = True
        elif t == "leave":
            if sc.hover is not None:
                sc.hover = None
                self.dirty = True
        elif t == "down":
            a = sc.hit(x, y)
            if a:
                self.act(a, x, y)
        elif t == "up":
            if self.dragging:
                self.dragging = None
                if hasattr(sc, "end_drag"):
                    sc.end_drag()
                self.dirty = True

    def act(self, action, x=None, y=None):
        sc = self.sc
        if action == "next" and sc.step + 1 in self.gates:
            emit({"t": "gate", "id": self.id, "step": sc.step + 1})
            return
        effects = sc.act(action)
        if "drag" in effects:
            self.dragging = action
            effects = [e for e in effects if e != "drag"] + (sc.drag_to(action, x, y) if x is not None else [])
        self.apply(effects)

    def apply(self, effects):
        self.dirty = True
        if "close" in effects:
            self.closed = True
            emit({"t": "closed", "id": self.id, "result": getattr(self.sc, "result", None)})

    def state(self):
        sc = self.sc
        steps = getattr(sc, "steps", []) or []
        cap = steps[sc.step].get("say") if steps and 0 <= sc.step < len(steps) else None
        st = {"step": sc.step, "steps": len(steps), "values": {k: round(float(v), 6) for k, v in sc.values.items()}, "caption": cap}
        if st != self.last_state:
            self.last_state = st
            emit({"t": "state", "id": self.id, **st})

    def grab(self):
        """The card's pixels now (under the lock: ~2 ms). encode() turns them into a frame without it, so their
        next pointer events go in while the last frame is still being compressed."""
        surf, w, h, _ = self.sc.snapshot()
        surf.flush()
        self.seq += 1
        return (self.id, self.seq, self.ack, w, h, surf.get_width(), surf.get_height(), surf.get_stride(), bytes(surf.get_data()))

    @staticmethod
    def encode(grabbed):
        sid, seq, ack, w, h, pw, ph, stride, data = grabbed
        buf = io.BytesIO()
        Image.frombuffer("RGBA", (pw, ph), data, "raw", "BGRA", stride, 1).save(buf, "PNG", compress_level=1)
        emit({"t": "frame", "id": sid, "seq": seq, "w": round(w, 2), "h": round(h, 2), "scale": scene.panel.SCALE,
              "png": base64.b64encode(buf.getvalue()).decode(), "ack": ack})


scenes = {}


def handle(m):
    op, sid = m.get("op"), m.get("id")
    if op == "open":
        scenes[sid] = Hosted(sid, m["spec"].get("scene", m["spec"]), m.get("gates"))
        return
    h = scenes.get(sid)
    if not h:
        return emit({"t": "error", "id": sid, "error": "no such scene"})
    h.ack = max(h.ack, int(m.get("seq") or 0))
    if op == "close":
        del scenes[sid]
    elif op == "refresh":   # a page that just opened wants the current frame and state
        h.dirty = True
        h.last_state = None
    elif op == "replace":
        h.sc.replace(m["spec"].get("scene", m["spec"]))
        h.dirty = True
    elif op == "pointer":
        h.pointer(m.get("t"), float(m.get("x", 0)), float(m.get("y", 0)))
        h.dirty = True   # (the ack moves even when nothing on the card changed)
    elif op == "key" and hasattr(h.sc, "key"):
        h.apply(h.sc.key(m.get("name"), m.get("char") or ""))
    elif op == "act":
        a = m.get("action")
        h.act(tuple(a) if isinstance(a, list) else a)
    elif op == "release":
        step = int(m.get("step", -1))
        h.gates.discard(step)
        if h.sc.step + 1 == step:
            h.act("next")


def reader():
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        with lock:
            try:
                handle(m)
            except Exception as e:  # one bad message never takes the other scenes down
                emit({"t": "error", "id": m.get("id"), "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-800:]})
        wake.set()
    os._exit(0)   # the app went away


def painter():
    while True:
        wake.wait(1 / FPS)
        wake.clear()
        now = scene.clock()
        grabbed = []
        with lock:
            due = [h for h in scenes.values() if not h.closed and (h.dirty or h.sc.animating(now))]
            for h in due:
                if time.monotonic() - h.last_frame < 1 / FPS:
                    wake.set()   # come back for it when its frame time is up
                    continue
                h.dirty = False
                h.last_frame = time.monotonic()
                try:
                    grabbed.append(h.grab())
                    h.state()
                except Exception as e:
                    emit({"t": "error", "id": h.id, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-800:]})
        for g in grabbed:
            Hosted.encode(g)


if __name__ == "__main__":
    threading.Thread(target=reader, daemon=True).start()
    painter()
