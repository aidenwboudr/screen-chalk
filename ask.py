"""chalk ask: the HeyClicky-style flow. A question (typed into a rofi prompt from a hotkey, or
passed as arguments) -> screenshot -> Claude -> marks drawn on screen one by one as they stream
in, the spoken answer as a card beside the main mark, and for how/why questions a diagram
sketched on a chalkboard.

Felt latency is what matters here (Enter -> first stroke), so claude starts before the question
is typed, shapes are drawn as soon as each one is complete in the streamed JSON, and the diagram
comes from a second call running in parallel: its long JSON would otherwise hold the marks back
(the stream can arrive in late bursts), 3.7 s -> 6.5 s to the first stroke.
"""
import base64, json, os, re, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import chalkconf  # noqa: E402
chalkconf.load()

SCHEMA = {
    "type": "object",
    "properties": {  # shapes first: they stream out, and get drawn, before the spoken sentence
        "shapes": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["circle", "box", "underline", "highlight", "strike", "arrow",
                                                "point", "label", "path", "spotlight"]},
            "box": {"type": "array", "items": {"type": "number"}},
            "at": {"type": "array", "items": {"type": "number"}},
            "from": {"type": "array", "items": {"type": "number"}},
            "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
            "label": {"type": "string", "description": "2-5 handwritten words shown next to the mark"},
            "color": {"type": "string"},
        }, "required": ["kind"]}},
        "say": {"type": "string"},
        "remove": {"type": "array", "items": {"type": "string"},
                   "description": "follow-ups: tags of earlier marks or boards to erase"},
    },
    "required": ["shapes", "say"],
}

# the diagram is a scene board (scene.py, scene.md): it moves, and the user can drag its parameters
SCENE_SCHEMA = {
    "type": "object",
    "properties": {
        "draw": {"type": "boolean"},
        "box": {"type": "array", "items": {"type": "number"}},
        "at": {"type": "array", "items": {"type": "number"}},
        "scene": {"type": "object"},
    },
    "required": ["draw"],
}
# the board opens as soon as the scene's objects begin and each one draws itself in as it arrives; the steps come
# last, and the board then stands on the last step (writing steps first made worse scenes). CHALK_SCENE_STREAM=0: off
STREAM = os.environ.get("CHALK_SCENE_STREAM", "1") != "0"


def close_json(text):
    """Parse the start of a JSON object by closing what's open: '{"a": 1, "b": {"c": [2' -> {...}. None if it can't."""
    text = text.rstrip().rstrip(",")
    stack, in_str, esc = [], False, False
    for c in text:
        if in_str:
            esc = (c == "\\") and not esc
            if c == '"' and not esc:
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            stack.append("}" if c == "{" else "]")
        elif c in "}]" and stack:
            stack.pop()
    try:
        return json.loads(text + "".join(reversed(stack)))
    except ValueError:
        return None
CHECK_SCHEMA = {"type": "object", "properties": {
    "issues": {"type": "array", "items": {"type": "string"}}, "fixed": {"type": "boolean"}, "scene": {"type": "object"}},
    "required": ["issues", "fixed"]}


def check_scene(env, model, effort, question, shot, sk, state):
    """A second look (on unless CHALK_SCENE_CHECK=0) at a scene board, through its storyboard (scene.storyboard), that fixes
    wrong values, overlaps and steps that don't show what they say. Returns (fixed sk or None, issues)."""
    import scene as scene_mod  # cairo + Pango: only needed here
    out = os.path.join(state, "check")
    strip = scene_mod.storyboard({"scene": sk["scene"]}, out) and os.path.join(out, "strip.png")
    proc = spawn_claude(env, model, effort, CHECK_SCHEMA)
    spec = open(os.path.join(HERE, "scene.md")).read().split("Reply JSON:")[1]
    text = (open(os.path.join(HERE, "scene-check.md")).read().strip() + f'\n\nThe user asked: "{question}"\n\n'
            f"The scene:\n{json.dumps(sk['scene'])}\n\nThe scene format:{spec}")
    imgs = [shot, strip]
    proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": [
        *({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                       "data": base64.b64encode(open(p, "rb").read()).decode()}} for p in imgs),
        {"type": "text", "text": text}]}}) + "\n")
    proc.stdin.close()
    res = None
    for kind, val in stream_answer(proc):
        if kind == "done":
            res = val
    if res and res.get("fixed") and isinstance(res.get("scene"), dict) and res["scene"].get("objects"):
        return dict(sk, scene=res["scene"]), res.get("issues") or []
    return None, (res or {}).get("issues") or []


def claude_env(base):
    """The env for `claude -p --bare`, which authenticates only with ANTHROPIC_API_KEY (or an apiKeyHelper): the
    caller's own. CHALK_CLAUDE_ENV_FROM_SETTINGS=1 also takes the ANTHROPIC_* values from ~/.claude/settings.json's
    "env" (for a machine whose Claude Code is configured there)."""
    env = dict(base)
    if os.environ.get("CHALK_CLAUDE_ENV_FROM_SETTINGS") == "1":
        try:
            env.update({k: v for k, v in json.load(open(os.path.expanduser("~/.claude/settings.json"))).get("env", {}).items()
                        if k.startswith("ANTHROPIC_")})
        except (OSError, ValueError):
            pass
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")
    # the answer comes through a structured-output tool call, whose input the API otherwise holds back and sends
    # one top-level key at a time: the scene arrived in one burst at the end (bench/visual2/research/streaming.md)
    env.setdefault("CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING", "1")
    return env


def spawn_claude(env, model, effort=None, schema=SCHEMA):
    """Start claude before the question exists: its ~1 s startup overlaps typing and the screenshot."""
    cmd = [*(chalkconf.words("CHALK_CLAUDE") or ["claude"]), "-p", "--bare", "--no-session-persistence", "--model", model, "--tools", "",
           "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--json-schema", json.dumps(schema)]
    if effort:
        cmd += ["--effort", effort]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=claude_env(env))


class ShapeStream:
    """Pulls each finished object out of the "shapes" array (or another `key`) while the JSON is still streaming;
    `header` is the text before that array once it has begun (a scene's title, params and steps)."""

    def __init__(self, key="shapes"):
        self.key, self.header = key, None
        self.buf, self.pos, self.depth, self.obj = "", None, 0, None
        self.in_str = self.esc = self.done = False

    def feed(self, chunk):
        self.buf += chunk
        if self.done:
            return []
        if self.pos is None:
            i = self.buf.find(f'"{self.key}"')
            j = self.buf.find("[", i) if i >= 0 else -1
            if j < 0:
                return []
            self.pos = j + 1
            self.header = self.buf[:i]
        out = []
        while self.pos < len(self.buf):
            c = self.buf[self.pos]
            self.pos += 1
            if self.in_str:
                if self.esc:
                    self.esc = False
                elif c == "\\":
                    self.esc = True
                elif c == '"':
                    self.in_str = False
            elif c == '"':
                self.in_str = True
            elif c in "{[":
                if self.depth == 0 and c == "{":
                    self.obj = self.pos - 1
                self.depth += 1
            elif c in "}]":
                if self.depth == 0:  # the shapes array closed
                    self.done = True
                    break
                self.depth -= 1
                if self.depth == 0 and c == "}":
                    try:
                        out.append(json.loads(self.buf[self.obj:self.pos]))
                    except ValueError:
                        pass
        return out


def message(question, shot, size, policy_file, ending, context=""):
    policy = open(os.path.join(HERE, policy_file)).read().strip()
    w, h = size
    text = (f"{policy}\n\nThe screenshot is {w}x{h} pixels, origin top-left; give every coordinate in this "
            f"image's pixels.\n\n{context + chr(10) + chr(10) if context else ''}"
            f"The user asks: \"{question}\"\n{ending}")
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.b64encode(open(shot, "rb").read()).decode()}},
        {"type": "text", "text": text}]}}


def send_message(proc, msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.close()


SPEND = []  # API-equivalent cost of each claude call in this ask (logged; the visual bench sums it per arm)


def stream_answer(proc, key="shapes"):
    """Yield ("shape", s) as each shape (item of `key`) completes in the stream, then ("done", answer)."""
    ss = ShapeStream(key)
    for line in proc.stdout:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "stream_event":
            d = e["event"].get("delta") or {}
            if d.get("type") == "input_json_delta":
                had = ss.header is not None
                shapes = ss.feed(d.get("partial_json", ""))
                if not had and ss.header is not None:
                    yield "header", ss.header
                for shape in shapes:
                    yield "shape", shape
        elif e.get("type") == "result":
            SPEND.append(float(e.get("total_cost_usd") or 0))
            if e.get("structured_output"):
                yield "done", e["structured_output"]
                return
            raise RuntimeError(e.get("result") or "no answer")
    raise RuntimeError((proc.stderr.read() or "claude gave no result").strip()[-300:])


def prompt_question(env):
    r = subprocess.run(["rofi", "-dmenu", "-p", "ask chalk", "-lines", "0", "-theme-str",
                        "window {width: 40%;} listview {enabled: false;}"],
                       env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return r.stdout.strip()


def short(say, sentences=3):
    """The note card is read, not heard: keep the first few sentences."""
    parts = re.split(r"(?<=[.!?])\s+", say.strip())
    return " ".join(parts[:sentences])


TTL = 0  # answers stay up until Super+Shift+/ (`chalk clear`): follow-up questions build on them
IDLE_RESET = 30 * 60  # ...or until a new question comes after this long: then it starts over


class Convo:
    """The conversation the hotkey is in: what was asked and answered, and the tagged marks and
    boards still on screen, so a follow-up can build on them or erase some.
    Lives in STATE/convo.json; `chalk clear` deletes it."""

    def __init__(self, state, send):
        self.path = os.path.join(state, "convo.json")
        try:
            d = json.load(open(self.path))
        except (OSError, ValueError):
            d = None
        if d and time.time() - d.get("t", 0) > IDLE_RESET:
            send({"op": "clear"}, start=False)  # an old conversation: start over on a clean screen
            d = None
        self.d = d or {"turns": [], "marks": {}, "boards": {}, "n": 0}

    @property
    def active(self):
        return bool(self.d["turns"])

    def tag(self, prefix):
        self.d["n"] += 1
        return f"{prefix}{self.d['n']}"

    def add_mark(self, tag, shape):
        self.d["marks"][tag] = {k: shape[k] for k in ("kind", "box", "at", "label") if k in shape}

    def add_board(self, tag, sk):
        sc = sk.get("scene") or {}
        self.d["boards"][tag] = {"title": sc.get("title"), "box": sk.get("box"),
                                 "steps": [st.get("say") for st in sc.get("steps") or [] if isinstance(st, dict)]}

    def remove(self, tags):
        for t in tags:
            self.d["marks"].pop(t, None)
            self.d["boards"].pop(t, None)

    def end_turn(self, question, say):
        self.d["turns"].append({"q": question, "say": short(say)})
        self.d["t"] = time.time()
        json.dump(self.d, open(self.path, "w"))

    def context(self, for_sketch=False):
        if not self.active:
            return ""
        out = ["This is a follow-up question. Earlier in this conversation:"]
        for i, t in enumerate(self.d["turns"][-6:], 1):
            out.append(f'{i}. The user asked "{t["q"]}" and you answered: "{t["say"]}"')
        if self.d["marks"] or self.d["boards"]:
            out.append("Still drawn on the screen from earlier (the screenshot shows the screen without them):")
        for tag, m in self.d["marks"].items():
            where = m.get("box") or m.get("at")
            out.append(f"- {tag}: {m.get('kind', 'mark')} at {where}" + (f' labelled "{m["label"]}"' if m.get("label") else ""))
        for tag, b in self.d["boards"].items():
            out.append(f'- {tag}: board "{b.get("title") or ""}" with steps {json.dumps(b.get("steps") or [])}')
        if for_sketch:
            out.append('Build on what is there: draw a new board only for what the earlier one doesn\'t show, or set '
                       '"draw": false if the existing drawing already answers it.')
        else:
            out.append('Build on what is there: add only what this question needs, don\'t redraw marks that are '
                       'already there, and list in "remove" the tags of earlier marks or boards that would now '
                       'mislead or clutter the screen.')
        return "\n".join(out)


def as_mark(shape):
    """A streamed shape as the overlay should get it (None for ones the hotkey doesn't draw)."""
    if shape.get("kind") == "card":
        return None
    shape = dict(shape, ttl=shape.get("ttl", TTL))
    if "text" in shape and shape.get("kind") != "label":  # meant as words to write, not text to find
        shape.setdefault("label", shape.pop("text"))
    return shape


def note_card(ans, size):
    """The spoken answer, as a note beside the main mark."""
    say = (ans.get("say") or "").strip()
    if not say:
        return None
    shapes = ans.get("shapes", [])
    main = next((s for s in shapes if s.get("box") or s.get("at")), None)
    anchor = (main.get("box") or [*main["at"], 2, 2]) if main else [size[0] / 2 - 1, 60, 2, 2]
    return {"kind": "card", "text": short(say), "box": anchor, "color": "blue", "ttl": TTL}


def sketch_shape(sk):
    """The diagram call's answer as a scene board panel (None: no board)."""
    if sk and sk.get("draw") and isinstance(sk.get("scene"), dict) and sk["scene"].get("objects"):
        spec = {k: sk[k] for k in ("box", "at") if k in sk}
        return dict(spec, kind="panel", scene=sk["scene"], ttl=TTL)
    return None


class Pen:
    """Draws one answer's pieces in order; with CHALK_SEED every piece gets a fixed seed, so a
    replay of the same answer draws the same wobbles (the visual bench compares renderers that way)."""

    def __init__(self, draw, ground, keep_first=False):
        self.draw, self.ground, self.n = draw, ground, 0
        base = os.environ.get("CHALK_SEED")
        self.seed = int(base) if base and base.isdigit() else None
        self.times = []
        self.keep_first = keep_first  # a follow-up: draw on top of what's there instead of replacing it
        self.lock = threading.Lock()  # a streaming scene board draws from its own thread

    def put(self, shape, keep=None, tag=None):
        if shape is None:
            return False
        with self.lock:
            return self._put(shape, keep, tag)

    def _put(self, shape, keep, tag):
        if tag:
            shape = dict(shape, tag=tag)
        seed = None if self.seed is None else self.seed + self.n
        self.n += 1
        if keep is None:
            keep = bool(self.times) or self.keep_first
        try:
            self.draw([shape], keep=keep, seed=seed)
        except self.ground.GroundError:
            return False
        self.times.append(time.monotonic())
        return True


class Turn:
    """Draws one answer into the conversation: tagged marks, the note card, the board, erasures."""

    def __init__(self, pen, convo, send):
        self.pen, self.convo, self.send = pen, convo, send

    def mark(self, shape):
        m = as_mark(shape)
        if m is None:
            return
        tag = self.convo.tag("m")
        if self.pen.put(m, tag=tag):
            self.convo.add_mark(tag, m)
            if self.convo.active and len(self.pen.times) == 1:
                self.send({"op": "remove", "tags": ["busy"]}, start=False)

    def finish(self, ans, size):
        if ans.get("remove"):
            self.send({"op": "remove", "tags": list(ans["remove"])}, start=False)
            self.convo.remove(ans["remove"])
        if not self.pen.times and self.convo.active:
            self.send({"op": "remove", "tags": ["busy"]}, start=False)
        if not getattr(self, "scene_tag", None):
            self.pen.put(note_card(ans, size), keep=bool(self.pen.times) or self.convo.active, tag="note")

    def sketch(self, sk, say=""):
        shape = sketch_shape(sk)
        if shape is None:
            return False
        tag = self.convo.tag("b")
        ok = self.pen.put(shape, keep=True, tag=tag)
        if ok:
            self.convo.add_board(tag, sk)
            # the board's step captions say it: no note beside it (the bench: less text scored better)
            self.send({"op": "remove", "tags": ["note"]}, start=False)
        return ok


    def stream_open(self, header):
        """The scene's title, params and steps are written: open the board (objects follow)."""
        sc = dict(header.get("scene") or {}, objects=[], streaming=True)
        shape = dict({k: header[k] for k in ("box", "at") if k in header}, kind="panel", scene=sc, ttl=TTL)
        tag = self.convo.tag("b")
        if not self.pen.put(shape, keep=True, tag=tag):
            return None
        self.convo.add_board(tag, header)
        self.scene_tag = tag
        self.send({"op": "remove", "tags": ["note", "busy"]}, start=False)   # the board's captions say it
        return tag

    def stream_add(self, obj):
        self.pen.put({"kind": "scene_add", "objects": [obj]}, keep=True, tag=self.scene_tag)

    def resketch(self, sk, say=""):
        """Swap a corrected scene into the board this turn drew (the newest one), where it stands: same step,
        the user's slider values kept, only the changed parts draw in again."""
        tag = list(self.convo.d["boards"])[-1] if self.convo.d["boards"] else None
        if not tag:
            return self.sketch(sk, say)
        self.convo.add_board(tag, sk)
        return self.pen.put({"kind": "scene_replace", "scene": sk["scene"]}, keep=True, tag=tag)


def replay(paths, look, draw, ground, send, state):
    """`chalk ask --replay LOG.json [LOG2.json ...]`: draw saved answers again, in order, exactly as
    the hotkey did (follow-ups included)."""
    for path in paths:
        log = json.load(open(path))
        lk = look(ocr=False)  # fresh occupancy map for whatever is on screen now
        convo = Convo(state, send)
        turn = Turn(Pen(draw, ground, keep_first=convo.active), convo, send)
        ans = log.get("answer") or {}
        for s in ans.get("shapes", []):
            turn.mark(s)
        turn.finish(ans, lk["size"])
        turn.sketch(log.get("sketch"), ans.get("say") or "")
        convo.end_turn(log.get("q", ""), ans.get("say") or "")


def ask_tutor(route, args, env):
    """A Teach lesson is running on this screen (`chalk route teach`): the question goes to its tutor, who sees the
    screen, draws and talks itself, instead of to chalk's own quick answer."""
    question = " ".join(a for a in args if not a.startswith("--"))
    if not question:
        r = subprocess.run(["rofi", "-dmenu", "-p", "ask your tutor", "-lines", "0", "-mesg", route.get("title", ""),
                            "-theme-str", "window {width: 40%;} listview {enabled: false;}"],
                           env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        question = r.stdout.strip()
    if question:
        r = subprocess.run([*(chalkconf.words("CHALK_TEACH") or ["teach"]), "tell", route["slug"], question], env=env,
                           capture_output=True, text=True)
        if r.returncode:
            subprocess.run(["notify-send", "Teach didn't get that", (r.stderr or r.stdout).strip()[-200:]], env=env)


def run(args, env, send, look, draw, ground, state, die, route=None):
    """`chalk ask [question] [--json]`. The chalk CLI passes in its session env and helpers."""
    if route and route.get("to") == "teach" and "--chalk" not in args:
        return ask_tutor(route, args, env)
    if route and route.get("to") != "teach" and "--chalk" not in args and chalkconf.hook("route"):
        if chalkconf.hook("route")(route, args, env, send):
            return
    # Opus at low effort: 91% on the hard bench at a 3.3 s median, vs Sonnet's 84% at 3.1 s
    model = os.environ.get("CHALK_MODEL", "opus")
    effort = os.environ.get("CHALK_EFFORT", "low") or None
    want_sketch = os.environ.get("CHALK_SKETCH", "1") != "0"
    proc = spawn_claude(env, model, effort)
    # the diagram runs in parallel with the marks, so it can afford more thinking than they can
    sketch_effort = os.environ.get("CHALK_SKETCH_EFFORT", effort) or None
    sketcher = spawn_claude(env, model, sketch_effort, SCENE_SCHEMA) if want_sketch else None
    question = " ".join(a for a in args if not a.startswith("--")) or prompt_question(env)
    elsewhere = chalkconf.hook("intercept")(question) if question and chalkconf.hook("intercept") else None
    if not question or elsewhere:
        proc.kill()
        if sketcher:
            sketcher.kill()
        if elsewhere:
            return elsewhere(Convo(state, send), env, send)
        return
    t0 = time.monotonic()
    convo = Convo(state, send)
    send({"op": "draw", "shapes": [{"kind": "busy", "color": "blue", "ttl": 60, "tag": "busy"}],
          "replace": not convo.active})
    lk = look(ocr=False)
    t_look = time.monotonic()

    pen = Pen(draw, ground, keep_first=convo.active)  # a first question's first stroke replaces the busy dots
    drawn = pen.times
    turn = Turn(pen, convo, send)
    turn_box = {"turn": turn}   # what the streaming board needs from the sketch thread

    sketch = {}
    def get_sketch():
        try:
            send_message(sketcher, message(question, lk["shot"], lk["size"],
                                           os.environ.get("CHALK_SKETCH_PROMPT", "scene.md"),
                                           "Reply with JSON matching the schema.", convo.context(for_sketch=True)))
            sent = 0
            for kind, val in stream_answer(sketcher, "objects" if STREAM else "shapes"):
                if kind == "done":
                    sketch["answer"], sketch["t"] = val, time.monotonic()
                    if STREAM and turn_box.get("tag"):  # anything the stream parser didn't hand over
                        sc = val.get("scene") or {}
                        for o in (sc.get("objects") or [])[sent:]:
                            turn_box["turn"].stream_add(o)
                        if sc.get("steps") and not turn_box.get("steps"):
                            # the steps come last: everything streamed is up, so the board lands on the last step
                            # (its knob turning); Back and Replay walk through them
                            turn_box["turn"].pen.put({"kind": "scene_add", "steps": sc["steps"], "at_end": True},
                                                     keep=True, tag=turn_box["tag"])
                elif kind == "header" and STREAM:
                    head = close_json(val)
                    if head and head.get("draw") and isinstance(head.get("scene"), dict):
                        turn_box["steps"] = bool(head["scene"].get("steps"))
                        turn_box["tag"] = turn_box["turn"].stream_open(head)
                        sketch["t_open"] = time.monotonic()
                elif kind == "shape" and turn_box.get("tag"):
                    turn_box["turn"].stream_add(val)
                    sent += 1
        except Exception as e:  # the marks still stand on their own
            sketch["error"] = str(e)[:300]
    sketch_thread = threading.Thread(target=get_sketch, daemon=True) if sketcher else None
    if sketch_thread:
        sketch_thread.start()

    put = turn.mark

    streamed, ans = 0, None
    try:
        send_message(proc, message(question, lk["shot"], lk["size"], os.environ.get("CHALK_POLICY", "policy.md"),
                                   "Reply with the drawing as JSON matching the schema: the main mark first.",
                                   "\n\n".join(x for x in ((chalkconf.hook("context") or (lambda q: ""))(question),
                                                             convo.context()) if x)))
        for kind, val in stream_answer(proc):
            if kind == "done":
                ans = val
            elif kind == "shape":
                streamed += 1
                put(val)
    except Exception as e:
        draw([{"kind": "card", "title": "chalk couldn't answer", "text": str(e)[:300],
               "box": [lk["size"][0] / 2 - 1, 60, 2, 2], "ttl": 8}])
        die(str(e))
    t_model = time.monotonic()
    shapes = ans.get("shapes", [])
    for s in shapes[streamed:]:  # anything the stream parser didn't hand over
        put(s)
    say = (ans.get("say") or "").strip()
    t_first = drawn[0] if drawn else None
    turn.finish(ans, lk["size"])
    t_marks = time.monotonic()

    t_sketch = t_check = None
    check = {}
    if sketch_thread:
        sketch_thread.join(60)
        if turn_box.get("tag"):  # streamed: it's up already
            t_sketch = sketch.get("t_open")
        elif turn.sketch(sketch.get("answer"), say):
            t_sketch = time.monotonic()
        sk = sketch.get("answer") or {}
        if t_sketch:
            if os.environ.get("CHALK_SCENE_CHECK", "1") != "0" and isinstance(sk.get("scene"), dict):
                try:  # the first board is up already; the checked one takes its place
                    fixed, check["issues"] = check_scene(env, model, os.environ.get("CHALK_CHECK_EFFORT", sketch_effort),
                                                         question, lk["shot"], sk, state)
                    if fixed:
                        check["first"] = sk
                        sketch["answer"] = fixed
                        turn.resketch(fixed, say)
                        t_check = time.monotonic()
                except Exception as e:
                    check["error"] = str(e)[:300]
    convo.end_turn(question, say)
    ms = lambda t: round((t - t0) * 1000) if t else None
    log = {"at": round(time.time()), "q": question, "model": model, "effort": effort, "look_ms": ms(t_look), "first_shape_ms": ms(t_first),
           "model_done_ms": ms(t_model), "marks_done_ms": ms(t_marks), "sketch_ms": ms(t_sketch), "check_ms": ms(t_check), "check": check or None,
           "total_ms": ms(time.monotonic()), "cost_usd": round(sum(SPEND), 4), "answer": ans, "sketch": sketch.get("answer"),
           "sketch_error": sketch.get("error")}
    with open(os.path.join(state, "ask.log"), "a") as f:
        f.write(json.dumps(log) + "\n")
    if "--json" in args:
        print(json.dumps(log))
    else:
        print(f"{say}\n(first stroke {log['first_shape_ms']} ms, marks {log['marks_done_ms']} ms, "
              f"diagram {log['sketch_ms']} ms; look {log['look_ms']} ms, {model})")
