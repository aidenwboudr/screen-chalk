#!/usr/bin/python3
"""chalk overlay: a click-through layer on top of every Sway output that draws
hand-drawn circles, arrows, underlines, labels and cards where an agent asks.

It only draws. Working out *where* (OCR, element ids) happens in `chalk`, which
sends finished layout-pixel coordinates here over a unix socket, one JSON
object per line:

  {"op": "draw", "shapes": [...], "replace": true}   -> {"ok": true, "ids": [...]}
  {"op": "clear"}  {"op": "ping"}  {"op": "stats"}  {"op": "peek"}  {"op": "quit"}
  {"op": "tags"}   -> {"ok": true, "tags": {tag: "shown" | "away" | "lost" | "docked"}} for what's still drawn
  {"op": "wait", "tag": t, "timeout": s}   -> a panel's result once the user answers it (panel.py)
  {"op": "focus"}  {"op": "copy", "n": 1}   (Super+; and Super+Ctrl+C)

Everything is click-through except panels (panel.py): paper cards with questions, buttons or code to copy.
They take clicks without taking the keyboard; they get the keyboard only when the user asks (Super+;, or a click on a
text field) and give it back when the user answers, presses Esc or clicks their own window.

The drawing keeps up with the screen (track.py): marks belong to the window they were drawn on and hide
when it's switched away (another workspace, another tab) and come back with it; a mark whose target
scrolled away or changed fades out; a card or diagram that's in the way of what the user is doing moves to
empty screen, or docks as a small tab at the screen's edge. `peek` (Super+.) brings docked ones back, or
docks them all when none are.

Coordinates are Sway layout pixels across all outputs (the same space `desk` uses).
Runs with the system python3 (needs gi + GtkLayerShell + PangoCairo).
"""
import ctypes, json, math, os, random, socket, subprocess, sys, threading, time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
try:  # only the live overlay needs it; offscreen.py paints the same shapes onto a screenshot without it
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import GtkLayerShell
except (ValueError, ImportError):
    GtkLayerShell = None
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango, PangoCairo
import cairo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chalkconf  # noqa: E402
chalkconf.load()
import layout  # noqa: E402
import panel  # noqa: E402
import scene  # noqa: E402
import track  # noqa: E402
try:  # numpy: only the overlay's tracking needs it (offscreen rendering and a forwarding-only machine don't)
    import follow  # noqa: E402
except ImportError:
    follow = None

HERE = os.path.dirname(os.path.abspath(__file__))
SOCK = os.environ.get("CHALK_SOCK") or os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "chalk.sock")

# Ink: bright enough for dark themes, with a soft dark under-stroke so it also reads on white.
COLORS = {
    "red": (1.00, 0.33, 0.24), "orange": (1.00, 0.60, 0.15), "yellow": (1.00, 0.86, 0.20),
    "green": (0.30, 0.90, 0.45), "blue": (0.30, 0.65, 1.00), "purple": (0.75, 0.50, 1.00),
    "pink": (1.00, 0.45, 0.75), "white": (0.97, 0.97, 0.97),
}
DEFAULT_COLOR = "red"
HAND_FONT = "Caveat"
DRAW_SPEED = 1900.0   # px of stroke per second
MIN_DRAW, MAX_DRAW = 0.22, 0.65  # seconds per shape
GAP = 0.12            # seconds between shapes in one draw call
FADE = 0.35
BLINK = 0.12          # seconds a followed mark takes to hide when its target scrolls out of sight
BLINK_IN = 0.08       # ... and to come back when it does
POINT_PULSE = 4.2     # seconds a point keeps pulsing
SKETCH_SPEED = 1100.0  # canvas px per second for diagrams (slower than marks: you watch it being drawn)
SKETCH_MAX = 4.5
BOARD_MAX_W, BOARD_MAX_H = 560, 440

# Panel look, after HeyClicky's real-time graphics: cream cards with a graph-paper grid and a header strip,
# dark ink, one hand font. The visual bench (bench/visual/RESULTS.md) picked it over the first dark chalkboard.
# One tutor, one look: the Teach app's tokens (Teach's workspace design direction), which are
# scene.py's THEMES["dark"]: Manim's ground, serif for what the tutor says, the 3b1b colours by role.
THEME = {"panel": panel.GROUND, "panel_a": 1.0, "border": panel.EDGE_STRONG, "shadow": 0.45,
         "ink": panel.INK, "title_font": panel.TITLE, "title_size": 17,
         "text_font": panel.HAND, "card_font": panel.HAND, "card_title": panel.INK,
         "card_body": panel.INK2, "card_size": 15.5, "card_title_size": 15.5,
         "accents": {"yellow": panel.YELLOW, "blue": panel.BLUE, "red": panel.RED, "green": panel.GREEN,
                     "pink": (0.82, 0.28, 0.74), "purple": (0.60, 0.45, 0.67), "orange": (1.0, 0.525, 0.184),
                     "white": panel.INK}}
HEADER = 26  # the window header strip
LABEL_FONT, LABEL_SIZE = panel.HAND, 16  # labels are dark chips with a bar in the mark's colour
BUSY_MAX = 60.0       # the "thinking" dots stop animating after this, in case nobody replaces them
SLIDE = 0.45          # seconds a panel takes to move out of the way
PILL_SIZE = 17
DEBUG = os.environ.get("CHALK_TRACK_DEBUG") == "1"
DUMP = os.environ.get("CHALK_FOLLOW_DUMP")  # a directory: the Follower saves every frame it judges there
# marks that point at something on screen move with it when it scrolls (Follower); panels don't
FOLLOW_KINDS = {"circle", "box", "underline", "highlight", "strike", "arrow", "point", "spotlight", "label", "path"}
PILL_HINT = os.environ.get("CHALK_KEY_PEEK", "Super+.")   # the key that opens docked panels again (install.sh --sway binds it)
TOAST_TTL = 1.8
panel.setup(THEME)


def load_font():
    """Register the bundled fonts for this process only (the handwriting ones, and Teach's serif and sans)."""
    try:
        fc = ctypes.CDLL("libfontconfig.so.1")
        fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        for name in ("Caveat.ttf", "PatrickHand.ttf", "SourceSerif4.ttf", "SourceSans3.ttf"):
            fc.FcConfigAppFontAddFile(None, os.path.join(HERE, "fonts", name).encode())
    except OSError:
        pass


def pastel(rgb, k=0.78):
    """An ink colour washed into the dark ground, for fills (k = how much ground)."""
    return panel.tint(rgb, k)


def color_of(c):
    if isinstance(c, (list, tuple)) and len(c) >= 3:
        return tuple(float(v) for v in c[:3])
    if isinstance(c, str) and c.startswith("#") and len(c) == 7:
        return tuple(int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return COLORS.get(c or DEFAULT_COLOR, COLORS[DEFAULT_COLOR])


# ---------- geometry: every shape becomes strokes (polylines) ----------

def lerp(a, b, t):
    return a + (b - a) * t

def sample_quad(p0, c, p1, step=4.0):
    n = max(2, int(math.dist(p0, p1) / step))
    out = []
    for i in range(n + 1):
        t = i / n
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * c[0] + t * t * p1[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * c[1] + t * t * p1[1]
        out.append((x, y))
    return out

def rough_line(p0, p1, rnd, bow=0.025):
    """A line with a slight hand wobble: quadratic with its control nudged sideways."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    L = math.hypot(dx, dy) or 1
    nx, ny = -dy / L, dx / L
    off = rnd.uniform(-1, 1) * bow * L
    c = ((p0[0] + p1[0]) / 2 + nx * off, (p0[1] + p1[1]) / 2 + ny * off)
    return sample_quad(p0, c, p1)

def rough_ellipse(cx, cy, rx, ry, rnd):
    """One pen loop that overshoots its start and spirals out a little, like a real circle by hand."""
    start = rnd.uniform(-2.6, -1.9)          # start up and to the left, like most people do
    sweep = 2 * math.pi + rnd.uniform(0.35, 0.6)
    wob_a, wob_p = rnd.uniform(0.015, 0.035), rnd.uniform(0, 2 * math.pi)
    tilt = rnd.uniform(-0.06, 0.06)
    n = max(48, int((rx + ry) * 0.6))
    pts = []
    for i in range(n + 1):
        t = i / n
        a = start + sweep * t
        grow = lerp(0.95, 1.07, t)
        k = grow * (1 + wob_a * math.sin(2 * a + wob_p))
        x, y = rx * k * math.cos(a), ry * k * math.sin(a)
        pts.append((cx + x * math.cos(tilt) - y * math.sin(tilt), cy + x * math.sin(tilt) + y * math.cos(tilt)))
    return pts

def rough_pill(x, y, w, h, rnd):
    """A rounded rectangle drawn in one pen stroke, overshooting its start a little."""
    r = min(h / 2, 14)
    pts = []
    corners = [(x + w - r, y + r, -math.pi / 2, 0), (x + w - r, y + h - r, 0, math.pi / 2),
               (x + r, y + h - r, math.pi / 2, math.pi), (x + r, y + r, math.pi, 1.5 * math.pi)]
    for cx, cy, a0, a1 in corners:
        for i in range(7):
            a = a0 + (a1 - a0) * i / 6
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts.append(pts[0])
    pts += pts[1:4]  # overshoot like a real pen
    wob = rnd.uniform(-1.2, 1.2)
    return [(px + wob * math.sin(i * 0.7), py + wob * math.cos(i * 0.9)) for i, (px, py) in enumerate(pts)]


def box_of(s):
    b = s.get("box")
    if b:
        return [float(v) for v in b]
    at = s.get("at") or s.get("to")
    if at:
        return [float(at[0]) - 1, float(at[1]) - 1, 2.0, 2.0]
    return None

def edge_point(box, toward):
    """Point on the box's edge facing `toward` (where an arrow tip should land)."""
    x, y, w, h = box
    cx, cy = x + w / 2, y + h / 2
    dx, dy = toward[0] - cx, toward[1] - cy
    if dx == 0 and dy == 0:
        return cx, cy
    sx = (w / 2) / abs(dx) if dx else float("inf")
    sy = (h / 2) / abs(dy) if dy else float("inf")
    s = min(sx, sy)
    return cx + dx * s, cy + dy * s

def auto_tail(box, screen, length=150):
    """Pick where an arrow starts: off the target diagonally, toward the roomier side of the screen."""
    x, y, w, h = box
    cx, cy = x + w / 2, y + h / 2
    sx, sy, sw, sh = screen
    ux = -1 if cx > sx + sw / 2 else 1
    uy = 1 if cy < sy + sh * 0.35 else -1
    ang = math.atan2(uy * 0.75, ux)
    r = length + max(w, h) / 2
    tx, ty = cx + math.cos(ang) * r, cy + math.sin(ang) * r
    return min(max(tx, sx + 20), sx + sw - 20), min(max(ty, sy + 20), sy + sh - 20)


def catmull_rom(pts, steps=8):
    """A smooth curve through the points (for plotted curves the model gives as a few samples)."""
    if len(pts) < 3:
        return pts
    ext = [pts[0]] + pts + [pts[-1]]
    out = []
    for i in range(1, len(ext) - 2):
        p0, p1, p2, p3 = ext[i - 1], ext[i], ext[i + 1], ext[i + 2]
        for j in range(steps):
            t = j / steps
            t2, t3 = t * t, t * t * t
            out.append(tuple(0.5 * ((2 * p1[k]) + (-p0[k] + p2[k]) * t + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
                                    + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3) for k in (0, 1)))
    out.append(tuple(pts[-1]))
    return out


def compile_sketch(s, rnd):
    """A diagram the model describes as items in its own canvas units -> ops the pen draws in order.

    s = {"size": [w, h], "items": [...], "title": str, "board": bool (default true)}; items:
      {"line": [x1,y1,x2,y2]}  {"arrow": [x1,y1,x2,y2]}  {"rect": [x,y,w,h]}  {"circle": [cx,cy,r]}
      {"path": [[x,y],...], "smooth": bool, "closed": bool}  {"text": "...", "at": [x,y], "size": 20, "align": "center"}
    each with optional "color", "dash", "width". With board false the canvas is the target box on screen."""
    box = box_of(s)
    board = s.get("board", True) is not False
    if not board and box:
        w, h = box[2], box[3]
    else:
        w, h = (s.get("size") or [400, 300])[:2]
    ops = []

    def stroke(pts, it, closed=False):
        pts = [tuple(map(float, p)) for p in pts]
        if closed:
            pts.append(pts[0])
        ops.append({"type": "stroke", "pts": pts, "rgb": color_of(it["color"]) if it.get("color") else None,
                    "fill": it.get("fill"),
                    "cname": it.get("color"),
                    "width": float(it.get("width", 2.6)), "dash": bool(it.get("dash")),
                    "len": Shape.poly_len(pts)})

    for it in s.get("items", []):
        try:
            if "line" in it or "arrow" in it:
                x1, y1, x2, y2 = map(float, it.get("line") or it.get("arrow"))
                stroke(rough_line((x1, y1), (x2, y2), rnd, 0.02), it)
                if "arrow" in it:
                    ang = math.atan2(y2 - y1, x2 - x1)
                    hl = float(it.get("head", 11))
                    for side in (-1, 1):
                        a = ang + math.pi + side * 0.45
                        stroke([(x2, y2), (x2 + math.cos(a) * hl, y2 + math.sin(a) * hl)], it)
            elif "rect" in it:
                x, y, rw, rh = map(float, it["rect"])
                corners = [(x, y), (x + rw, y), (x + rw, y + rh), (x, y + rh)]
                pts = [corners[0]]
                for a, b in zip(corners, corners[1:] + corners[:1]):
                    pts += rough_line(a, b, rnd, 0.008)[1:]
                stroke(pts, it)
            elif "circle" in it:
                cx, cy, r = map(float, it["circle"][:3])
                stroke(rough_ellipse(cx, cy, r, r, rnd), it)
            elif "path" in it:
                pts = [tuple(map(float, p[:2])) for p in it["path"]]
                if len(pts) >= 2:
                    stroke(catmull_rom(pts) if it.get("smooth") else pts, it, bool(it.get("closed")))
            elif "text" in it:
                at = it.get("at") or [0, 0]
                ops.append({"type": "text", "text": str(it["text"]), "at": (float(at[0]), float(at[1])),
                            "size": float(it.get("size", 20)), "align": it.get("align", "left"),
                            "rgb": color_of(it["color"]) if it.get("color") else None, "cname": it.get("color"),
                            "len": 9.0 * len(str(it["text"]))})
        except (TypeError, ValueError, IndexError):
            continue  # one malformed item shouldn't sink the diagram
    return {"w": float(w), "h": float(h), "ops": ops, "length": sum(o["len"] for o in ops) or 1.0,
            "title": s.get("title"), "board": board, "segments": [], "caption": s.get("caption")}


_MEASURE = None


def text_width(text, font, size):
    global _MEASURE
    if _MEASURE is None:
        _MEASURE = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8))
    lay = PangoCairo.create_layout(_MEASURE)
    fd = Pango.FontDescription.from_string(font)
    fd.set_absolute_size(size * Pango.SCALE)
    lay.set_font_description(fd)
    lay.set_text(text, -1)
    return lay.get_pixel_extents()[1].width


def text_height(text, font, size, width):
    """Height Pango will actually take for this text wrapped at `width` (a guess cut captions off)."""
    global _MEASURE
    if _MEASURE is None:
        _MEASURE = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8))
    lay = PangoCairo.create_layout(_MEASURE)
    fd = Pango.FontDescription.from_string(font)
    fd.set_absolute_size(size * Pango.SCALE)
    lay.set_font_description(fd)
    lay.set_width(int(width * Pango.SCALE))
    lay.set_wrap(Pango.WrapMode.WORD_CHAR)
    lay.set_text(text, -1)
    return lay.get_pixel_extents()[1].height


def caption_height(sk, width):
    """Room for the board's caption, measured."""
    if not sk.get("caption"):
        return 0
    return text_height(sk["caption"], THEME["card_font"], THEME["card_size"], width) + 14


def board_size(sk):
    """Scale and outer size of a sketch's chalkboard: (k, pad, title_h, w, h)."""
    k = min(1.4, BOARD_MAX_W / sk["w"], BOARD_MAX_H / sk["h"])
    pad, th = 18, (34 if sk["title"] else 0) + HEADER
    w = sk["w"] * k + 2 * pad
    return k, pad, th, w, sk["h"] * k + 2 * pad + th + caption_height(sk, w - 2 * pad)


class Shape:
    """A compiled shape: strokes to reveal over `dur`, plus optional text/card/spotlight extras."""

    def __init__(self, spec, screen, rnd, t0):
        self.spec = spec
        self.kind = spec.get("kind", "circle")
        self.rgb = color_of(spec.get("color"))
        self.width = float(spec.get("width", 4.0))
        self.strokes = []          # list of polylines
        self.label = None          # (text, x, y, size)
        self.card = None
        self.hole = None           # spotlight
        self.marker = False
        self.anchor = None         # where labels hang off
        self.label_pref = None
        self.placed = {}           # "label"/"card" -> (x, y, w, h), decided on first paint
        self.ink_box = None        # what this shape covers, so other labels keep off it
        self.ttl = float(spec.get("ttl", TOAST_TTL if self.kind == "toast" else 0) or 0)
        self.panel = None
        self.build(screen, rnd)
        pts = [q for st in self.strokes for q in st]
        if pts:
            xs, ys = [q[0] for q in pts], [q[1] for q in pts]
            self.ink_box = (min(xs) - 4, min(ys) - 4, max(xs) - min(xs) + 8, max(ys) - min(ys) + 8)
        elif box_of(spec) and self.kind not in ("card", "label", "busy", "panel", "toast"):
            self.ink_box = tuple(box_of(spec))
        L = sum(self.poly_len(p) for p in self.strokes)
        self.dur = min(MAX_DRAW, max(MIN_DRAW, L / DRAW_SPEED)) if self.strokes else 0.25
        if self.kind == "sketch":  # a diagram is drawn at a watchable pace, op by op
            self.dur = min(SKETCH_MAX, max(0.8, self.sketch["length"] / SKETCH_SPEED))
            self.sketch["segments"] = [{"ops": self.sketch["ops"], "t0": t0, "dur": self.dur,
                                        "length": self.sketch["length"]}]
        self.t0 = t0
        self.total = L
        self.fading_at = None
        self.first_frame = None
        # keeping up with the screen (see Tracker)
        self.ctx = None            # (workspace, window id, window rect) it was drawn on
        self.away = False          # its window isn't on screen right now
        self.vis_t = None          # when it last hid or came back (for the fade)
        self.offset = (0, 0)       # its window moved by this much since
        self.docked = False        # a panel tucked away as a tab
        self.dock_t = None
        self.slide = None          # (from_x, from_y, t0) while a panel moves
        self.grace_until = 0.0     # don't move it again before this
        self.base = None           # what its target looked like once drawn
        self.strikes = 0
        self.reported = False      # a panel's result went out (or nobody will ask for it)
        self.scroll = (0, 0)       # its target scrolled this far inside its window (Follower)
        self.lost = False          # its target is out of sight (scrolled off, covered): hidden until it's back
        self.follow = None         # its follow.Anchor once the Follower has one; False when it can't follow

    @property
    def panel_key(self):
        """"card" / "sketch" for the big panels that cover the screen (their box is in placed[key])."""
        if self.kind == "card":
            return "card"
        if self.kind == "sketch" and self.sketch["board"]:
            return "sketch"
        if self.kind == "panel":
            return "panel"
        return None

    def arrow_from(self, tail, tgt, rnd):
        """(Re)build an arrow's strokes from tail to the edge of tgt; returns its direction."""
        s = self.spec
        self.strokes = []
        ex, ey = edge_point(tgt, tail)
        gap = float(s.get("gap", 8))
        d = math.dist(tail, (ex, ey)) or 1
        tip = (ex + (tail[0] - ex) / d * gap, ey + (tail[1] - ey) / d * gap)
        dx, dy = tip[0] - tail[0], tip[1] - tail[1]
        L = math.hypot(dx, dy) or 1
        nx, ny = -dy / L, dx / L
        bow = rnd.choice((-1, 1)) * L * rnd.uniform(0.08, 0.16)
        c = ((tail[0] + tip[0]) / 2 + nx * bow, (tail[1] + tip[1]) / 2 + ny * bow)
        shaft = sample_quad(tail, c, tip)
        self.strokes.append(shaft)
        # head follows the shaft's final direction
        hx, hy = shaft[-1][0] - shaft[-3][0], shaft[-1][1] - shaft[-3][1]
        ang = math.atan2(hy, hx)
        hl = float(s.get("head", 18))
        for side in (-1, 1):
            a = ang + math.pi + side * rnd.uniform(0.42, 0.55)
            self.strokes.append(rough_line(tip, (tip[0] + math.cos(a) * hl, tip[1] + math.sin(a) * hl), rnd, 0.05))
        self.anchor = (tail[0] - 2, tail[1] - 2, 4, 4)
        return dx, dy

    def finish_strokes(self):
        """Recompute what depends on the strokes (after an arrow is re-aimed)."""
        pts = [q for st in self.strokes for q in st]
        xs, ys = [q[0] for q in pts], [q[1] for q in pts]
        self.ink_box = (min(xs) - 4, min(ys) - 4, max(xs) - min(xs) + 8, max(ys) - min(ys) + 8)
        self.total = sum(self.poly_len(p) for p in self.strokes)
        self.dur = min(MAX_DRAW, max(MIN_DRAW, self.total / DRAW_SPEED))

    @staticmethod
    def poly_len(p):
        return sum(math.dist(p[i], p[i + 1]) for i in range(len(p) - 1))

    def build(self, screen, rnd):
        s, k = self.spec, self.kind
        box = box_of(s)
        if k == "circle":
            x, y, w, h = box
            pad = float(s.get("pad", 10))
            if w > 2.2 * h and h < 60:
                # text-like target: a hand-drawn rounded box that hugs it, so it doesn't run through the
                # lines above and below the way an oval does
                p2 = min(pad, 6)
                self.strokes.append(rough_pill(x - p2, y - p2 * 0.6, w + 2 * p2, h + 1.2 * p2, rnd))
                rx, ry = w / 2 + p2, h / 2 + p2
            else:
                # an oval through roughly the target's corners (a true circumscribed ellipse would balloon
                # around long text); tiny targets still get a visible loop
                rx, ry = max(w / 2 * 1.04 + pad, 18), max(h / 2 * 1.15 + pad, 16)
                self.strokes.append(rough_ellipse(x + w / 2, y + h / 2, rx, ry, rnd))
            self.anchor = (x + w / 2 - rx, y + h / 2 - ry, rx * 2, ry * 2)
        elif k == "box":
            x, y, w, h = box
            pad = float(s.get("pad", 6))
            x0, y0, x1, y1 = x - pad, y - pad, x + w + pad, y + h + pad
            o = 6
            for a, b in (((x0 - o, y0), (x1 + o / 2, y0)), ((x1, y0 - o), (x1, y1 + o / 2)),
                         ((x1 + o, y1), (x0 - o / 2, y1)), ((x0, y1 + o), (x0, y0 - o / 2))):
                self.strokes.append(rough_line(a, b, rnd, 0.012))
            self.anchor = (x0, y0, x1 - x0, y1 - y0)
        elif k == "underline":
            x, y, w, h = box
            yy = y + h + float(s.get("gap", 4))
            pts, n = [], max(8, int(w / 6))
            ph, drift = rnd.uniform(0, 6.28), rnd.uniform(-3, 3)
            waves = max(1.0, w / 160)
            for i in range(n + 1):
                t = i / n
                pts.append((x - 4 + (w + 8) * t, yy + 1.3 * math.sin(t * waves * 6.28 + ph) + drift * t))
            self.strokes.append(pts)
            if s.get("double"):
                self.strokes.append([(px + 3, py + 6) for px, py in pts[::-1]])
            self.anchor = (x, y, w, h + 8)
        elif k == "highlight":
            x, y, w, h = box
            self.marker = True
            self.width = h * 0.95
            self.strokes.append(rough_line((x - 3, y + h / 2 + 1), (x + w + 3, y + h / 2 - 1), rnd, 0.004))
            self.anchor = (x, y, w, h)
        elif k == "strike":
            x, y, w, h = box
            self.strokes.append(rough_line((x - 4, y + h * 0.55), (x + w + 4, y + h * 0.48), rnd, 0.01))
            self.anchor = (x, y, w, h)
        elif k == "arrow":
            tgt = box or [0, 0, 1, 1]
            frm = s.get("from")
            tail = tuple(frm) if frm else auto_tail(tgt, screen, float(s.get("length", 150)))
            dx, dy = self.arrow_from(tail, tgt, rnd)
            # the label goes behind the tail, away from where the arrow points, so it never sits on the shaft
            if abs(dx) > abs(dy):
                self.label_pref = "left" if dx > 0 else "right"
            else:
                self.label_pref = "above" if dy > 0 else "below"
        elif k == "path":
            pts = [tuple(map(float, p)) for p in s.get("points", [])]
            if len(pts) >= 2:
                out = [pts[0]]
                for a, b in zip(pts, pts[1:]):
                    out += rough_line(a, b, rnd, 0.01)[1:]
                self.strokes.append(out)
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                self.anchor = (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
        elif k == "point":
            x, y, w, h = box
            self.anchor = (x, y, w, h)
            self.point = (x + w / 2, y + h / 2)
        elif k == "spotlight":
            x, y, w, h = box
            pad = float(s.get("pad", 10))
            self.hole = (x - pad, y - pad, w + 2 * pad, h + 2 * pad)
            self.anchor = self.hole
        elif k == "label":
            at = s.get("at") or [box[0], box[1]]
            self.anchor = (float(at[0]), float(at[1]), 0, 0)
        elif k == "card":
            self.anchor = box
        elif k == "sketch":
            self.sketch = compile_sketch(s, rnd)
            self.anchor = box
        elif k == "panel":
            self.panel = scene.Scene(s) if isinstance(s.get("scene"), dict) else panel.Panel(s)
            self.anchor = box
        elif k == "toast":
            self.anchor = None
        elif k == "busy":
            # "I heard you, I'm looking": three ink dots breathing at top centre until replaced
            sx, sy, sw, sh = screen
            self.point = (sx + sw / 2, sy + 34)
            self.anchor = (self.point[0] - 30, self.point[1] - 10, 60, 20)
        text = s.get("text") if k not in ("card", "sketch", "panel", "toast") else None
        if text:
            self.label = (str(text), float(s.get("size", 30)))
        if k == "card":
            self.card = {"title": s.get("title"), "text": s.get("text") or s.get("body") or "",
                         "image": s.get("image"), "width": float(s.get("card_width", 340))}


# ---------- the window(s) ----------

class Overlay:
    def __init__(self):
        self.shapes = []
        self.hidden = False
        self.lock = threading.RLock()  # draw() places chalkboards while holding it
        self.windows = []
        self.ticking = False
        self.stats = {"draws": 0, "last_latency_ms": None}
        self.pending_latency = None
        self.rnd = random.Random()
        self.busy = None
        self.image_cache = {}
        self.env = dict(os.environ)
        self.sway = None
        self.results = {}              # panel tag -> its result, until `wait` collects it
        self.cond = threading.Condition()
        self.keys_for = None           # the panel that has the keyboard
        self.dragging = None           # (shape, slider action) while a scene slider is held
        self.keys_t = 0.0
        self.last_copy = None          # (shape, target index, when): Super+Ctrl+C again copies the next block
        self.tracker = Tracker(self)
        self.follower = Follower(self) if follow else None
        self.display = Gdk.Display.get_default()
        self.sync_monitors()
        # monitors come and go (external screen, kanshi moving outputs): keep one surface per monitor
        self.display.connect("monitor-added", lambda *a: GLib.idle_add(self.sync_monitors))
        self.display.connect("monitor-removed", lambda *a: GLib.idle_add(self.sync_monitors))

    def monitor_rects(self):
        out = []
        for i in range(self.display.get_n_monitors()):
            g = self.display.get_monitor(i).get_geometry()
            out.append((g.x, g.y, g.width, g.height))
        return out

    def sync_monitors(self):
        """Rebuild the surfaces if the monitor set or layout changed since they were made."""
        if [(w.geom.x, w.geom.y, w.geom.width, w.geom.height) for w in self.windows] == self.monitor_rects():
            return False
        for w in self.windows:
            w.destroy()
        self.windows = []
        for i in range(self.display.get_n_monitors()):
            self.add_window(self.display.get_monitor(i))
        return False

    def screen(self):
        rs = [w.geom for w in self.windows]
        x0 = min(r.x for r in rs); y0 = min(r.y for r in rs)
        x1 = max(r.x + r.width for r in rs); y1 = max(r.y + r.height for r in rs)
        return (x0, y0, x1 - x0, y1 - y0)

    def screen_for(self, box):
        if box:
            cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
            for w in self.windows:
                g = w.geom
                if g.x <= cx < g.x + g.width and g.y <= cy < g.y + g.height:
                    return (g.x, g.y, g.width, g.height)
        g = self.windows[0].geom
        return (g.x, g.y, g.width, g.height)

    def add_window(self, mon):
        win = Gtk.Window()
        win.geom = mon.get_geometry()
        GtkLayerShell.init_for_window(win)
        GtkLayerShell.set_layer(win, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(win, "chalk")
        GtkLayerShell.set_monitor(win, mon)
        for e in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM, GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
            GtkLayerShell.set_anchor(win, e, True)
        GtkLayerShell.set_exclusive_zone(win, -1)
        GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.NONE)
        vis = win.get_screen().get_rgba_visual()
        if vis:
            win.set_visual(vis)
        win.set_app_paintable(True)
        area = Gtk.DrawingArea()
        area.connect("draw", self.on_draw, win)
        # panels take the mouse (only where they are: the input region follows them, see on_draw)
        area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        area.connect("button-press-event", self.on_press, win)
        area.connect("button-release-event", self.on_release, win)
        area.connect("motion-notify-event", self.on_motion, win)
        area.connect("leave-notify-event", lambda *a: self.set_hover(None, None))
        win.connect("key-press-event", self.on_key)
        win.connect("focus-out-event", self.on_focus_out)
        win.add(area)
        win.area = area
        win.input_rects = []
        win.connect("realize", lambda w: w.input_shape_combine_region(cairo.Region()))
        win.show_all()
        win.get_window().input_shape_combine_region(cairo.Region(), 0, 0)
        self.windows.append(win)

    # --- commands (called on the GTK thread) ---

    def draw(self, specs, replace=True, busy=None, seed=None):
        self.sync_monitors()  # outputs moved without a hotplug signal (e.g. a scale or position change)
        now = time.monotonic()
        if seed is not None:  # same wobble every time (the visual bench replays answers to compare renderers)
            self.rnd.seed(seed)
        if busy:
            self.busy = layout.Busy(busy["grid"], busy.get("origin", (0, 0)))
        if getattr(self, "tracker", None) is not None:  # which windows the new shapes land on, as of now (~30 ms)
            self.sway = track.sway_state(self.env) or self.sway
        with self.lock:
            if replace:
                for sh in self.shapes:
                    # a question still waiting on them outlives the tutor's next marks (the user dismisses it, or clears)
                    if sh.fading_at is None and not (sh.panel and sh.panel.pending):
                        sh.fading_at = now
            t = now
            ids = []
            for spec in specs:
                t += float(spec.get("delay", 0)) / 1000.0
                tag = spec.get("tag")
                if spec.get("kind") == "sketch" and spec.get("extend"):
                    if self.extend_sketch(spec, t):
                        ids.append(spec["extend"])
                        continue
                if spec.get("kind") == "scene_replace":  # a corrected scene board, swapped in where it stands
                    target = next((sh for sh in self.shapes if sh.fading_at is None and sh.spec.get("tag") == tag
                                   and isinstance(getattr(sh, "panel", None), scene.Scene)), None)
                    if target and isinstance(spec.get("scene"), dict):
                        target.panel.replace({"scene": spec["scene"]})
                        ids.append(tag)
                    continue
                if spec.get("kind") == "scene_add":  # a streaming scene board's next objects
                    target = next((sh for sh in self.shapes if sh.fading_at is None and sh.spec.get("tag") == tag
                                   and isinstance(getattr(sh, "panel", None), scene.Scene)), None)
                    if target:
                        target.panel.add_objects(spec.get("objects") or [])
                        if spec.get("steps"):
                            target.panel.set_steps(spec["steps"], at_end=bool(spec.get("at_end")))
                        ids.append(tag)
                    continue
                if spec.get("kind", "card") == "card" and panel.wants_panel(spec):
                    spec = dict(spec, kind="panel")   # code or a lot of text: a card the user can copy from
                if tag:  # same tag = the new version of that shape (the note card of the latest answer)
                    for old in self.shapes:
                        if old.spec.get("tag") == tag and old.fading_at is None:
                            old.fading_at = now
                            old.reported = True          # its replacement answers for that tag now
                    with self.cond:
                        self.results.pop(tag, None)
                try:
                    sh = Shape(spec, self.screen_for(box_of(spec)), self.rnd, t)
                except Exception as e:  # one bad shape shouldn't sink the call
                    print(f"chalk: bad shape {spec}: {e}", file=sys.stderr)
                    continue
                if sh.kind == "arrow" and sh.label and not spec.get("from"):
                    self.aim_from_label(sh)
                self.shapes.append(sh)
                ids.append(id(sh))
                if sh.kind == "sketch" and sh.sketch["board"]:
                    self.place_board(sh)
                self.give_context(sh)
                if sh.ttl:
                    GLib.timeout_add(int((sh.t0 - now + sh.dur + sh.ttl) * 1000) + 50, self.kick)
                t += sh.dur + GAP
        if getattr(self, "follower", None):
            self.follower.wake.set()
        self.stats["draws"] += 1
        self.pending_latency = now
        self.kick()
        return ids

    def remove(self, tags):
        """Erase the shapes with these tags (a follow-up answer dropping marks that no longer fit)."""
        now = time.monotonic()
        with self.lock:
            for sh in self.shapes:
                if sh.spec.get("tag") in tags and sh.fading_at is None:
                    sh.fading_at = now
        self.kick()

    def extend_sketch(self, spec, t):
        """Draw more onto an existing board, in its own canvas units, picking up where it left off."""
        target = next((sh for sh in self.shapes if sh.kind == "sketch" and sh.fading_at is None
                       and sh.spec.get("tag") == spec["extend"]), None)
        if target is None:
            return False
        sk = target.sketch
        more = compile_sketch(dict(spec, size=[sk["w"], sk["h"]], board=sk["board"],
                                   box=target.spec.get("box")), self.rnd)
        dur = min(SKETCH_MAX, max(0.6, more["length"] / SKETCH_SPEED))
        start = max(t, target.t0 + target.dur)
        sk["segments"].append({"ops": more["ops"], "t0": start, "dur": dur, "length": more["length"]})
        target.dur = start + dur - target.t0  # keeps the overlay animating until the new strokes are done
        if target.ttl:
            target.ttl = max(target.ttl, 0)
        return True

    def clear(self, everything=True):
        """Wipe the drawing. A tutor's clear (everything=False) leaves questions still waiting on them."""
        now = time.monotonic()
        with self.lock:
            for sh in self.shapes:
                if sh.fading_at is None and (everything or not (sh.panel and sh.panel.pending)):
                    sh.fading_at = now
        self.kick()

    # --- keeping up with the screen (the Tracker thread decides, these apply it on the GTK thread) ---

    def give_context(self, sh):
        """Remember which workspace and window a new shape was drawn on (where its target is)."""
        if getattr(self, "tracker", None) is None:
            return
        b = self.target_box(sh) or (sh.placed.get(sh.panel_key) if sh.panel_key else None)
        if b and sh.kind != "busy":
            sh.ctx = track.context_at(self.sway, b[0] + b[2] / 2, b[1] + b[3] / 2)

    @staticmethod
    def target_box(sh):
        """The thing a shape points at (an arrow's target, not its tail), in drawing coordinates."""
        b = box_of(sh.spec) or sh.hole or sh.anchor  # the thing itself, not the ink around it
        return tuple(b) if b and len(b) >= 4 else None

    @staticmethod
    def shift(sh):
        """How far a shape is drawn from where it was put: its window moved, its target scrolled."""
        return sh.offset[0] + sh.scroll[0], sh.offset[1] + sh.scroll[1]

    def footprint(self, sh):
        """Everything a shape paints, in screen px (window offset and scroll included)."""
        dx, dy = self.shift(sh)
        boxes = [b for k, b in sh.placed.items() if not (sh.docked and k == sh.panel_key)]
        if sh.docked:
            boxes.append(self.pill_rect(sh))
        if sh.ink_box:
            boxes.append(sh.ink_box)
        return [(b[0] + dx, b[1] + dy, b[2], b[3]) for b in boxes if b]

    def slid(self, sh, x, y):
        """A panel's position while it glides from its old spot to the new one."""
        if not sh.slide:
            return x, y
        fx, fy, t0 = sh.slide
        f = min(1.0, (time.monotonic() - t0) / SLIDE)
        f = 1 - (1 - f) ** 3
        return lerp(fx, x, f), lerp(fy, y, f)

    def pill_hint(self, sh):
        return panel.KEY_FOCUS if sh.panel and sh.panel.pending else PILL_HINT

    def pill_text(self, sh):
        if sh.kind == "card":
            t = sh.card.get("title") or sh.card.get("text") or "note"
        elif sh.panel:
            pn = sh.panel
            t = pn.title or (pn.questions[pn.page]["prompt"] if pn.questions else "") or "note"
            t = panel.plain(t)
        else:
            t = sh.sketch.get("title") or "diagram"
        t = " ".join(str(t).split())
        return t if len(t) <= 34 else t[:32].rstrip() + "…"

    def pill_rect(self, sh):
        """Docked panels stack as small tabs down the right edge of their screen, under the bar."""
        box = sh.placed.get(sh.panel_key) or (0, 0, 1, 1)
        sx, sy, sw, shh = self.screen_for(box)
        with self.lock:
            docked = [s for s in self.shapes if s.docked and s.fading_at is None and not s.away
                      and self.screen_for(s.placed.get(s.panel_key)) == (sx, sy, sw, shh)]
        i = docked.index(sh) if sh in docked else 0
        if getattr(sh, "pill_w", None) is None:  # measured once (on the GTK thread, which docks it)
            sh.pill_w = text_width(self.pill_text(sh), LABEL_FONT, PILL_SIZE) + text_width(self.pill_hint(sh), LABEL_FONT, PILL_SIZE - 4) + 44
        w = sh.pill_w
        h = int(PILL_SIZE * 1.25) + 12
        return (sx + sw - w - 14 - sh.offset[0], sy + 46 + i * (h + 8) - sh.offset[1], w, h)

    def paint_pill(self, cr, sh, alpha):
        x, y, w, h = self.pill_rect(sh)
        a = alpha * (min(1.0, (time.monotonic() - sh.dock_t) / FADE) if sh.dock_t else 1.0)
        T = THEME
        rounded(cr, x + 1, y + 3, w, h, h / 2)
        cr.set_source_rgba(0, 0, 0, T["shadow"] * a); cr.fill()
        rounded(cr, x, y, w, h, h / 2)
        cr.set_source_rgba(*T["panel"], 0.96 * a); cr.fill_preserve()
        cr.set_source_rgba(*T["border"], a); cr.set_line_width(1.3); cr.stroke()
        cr.new_sub_path()
        cr.arc(x + 13, y + h / 2, 3.2, 0, 2 * math.pi)  # a dot: there's more here (amber: it's waiting on you)
        cr.set_source_rgba(*T["accents"]["yellow" if sh.panel and sh.panel.pending else "blue"], a); cr.fill()
        lay = self.layout(cr, self.pill_text(sh), LABEL_FONT, PILL_SIZE)
        _, ext = lay.get_pixel_extents()
        cr.move_to(x + 22 - ext.x, y + (h - ext.height) / 2 - ext.y)
        cr.set_source_rgba(*T["card_body"], a)
        PangoCairo.show_layout(cr, lay)
        hint = self.layout(cr, self.pill_hint(sh), LABEL_FONT, PILL_SIZE - 4)
        _, he = hint.get_pixel_extents()
        cr.move_to(x + w - 12 - he.width - he.x, y + (h - he.height) / 2 - he.y)
        cr.set_source_rgba(*T["card_body"], 0.5 * a)
        PangoCairo.show_layout(cr, hint)

    def apply_context(self, st):
        """Workspaces or windows changed: hide what belongs to windows that left, bring back what returned,
        follow windows that moved, drop marks whose window closed or was resized (its contents reflowed)."""
        now = time.monotonic()
        self.sway = st
        with self.lock:
            for sh in self.shapes:
                if sh.ctx is None or sh.fading_at is not None:
                    continue
                state, off = track.where_now(st, sh.ctx)
                if state == "gone":  # its window closed or reflowed: a mark is wrong now, a panel tucks away
                    if sh.panel_key:
                        sh.ctx = None
                        if not sh.docked:
                            sh.docked, sh.dock_t = True, now
                            self.pill_rect(sh)
                    else:
                        sh.fading_at = now
                    continue
                away = state != "show"
                if away != sh.away:
                    sh.away, sh.vis_t, sh.vis_fade = away, now, FADE
                if off is not None and not sh.panel_key:
                    sh.offset = off
        self.kick()
        return False

    def follow_update(self, updates):
        """What the Follower found: where marks' targets scrolled to, and which went out of sight or came back.
        Repaints without restarting the Tracker's comparison (that's for changes of the drawing itself)."""
        now = time.monotonic()
        fade = False
        with self.lock:
            for sh, shift, lost in updates:
                if sh.fading_at is not None:
                    continue
                if not lost:
                    sh.scroll = (shift[0] - sh.offset[0], shift[1] - sh.offset[1])
                if lost != sh.lost:
                    sh.lost, sh.vis_t, sh.vis_fade, fade = lost, now, BLINK if lost else BLINK_IN, True
            # (drawing marks ahead of a moving page didn't track any closer on the bench and overshot at stops)
            self.invalidate([b for sh, *_ in updates for b in getattr(sh, "painted_boxes", []) + self.paints(sh)])
        if fade and not self.ticking:
            self.ticking = True
            GLib.timeout_add(16, self.tick)
        return False

    def paints(self, sh):
        """One box around everything a followed mark paints (its ink, its label and the line between them)."""
        if sh.hole:  # a spotlight dims the whole screen around its hole
            return [self.screen()]
        boxes = self.footprint(sh)
        if sh.kind == "point":
            (px, py), (dx, dy) = sh.point, self.shift(sh)
            boxes.append((px + dx - 34, py + dy - 34, 70, 70))
        if not boxes:
            return []
        x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x1, y1 = max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)
        return [(x0, y0, x1 - x0, y1 - y0)]

    def invalidate(self, boxes):
        """Repaint only these layout rects (a full-screen repaint costs a full-screen buffer every frame)."""
        for w in self.windows:
            g = w.geom
            for x, y, bw, bh in boxes:
                x0, y0 = max(g.x, int(x) - 14), max(g.y, int(y) - 14)
                x1, y1 = min(g.x + g.width, int(x + bw) + 15), min(g.y + g.height, int(y + bh) + 15)
                if x0 < x1 and y0 < y1:
                    w.area.queue_draw_area(x0 - g.x, y0 - g.y, x1 - x0, y1 - y0)

    def adapt(self, stale, move, dock):
        """What the Tracker found: marks whose target changed, panels in their way."""
        now = time.monotonic()
        with self.lock:
            for sh in stale:
                if sh.fading_at is None:
                    sh.fading_at = now
            for sh, (x, y) in move:
                key = sh.panel_key
                ox, oy, w, h = sh.placed[key]
                sh.slide = (ox, oy, now)
                sh.placed[key] = (x, y, w, h)
                sh.grace_until = now + 3
            for sh in dock:
                sh.docked, sh.dock_t = True, now
                self.pill_rect(sh)
        self.kick()
        return False

    def counts(self):
        with self.lock:
            live = [s for s in self.shapes if s.fading_at is None and s.kind not in ("busy", "toast")]
        return {"shown": sum(not s.away and not s.docked and not s.lost for s in live), "away": sum(s.away for s in live),
                "docked": sum(s.docked for s in live), "lost": sum(s.lost and not s.away for s in live),
                "panels": [[round(v) for v in s.placed[s.panel_key][:2]] for s in live if s.panel_key and s.panel_key in s.placed],
                # where their panels are (chalk watch ignores changes there: that's them answering, not working)
                "boxes": [[round(v) for v in b] for s in self.shapes if s.kind in ("panel", "toast") for b in self.footprint(s)],
                "keys": self.keys_for is not None,
                "waiting": [s.spec.get("tag") for s in live if s.panel and s.panel.pending]}

    def tags(self):
        """Where each tagged shape is now; a tag missing here was closed, faded or cleared (the hotkey's follow-ups
        check this, so they don't answer as if something the user closed were still on screen)."""
        with self.lock:
            return {s.spec["tag"]: "docked" if s.docked else "away" if s.away else "lost" if s.lost else "shown"
                    for s in self.shapes if s.fading_at is None and s.spec.get("tag")}

    def peek(self):
        """Super+.: bring docked panels back (where they fit best now), or tuck all of them away."""
        now = time.monotonic()
        with self.lock:
            panels = [s for s in self.shapes if s.panel_key and s.fading_at is None and not s.away]
        docked = [s for s in panels if s.docked]
        if not docked:
            for sh in panels:
                sh.docked, sh.dock_t = True, now
                self.pill_rect(sh)
            self.kick()
            return {"docked": len(panels)}
        last = self.tracker.last if getattr(self, "tracker", None) else None
        for sh in docked:
            key = sh.panel_key
            x, y, w, h = sh.placed[key]
            if last:
                mean, detail, heat, origin = last
                others = [b for o in panels if o is not sh and not o.docked for b in self.footprint(o)]
                pos = track.place_panel(w, h, self.screen_for((x, y, w, h)), detail, heat * 0, origin,
                                        avoid=others, cur=(x, y))
                if pos:
                    sh.placed[key] = (pos[0], pos[1], w, h)
            px, py = self.pill_rect(sh)[:2]
            sh.docked, sh.dock_t, sh.slide = False, None, (px, py, now)
            sh.grace_until = now + 6  # the user asked for it: don't tuck it away again right away
        self.kick()
        return {"shown": len(docked)}

    def kick(self):
        self.epoch = getattr(self, "epoch", 0) + 1  # the Tracker restarts its comparison after any change of ours
        for w in self.windows:
            w.area.queue_draw()
        if not self.ticking:
            self.ticking = True
            GLib.timeout_add(16, self.tick)

    def tick(self):
        now = time.monotonic()
        with self.lock:
            for sh in self.shapes:
                if sh.ttl and sh.fading_at is None and now > sh.t0 + sh.dur + sh.ttl:
                    sh.fading_at = now
            gone = [s for s in self.shapes if s.fading_at is not None and now - s.fading_at >= FADE]
            self.shapes = [s for s in self.shapes if s.fading_at is None or now - s.fading_at < FADE]
            # only animate while something moves; ttl expiry has its own one-shot timer (see draw)
            busy = any(now < s.t0 + s.dur + 0.05 or s.fading_at is not None
                       or (s.vis_t is not None and now - s.vis_t < FADE)
                       or (s.slide is not None and now - s.slide[2] < SLIDE)
                       or (s.dock_t is not None and now - s.dock_t < FADE)
                       or (s.panel is not None and s.panel.animating(now))
                       or (s.kind == "point" and now < s.t0 + POINT_PULSE)
                       or (s.kind == "busy" and now < s.t0 + BUSY_MAX) for s in self.shapes)
        for s in gone:   # a panel that went before the user answered (cleared, replaced): whoever waits hears so
            if s.panel:
                self.post(s, {"status": "gone"})
                if self.keys_for is s:
                    self.release_keys()
        for w in self.windows:
            w.area.queue_draw()
        if not busy:
            self.ticking = False
        return busy

    # --- painting ---

    def on_draw(self, area, cr, win):
        g = win.geom
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        if self.hidden:
            self.set_input(win, [])
            return True
        cr.translate(-g.x, -g.y)
        self.paint_all(cr, time.monotonic())
        self.set_input(win, self.input_rects(win))
        return True

    def paint_all(self, cr, now):
        """Every live shape as of `now`, in layout px (the caller translates to its surface)."""
        with self.lock:
            shapes = list(self.shapes)
        for sh in shapes:
            if now < sh.t0:
                continue
            p = min(1.0, (now - sh.t0) / sh.dur) if sh.dur else 1.0
            p = 1 - (1 - p) ** 2.2  # ease out: quick pen, slow finish
            alpha = 1.0
            if sh.fading_at is not None:
                alpha = max(0.0, 1 - (now - sh.fading_at) / FADE)
            hide = sh.away or sh.lost
            if sh.vis_t is not None:  # its window went away or came back, its target scrolled out of sight or back
                f = min(1.0, (now - sh.vis_t) / getattr(sh, "vis_fade", FADE))
                alpha *= (1 - f) if hide else f
            elif hide:
                alpha = 0.0
            if alpha <= 0:
                continue
            if sh.first_frame is None:
                sh.first_frame = now
                if self.pending_latency is not None:
                    self.stats["last_latency_ms"] = round((now - self.pending_latency) * 1000, 1)
                    self.pending_latency = None
            cr.save()
            dx, dy = self.shift(sh)
            if sh.follow:  # where it was painted when: the Follower leaves the ink out of the frames it judges
                log = sh.__dict__.setdefault("painted", [])
                if not log or log[-1][1] != (dx, dy):
                    log.append((now * 1000, (dx, dy)))
                    del log[:-12]
                    sh.painted_boxes = self.paints(sh)
            if dx or dy:
                cr.translate(dx, dy)
            self.paint_shape(cr, sh, p, alpha, now)
            cr.restore()

    def stroke_partial(self, cr, sh, p, alpha):
        budget = sh.total * p
        for pts in sh.strokes:
            if budget <= 0:
                break
            cr.move_to(*pts[0])
            for a, b in zip(pts, pts[1:]):
                d = math.dist(a, b)
                if d >= budget:
                    t = budget / d if d else 1
                    cr.line_to(lerp(a[0], b[0], t), lerp(a[1], b[1], t))
                    budget = 0
                    break
                cr.line_to(*b)
                budget -= d
        path = cr.copy_path()
        r, g, b = sh.rgb
        cr.set_line_cap(cairo.LINE_CAP_ROUND if not sh.marker else cairo.LINE_CAP_BUTT)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        if sh.marker:
            cr.set_source_rgba(r, g, b, 0.38 * alpha)
            cr.set_line_width(sh.width)
            cr.stroke()
            return
        # soft dark under-stroke keeps bright ink readable on white pages
        cr.set_source_rgba(0, 0, 0, 0.28 * alpha)
        cr.set_line_width(sh.width + 3.5)
        cr.stroke()
        cr.append_path(path)
        cr.set_source_rgba(r, g, b, alpha)
        cr.set_line_width(sh.width)
        cr.stroke()

    def paint_shape(self, cr, sh, p, alpha, now):
        cr.new_path()  # the path isn't part of save/restore; don't inherit the last shape's
        if sh.hole:
            x, y, w, h = sh.hole
            sx, sy, sw, shh = self.screen()
            cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            cr.rectangle(sx, sy, sw, shh)
            rounded(cr, x, y, w, h, 12)
            cr.set_source_rgba(0, 0, 0, 0.5 * alpha * p)
            cr.fill()
            cr.set_fill_rule(cairo.FILL_RULE_WINDING)
        if sh.strokes:
            self.stroke_partial(cr, sh, p, alpha)
        if sh.kind == "point":
            self.paint_point(cr, sh, p, alpha, now)
        if sh.kind == "busy":
            x, y = sh.point
            r, g, b = sh.rgb
            rounded(cr, x - 46, y - 17, 92, 34, 17)
            cr.set_source_rgba(0.11, 0.11, 0.14, 0.85 * alpha)
            cr.fill()
            for i in range(3):
                ph = (now - sh.t0) * 5 - i * 0.9
                lift = max(0.0, math.sin(ph)) * 5
                cr.new_sub_path()
                cr.arc(x + (i - 1) * 20, y - lift, 5, 0, 2 * math.pi)
                cr.set_source_rgba(r, g, b, alpha * (0.55 + 0.45 * max(0.0, math.sin(ph))))
                cr.fill()
        if sh.label and p > 0.55 and not (sh.kind != "label" and self.board_up()):  # the board explains instead
            self.paint_label(cr, sh, min(1, (p - 0.55) / 0.45), alpha)
        if sh.card:
            self.paint_card(cr, sh, p, alpha)
        if sh.kind == "sketch":
            self.paint_sketch(cr, sh, alpha, now)
        if sh.panel:
            self.paint_panel(cr, sh, p, alpha, now)
        if sh.kind == "toast":
            self.paint_toast(cr, sh, p, alpha)

    def label_size(self, sh):
        return text_width(sh.label[0], LABEL_FONT, LABEL_SIZE) + 22, int(LABEL_SIZE * 1.25) + 10

    def aim_from_label(self, sh):
        """Put an arrow's label first, a little way off its target over quiet screen, then draw the
        arrow from the label to the target: judges kept flagging arrows that start in empty space."""
        tgt = box_of(sh.spec) or [0, 0, 1, 1]
        w, h = self.label_size(sh)
        with self.lock:
            live = [s for s in self.shapes if s.fading_at is None]
        taken = [b for s in live for b in s.placed.values()]
        clear = [s.ink_box for s in live if s.ink_box] + [tuple(tgt)]
        m = 70  # room for a visible arrow between the label and its target
        ring = (tgt[0] - m, tgt[1] - m, tgt[2] + 2 * m, tgt[3] + 2 * m)
        x, y = layout.place(ring, w, h, self.screen_for(tgt), taken, self.busy, None, clear)
        lab = (x, y, w, h)
        cx, cy = tgt[0] + tgt[2] / 2, tgt[1] + tgt[3] / 2
        tail = layout.leader(lab, tgt)
        start = tail[0] if tail else (x + w / 2, y + h / 2)
        # leave the label a few px before the line starts
        d = math.dist(start, (cx, cy)) or 1
        start = (start[0] + (cx - start[0]) / d * 6, start[1] + (cy - start[1]) / d * 6)
        sh.arrow_from(start, tgt, self.rnd)
        sh.finish_strokes()
        sh.placed["label"] = lab

    def place_board(self, sh):
        """Decide where a sketch's chalkboard goes, once, when it's drawn (so labels placed after
        it keep off it): the emptiest spot on that screen, or the model's own pick if it's about as empty."""
        _, _, _, pw, ph = board_size(sh.sketch)
        with self.lock:
            live = [s for s in self.shapes if s.fading_at is None and s is not sh]
        taken = [b for s in live for b in s.placed.values()]
        clear = [s.ink_box for s in live if s.ink_box] + ([sh.ink_box] if sh.ink_box else [])
        want = sh.spec.get("at")
        x, y = layout.place_board(pw, ph, self.screen_for(sh.anchor or (tuple(want[:2]) + (pw, ph) if want else None)),
                                  sh.anchor, taken, self.busy, clear, tuple(want[:2]) if want else None)
        sh.placed["sketch"] = (x, y, pw, ph)

    def paint_sketch(self, cr, sh, alpha, now):
        sk = sh.sketch
        first = sk["segments"][0]
        prog = min(1.0, (now - first["t0"]) / first["dur"]) if first["dur"] else 1.0  # steady pen pace
        if sk["board"]:
            k, pad, th, pw, ph = board_size(sk)
            if "sketch" not in sh.placed:
                self.place_board(sh)
            if sh.docked:
                return self.paint_pill(cr, sh, alpha)
            x, y = self.slid(sh, *sh.placed["sketch"][:2])
            a = alpha * min(1.0, prog * 6)  # the board fades in first, then the drawing starts
            T = THEME
            rounded(cr, x + 3, y + 5, pw, ph, 16)
            cr.set_source_rgba(0, 0, 0, T["shadow"] * a); cr.fill()
            rounded(cr, x, y, pw, ph, 16)
            cr.set_source_rgba(*T["panel"], T["panel_a"] * a); cr.fill_preserve()
            cr.set_source_rgba(*T["border"], a); cr.set_line_width(1.5); cr.stroke()
            self.paper_grid(cr, x, y, pw, ph, a)
            if sk["title"]:
                lay = self.layout(cr, sk["title"], T["title_font"], T["title_size"], pw - 2 * pad)
                cr.move_to(x + pad, y + 10 + HEADER)
                cr.set_source_rgba(*T["ink"], a)
                PangoCairo.show_layout(cr, lay)
            ox, oy, ink = x + pad, y + pad + th, T["ink"]
            if sk.get("caption"):  # the spoken answer, under the drawing
                lay = self.layout(cr, sk["caption"], T["card_font"], T["card_size"], pw - 2 * pad)
                ch = caption_height(sk, pw - 2 * pad)
                cr.move_to(x + pad, y + ph - pad - ch + 10)
                cr.set_source_rgba(*T["card_body"], a)
                PangoCairo.show_layout(cr, lay)
        else:
            bx, by = (sh.anchor or (0, 0, 0, 0))[:2]
            ox, oy, k, ink = bx, by, 1.0, sh.rgb
        for i, seg in enumerate(sk["segments"]):
            sp = min(1.0, max(0.0, (now - seg["t0"]) / seg["dur"])) if seg["dur"] else 1.0
            if i == 0:
                sp = min(1.0, max(0.0, sp * 1.15 - 0.1))  # small head start for the board itself
            self.paint_ops(cr, sk, seg["ops"], seg["length"] * sp, ox, oy, k, ink, alpha)

    def paint_ops(self, cr, sk, ops, budget, ox, oy, k, ink, alpha):
        for op in ops:
            if budget <= 0:
                break
            frac = min(1.0, budget / op["len"]) if op["len"] else 1.0
            budget -= op["len"]
            rgb = op["rgb"] or ink
            if sk["board"] and op.get("cname") in THEME["accents"]:
                rgb = THEME["accents"][op["cname"]]
            if op["type"] == "stroke":
                self.sketch_stroke(cr, op, frac, ox, oy, k, rgb, alpha, halo=not sk["board"])
            else:
                self.sketch_text(cr, op, frac, ox, oy, k, rgb, alpha, halo=not sk["board"], board=sk["board"])

    def sketch_stroke(self, cr, op, frac, ox, oy, k, rgb, alpha, halo):
        pts = [(ox + px * k, oy + py * k) for px, py in op["pts"]]
        if op.get("fill") and frac > 0.6:  # a pastel block, washed in as the outline closes
            name = op["fill"] if isinstance(op["fill"], str) else op.get("cname")
            base = THEME["accents"].get(name) or color_of(name)
            cr.new_path()
            cr.move_to(*pts[0])
            for q in pts[1:]:
                cr.line_to(*q)
            cr.close_path()
            cr.set_source_rgba(*pastel(base), alpha * min(1.0, (frac - 0.6) / 0.4))
            cr.fill()
        need = op["len"] * k * frac
        cr.new_path()
        cr.move_to(*pts[0])
        for a, b in zip(pts, pts[1:]):
            d = math.dist(a, b)
            if d >= need:
                t = need / d if d else 1
                cr.line_to(lerp(a[0], b[0], t), lerp(a[1], b[1], t))
                break
            cr.line_to(*b)
            need -= d
        path = cr.copy_path()
        cr.set_line_cap(cairo.LINE_CAP_ROUND); cr.set_line_join(cairo.LINE_JOIN_ROUND)
        if op["dash"]:
            cr.set_dash([7 * k, 6 * k])
        if halo:
            cr.set_source_rgba(0, 0, 0, 0.3 * alpha); cr.set_line_width(op["width"] * k + 3); cr.stroke()
            cr.append_path(path)
        cr.set_source_rgba(*rgb, alpha); cr.set_line_width(op["width"] * k); cr.stroke()
        cr.set_dash([])

    def sketch_text(self, cr, op, frac, ox, oy, k, rgb, alpha, halo, board=True):
        font, bold = (THEME["text_font"], False) if board else (HAND_FONT, True)
        lay = self.layout(cr, op["text"], font, op["size"] * k * (1.1 if font != HAND_FONT else 1.0), None,
                          Pango.Weight.BOLD if bold else None)
        _, ext = lay.get_pixel_extents()
        base = lay.get_baseline() / Pango.SCALE
        x, y = ox + op["at"][0] * k, oy + op["at"][1] * k - base  # "at" is the baseline, like SVG text
        if op["align"] == "center":
            x -= ext.width / 2
        elif op["align"] == "right":
            x -= ext.width
        cr.save()
        cr.rectangle(x - 4, y - 4, (ext.width + 8) * frac, ext.height + 12)  # written left to right
        cr.clip()
        cr.new_path()
        cr.move_to(x - ext.x, y)
        PangoCairo.layout_path(cr, lay)
        if halo:
            box = (x, y, ext.width, ext.height)
            cr.set_source_rgba(*self.halo(box), (0.9 if self.is_light(box) else 0.55) * alpha)
            cr.set_line_width(4); cr.stroke_preserve()
        cr.set_source_rgba(*rgb, alpha)
        cr.fill()
        cr.restore()

    def is_light(self, box):
        return bool(self.busy and self.busy.light(*box))

    def halo(self, box):
        """Outline colour for text drawn straight on the screen: white on light pages, dark on dark ones."""
        return (1.0, 1.0, 1.0) if self.is_light(box) else (0.0, 0.0, 0.0)

    def paper_grid(self, cr, x, y, w, h, a):
        """Faint graph paper inside the card and a thin header strip with three dots, like HeyClicky's cards."""
        cr.save()
        rounded(cr, x, y, w, h, 16)
        cr.clip()
        cr.set_line_width(1)
        cr.set_source_rgba(0.55, 0.62, 0.75, 0.10 * a)
        step = 18
        gx = x + step
        while gx < x + w:
            cr.move_to(gx, y + 26); cr.line_to(gx, y + h); gx += step
        gy = y + 26 + step
        while gy < y + h:
            cr.move_to(x, gy); cr.line_to(x + w, gy); gy += step
        cr.stroke()
        cr.set_source_rgba(0.93, 0.91, 0.86, a)
        cr.rectangle(x, y, w, 24); cr.fill()
        cr.set_source_rgba(0.83, 0.79, 0.71, a)
        cr.move_to(x, y + 24); cr.line_to(x + w, y + 24); cr.stroke()
        for i, c in enumerate(((0.93, 0.45, 0.40), (0.95, 0.75, 0.30), (0.45, 0.75, 0.45))):
            cr.new_sub_path()
            cr.arc(x + 16 + i * 14, y + 12, 4, 0, 2 * math.pi)
            cr.set_source_rgba(*c, 0.85 * a)
            cr.fill()
        cr.restore()

    def board_up(self):
        return any(s.kind == "sketch" and s.sketch["board"] and s.fading_at is None
                   for s in self.shapes)

    def paint_point(self, cr, sh, p, alpha, now):
        x, y = sh.point
        r, g, b = sh.rgb if sh.spec.get("color") else COLORS["blue"]
        # a pulse ring around the spot, then a cursor-shaped pointer resting just off it
        if now - sh.t0 < POINT_PULSE:  # a few pulses to catch the eye, then it sits still (no idle repaints)
            ph = ((now - sh.t0) % 1.4) / 1.4
            cr.set_source_rgba(r, g, b, (1 - ph) * 0.7 * alpha)
            cr.set_line_width(3)
            cr.new_sub_path()
            cr.arc(x, y, 8 + 22 * ph, 0, 2 * math.pi)
            cr.stroke()
        cr.set_source_rgba(r, g, b, alpha)
        cr.new_sub_path()
        cr.arc(x, y, 5, 0, 2 * math.pi)
        cr.fill()
        ox, oy = x + 10, y + 10
        cr.move_to(ox, oy); cr.line_to(ox + 4, oy + 20); cr.line_to(ox + 9, oy + 13); cr.line_to(ox + 17, oy + 12); cr.close_path()
        cr.set_source_rgba(r, g, b, alpha)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.9 * alpha)
        cr.set_line_width(1.5)
        cr.stroke()

    def layout(self, cr, text, font, size, width=None, weight=None):
        lay = PangoCairo.create_layout(cr)
        fd = Pango.FontDescription.from_string(font)
        fd.set_absolute_size(size * Pango.SCALE)
        if weight:
            fd.set_weight(weight)
        lay.set_font_description(fd)
        if width:
            lay.set_width(int(width * Pango.SCALE))
            lay.set_wrap(Pango.WrapMode.WORD_CHAR)
        lay.set_text(text, -1)
        return lay

    def place(self, sh, key, w, h, pref=None):
        """Where this shape's label/card goes. Decided once, so it doesn't hop around while animating:
        beside the mark, on screen, off other labels and marks, and over empty screen if possible."""
        if key in sh.placed:
            return sh.placed[key][:2]
        anchor = sh.anchor or (self.screen()[0] + 40, self.screen()[1] + 40, 0, 0)
        with self.lock:
            live = [s for s in self.shapes if s.fading_at is None]
        taken = [b for s in live for b in s.placed.values()]
        clear = [s.ink_box for s in live if s.ink_box]
        x, y = layout.place(anchor, w, h, self.screen_for(anchor), taken, self.busy, pref, clear)
        sh.placed[key] = (x, y, w, h)
        return x, y

    def paint_label(self, cr, sh, p, alpha):
        """A callout tied to its mark by a thin line: a dark chip (Teach's ground) with a bar in the mark's colour."""
        text, _ = sh.label
        lay = self.layout(cr, text, LABEL_FONT, LABEL_SIZE, width=320)
        _, ext = lay.get_pixel_extents()
        px, py = 12, 6
        w, h = ext.width + 2 * px, ext.height + 2 * py
        anchor = sh.anchor or (0, 0, 0, 0)
        if sh.kind == "label":
            x, y = anchor[0], anchor[1]
            sh.placed.setdefault("label", (x, y, w, h))
        else:
            x, y = self.place(sh, "label", w, h, sh.spec.get("label_side") or sh.label_pref)
        r, g, b = sh.rgb
        target = sh.ink_box if sh.kind != "arrow" else anchor
        ln = layout.leader((x, y, w, h), target) if target and sh.kind != "label" else None
        a = alpha * min(1.0, p * 1.5)
        if ln:
            (x0, y0), (x1, y1) = ln
            cr.new_path(); cr.move_to(x0, y0); cr.line_to(x1, y1)
            cr.set_source_rgba(r, g, b, 0.8 * a); cr.set_line_width(1.6); cr.stroke()
        rounded(cr, x + 1, y + 3, w, h, 8)
        cr.set_source_rgba(0, 0, 0, 0.4 * a); cr.fill()
        rounded(cr, x, y, w, h, 8)
        cr.set_source_rgba(*panel.GROUND, 0.97 * a); cr.fill_preserve()
        cr.set_source_rgba(*panel.EDGE_STRONG, a); cr.set_line_width(1); cr.stroke()
        cr.save()
        rounded(cr, x, y, w, h, 8); cr.clip()
        cr.rectangle(x, y, 3, h); cr.set_source_rgba(r, g, b, a); cr.fill()
        cr.restore()
        cr.move_to(x + px - ext.x, y + py - ext.y)
        cr.set_source_rgba(*panel.INK, a)
        PangoCairo.show_layout(cr, lay)

    def paint_card(self, cr, sh, p, alpha):
        c = sh.card
        cw = c["width"]
        pad = 16
        T = THEME
        title = self.layout(cr, c["title"], T["title_font"], T["card_title_size"], cw - 2 * pad, Pango.Weight.SEMIBOLD) if c["title"] else None
        body = self.layout(cr, c["text"], T["card_font"], T["card_size"], cw - 2 * pad) if c["text"] else None
        img = None
        if c["image"]:
            img = self.image_cache.get(c["image"])
            if img is None and os.path.exists(c["image"]):
                try:
                    pb = GdkPixbuf.Pixbuf.new_from_file(c["image"])
                    iw = cw - 2 * pad
                    ih = min(220, pb.get_height() * iw / pb.get_width())
                    img = pb.scale_simple(int(iw), int(ih), GdkPixbuf.InterpType.BILINEAR)
                    self.image_cache[c["image"]] = img
                except GLib.Error:
                    img = None
        h = pad
        th = title.get_pixel_extents()[1].height if title else 0
        bh = body.get_pixel_extents()[1].height if body else 0
        ih = img.get_height() if img else 0
        h += (ih + 10 if img else 0) + (th + 6 if title else 0) + bh + pad
        x, y = self.place(sh, "card", cw, h, sh.spec.get("label_side", "right"))
        if sh.docked:
            return self.paint_pill(cr, sh, alpha)
        x, y = self.slid(sh, x, y)
        a = alpha * min(1, p * 1.6)
        rise = (1 - min(1, p * 1.4)) * 10
        y += rise
        cr.save()
        rounded(cr, x + 2, y + 4, cw, h, 14)
        cr.set_source_rgba(0, 0, 0, T["shadow"] * a)
        cr.fill()
        rounded(cr, x, y, cw, h, 14)
        cr.set_source_rgba(*T["panel"], T["panel_a"] * a)
        cr.fill_preserve()
        r, g, b = sh.rgb
        if T["border"]:
            cr.set_source_rgba(*T["border"], a)
            cr.set_line_width(1.5)
        else:
            cr.set_source_rgba(r, g, b, 0.85 * a)
            cr.set_line_width(2)
        cr.stroke()
        yy = y + pad
        if img:
            cr.save()
            rounded(cr, x + pad, yy, img.get_width(), img.get_height(), 8)
            cr.clip()
            Gdk.cairo_set_source_pixbuf(cr, img, x + pad, yy)
            cr.paint_with_alpha(a)
            cr.restore()
            yy += ih + 10
        if title:
            cr.move_to(x + pad, yy)
            cr.set_source_rgba(*THEME["card_title"], a)
            PangoCairo.show_layout(cr, title)
            yy += th + 6
        if body:
            cr.move_to(x + pad, yy)
            cr.set_source_rgba(*THEME["card_body"], a)
            PangoCairo.show_layout(cr, body)
        cr.restore()

    # --- panels: cards the user answers, clicks and copies from (panel.py has what's inside) ---

    def place_panel(self, sh, w, h):
        """Beside what it's about (right of the code line, say) with the lines around that kept readable, over
        the emptiest screen there; with nothing to be about, the emptiest spot on the screen."""
        box = box_of(sh.spec) if sh.spec.get("box") else None
        with self.lock:
            live = [s for s in self.shapes if s.fading_at is None and s is not sh]
        taken = [b for s in live for b in s.placed.values()]
        clear = [s.ink_box for s in live if s.ink_box]
        want = sh.spec.get("at")
        scr = self.screen_for(box or (tuple(want[:2]) + (w, h) if want else None))
        sx, sy, sw, shh = scr
        if want:
            x, y = want[0], want[1]
        elif box:  # the emptiest screen near it (code lines are short: usually just right of the code)
            reading = (box[0] - 8, box[1] - 72, box[2] + 16, box[3] + 144)
            if self.busy:
                x, y = layout.place_board(w, h, scr, box, taken, self.busy, clear + [reading])
            else:
                x, y = layout.place(box, w, h, scr, taken, None, sh.spec.get("label_side", "right"), clear + [reading])
        elif self.busy:
            x, y = layout.place_board(w, h, scr, None, taken, self.busy, clear)
        else:  # no picture of the screen: the right side, a bit above the middle
            x, y = sx + sw - w - 28, sy + max(60, (shh - h) / 2 - 80)
        x = min(max(x, sx + 6), sx + sw - w - 6)
        y = min(max(y, sy + 36), sy + shh - h - 8)
        sh.placed["panel"] = (x, y, w, h)

    def paint_panel(self, cr, sh, p, alpha, now):
        pn = sh.panel
        surf, w, h, _ = pn.snapshot()
        if "panel" not in sh.placed:
            self.place_panel(sh, w, h)
        x, y, ow, oh = sh.placed["panel"]
        if (w, h) != (ow, oh):  # it grew or shrank (the next question, feedback): keep the top, stay on screen
            sx, sy, sw, shh = self.screen_for((x, y, ow, oh))
            y = max(sy + 36, min(y, sy + shh - h - 8))
            sh.placed["panel"] = (x, y, w, h)
        if sh.docked:
            return self.paint_pill(cr, sh, alpha)
        x, y = self.slid(sh, x, y)
        a = alpha * min(1, p * 1.6)
        y += (1 - min(1, p * 1.4)) * 10
        box = box_of(sh.spec) if sh.spec.get("box") else None
        if box:  # what it's about: a faint pencil bracket on that line and a leader to it
            bx, by, bw, bh = box
            ink = THEME["accents"]["blue"]
            cr.new_path()
            cr.move_to(bx - 7, by + 1); cr.line_to(bx - 10, by + 1); cr.line_to(bx - 10, by + bh - 1); cr.line_to(bx - 7, by + bh - 1)
            cr.set_source_rgba(*ink, 0.55 * a); cr.set_line_width(2); cr.stroke()
            ln = layout.leader((x, y, w, h), (bx + bw, by, 1, bh))
            if ln:
                (x0, y0), (x1, y1) = ln
                cr.move_to(x0, y0); cr.line_to(x1 + 4, y1)
                cr.set_dash([4, 5]); cr.set_source_rgba(*ink, 0.35 * a); cr.set_line_width(1.4); cr.stroke(); cr.set_dash([])
        M = panel.MARGIN
        f = 1.0
        if pn.prev and now - pn.prev[3] < panel.XFADE:  # a new state crossfades in over the old one
            f = (now - pn.prev[3]) / panel.XFADE
            cr.set_source_surface(pn.prev[0], x - M, y - M)
            cr.paint_with_alpha(a * (1 - f))
        cr.set_source_surface(surf, x - M, y - M)
        cr.paint_with_alpha(a * f)

    def paint_toast(self, cr, sh, p, alpha):
        """A tiny state toast ("Copied", "Step 9 done"): a round pill that rises in and fades on its own."""
        T = THEME
        lay = self.layout(cr, str(sh.spec.get("text") or ""), panel.MONO, 13)
        _, ext = lay.get_pixel_extents()
        w, h = ext.width + 38, ext.height + 12
        if "toast" not in sh.placed:
            at = sh.spec.get("at")
            if at:
                x, y = at[0] - w / 2, at[1] - h
            else:  # top centre, under any toast still showing there
                sx, sy, sw, shh = self.screen_for(None)
                with self.lock:
                    below = [s.placed["toast"] for s in self.shapes if s is not sh and s.kind == "toast" and s.fading_at is None
                             and "toast" in s.placed and not s.spec.get("at")]
                x, y = sx + (sw - w) / 2, max([sy + 44] + [b[1] + b[3] + 8 for b in below])
            sh.placed["toast"] = (x, y, w, h)
        x, y = sh.placed["toast"][:2]
        a = alpha * min(1.0, p * 2)
        y += (1 - min(1.0, p * 1.5)) * 6
        rounded(cr, x + 1, y + 3, w, h, h / 2)
        cr.set_source_rgba(0, 0, 0, T["shadow"] * a); cr.fill()
        rounded(cr, x, y, w, h, h / 2)
        cr.set_source_rgba(*panel.CARD2, 0.97 * a); cr.fill_preserve()
        cr.set_source_rgba(*T["border"], a); cr.set_line_width(1); cr.stroke()
        cr.new_sub_path()
        cr.arc(x + 14, y + h / 2, 3.4, 0, 2 * math.pi)
        cr.set_source_rgba(*T["accents"].get(sh.spec.get("color") or "green", T["accents"]["green"]), a); cr.fill()
        cr.move_to(x + 24 - ext.x, y + (h - ext.height) / 2 - ext.y)
        cr.set_source_rgba(*T["card_body"], a)
        PangoCairo.show_layout(cr, lay)

    def toast(self, text, at=None, color="green", ttl=TOAST_TTL):
        self.draw([{"kind": "toast", "text": text, "at": at, "color": color, "ttl": ttl}], replace=False)

    def panel_rect(self, sh):
        """Where a panel (or its tab) is on screen right now, window offset included."""
        dx, dy = sh.offset
        if sh.docked:
            x, y, w, h = self.pill_rect(sh)
        elif "panel" in sh.placed:
            x, y, w, h = sh.placed["panel"]
        else:
            return None
        return (x + dx, y + dy, w, h)

    def live_panels(self):
        """Panels on screen now, newest first."""
        with self.lock:
            return [s for s in reversed(self.shapes) if s.panel and s.fading_at is None and not s.away]

    def input_rects(self, win):
        g = win.geom
        if self.keys_for is not None and self.window_for(self.keys_for) is win:
            return [(0, 0, g.width, g.height)]   # a click anywhere else hands the keyboard back (see grab_keys)
        out = []
        for s in self.live_panels():
            r = self.panel_rect(s)
            if r:
                out.append((int(r[0] - g.x), int(r[1] - g.y), int(math.ceil(r[2])) + 2, int(math.ceil(r[3])) + 4))
        return out

    def set_input(self, win, rects):
        if rects == win.input_rects:
            return
        win.input_rects = rects
        gw = win.get_window()
        if gw:
            gw.input_shape_combine_region(cairo.Region([cairo.RectangleInt(*r) for r in rects]), 0, 0)

    def panel_at(self, x, y):
        """(shape, action) under a point in layout px: "pill" for a tab, else what panel.hit says."""
        for s in self.live_panels():
            r = self.panel_rect(s)
            if r and r[0] <= x <= r[0] + r[2] and r[1] <= y <= r[1] + r[3]:
                if s.docked:
                    return s, "pill"
                return s, s.panel.hit(x - r[0], y - r[1])
        return None, None

    def set_hover(self, sh, action):
        changed = False
        for s in self.live_panels():
            want = action if s is sh else None
            if s.panel.hover != want:
                s.panel.hover = want
                changed = True
        for w in self.windows:
            gw = w.get_window()
            if gw:
                gw.set_cursor(Gdk.Cursor.new_from_name(self.display, "pointer" if action else "default"))
        if changed:
            self.redraw()

    def on_motion(self, area, ev, win):
        if self.dragging:  # a scene's slider follows the pointer while the button is down
            sh, action = self.dragging
            r = self.panel_rect(sh)
            if r and sh.panel:
                self.apply(sh, sh.panel.drag_to(action, ev.x + win.geom.x - r[0], ev.y + win.geom.y - r[1]))
            return True
        sh, action = self.panel_at(ev.x + win.geom.x, ev.y + win.geom.y)
        self.set_hover(sh, action)
        return True

    def on_press(self, area, ev, win):
        if ev.button != 1 or ev.type != Gdk.EventType.BUTTON_PRESS:
            return True
        sh, action = self.panel_at(ev.x + win.geom.x, ev.y + win.geom.y)
        if self.keys_for is not None and sh is not self.keys_for:
            self.release_keys()   # a click outside the panel that has the keys: they're their again
        if sh is None:
            return True
        if action == "pill":
            self.undock(sh)
        elif action:
            effects = sh.panel.act(action)
            if "drag" in effects:
                r = self.panel_rect(sh)
                self.dragging = (sh, action)
                effects = [e for e in effects if e != "drag"] + sh.panel.drag_to(
                    action, ev.x + win.geom.x - r[0], ev.y + win.geom.y - r[1])
            self.apply(sh, effects)
        return True

    def on_release(self, area, ev, win):
        if self.dragging:
            sh = self.dragging[0]
            self.dragging = None
            if sh.panel and hasattr(sh.panel, "end_drag"):
                sh.panel.end_drag()
                self.redraw()
        return True

    def undock(self, sh):
        now = time.monotonic()
        px, py = self.pill_rect(sh)[:2]
        sh.docked, sh.dock_t, sh.slide = False, None, (px, py, now)
        sh.grace_until = now + 6
        self.kick()

    def apply(self, sh, effects):
        """What a click or key on a panel asks the overlay to do."""
        for e in effects:
            if e == "redraw":
                self.redraw()
            elif e == "close":
                sh.fading_at = time.monotonic()
                if self.keys_for is sh:
                    self.release_keys()
                self.kick()
            elif e == "dock":
                if self.keys_for is sh:
                    self.release_keys()
                sh.docked, sh.dock_t = True, time.monotonic()
                self.pill_rect(sh)
                self.kick()
            elif e == "focus":
                self.grab_keys(sh)
            elif e == "unfocus":
                self.release_keys()
            elif e == "paste":
                try:
                    txt = subprocess.run(["wl-paste", "-n"], env=self.env, capture_output=True, text=True, timeout=2).stdout
                except (OSError, subprocess.TimeoutExpired):
                    txt = ""
                sh.panel.paste(txt)
                self.redraw()
            elif isinstance(e, tuple) and e[0] == "result":
                self.post(sh, e[1])
            elif isinstance(e, tuple) and e[0] == "copy":
                self.copy(sh, e[1], e[2])

    def copy(self, sh, text, target):
        """Onto the Wayland clipboard (wl-copy keeps serving it after we return), then a tiny toast by the button."""
        try:
            p = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE, env=self.env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            p.stdin.write(text.encode()); p.stdin.close()
            threading.Thread(target=p.wait, daemon=True).start()
        except OSError as e:
            print(f"chalk: copy failed: {e}", file=sys.stderr)
            return self.toast("Couldn't copy (no wl-copy)", color="red")
        self.last_copy = (sh, target, time.monotonic())
        targets = sh.panel.targets
        label = f"Copied {targets.index(target) + 1} of {len(targets)}" if len(targets) > 1 else "Copied"
        r = self.panel_rect(sh)
        btn = next((b for b, act in sh.panel.snapshot()[3] if act == ("copy", target)), None)
        at = (r[0] + btn[0] + btn[2] / 2, r[1] + btn[1] - 4) if r and btn and not sh.docked else None
        self.toast(label, at=at)
        self.redraw()

    def copy_op(self, n=None):
        """Super+Ctrl+C: copy the newest panel's code; pressed again soon after, the next block."""
        cands = [s for s in self.live_panels() if s.panel.targets]
        if not cands:
            self.toast("Nothing to copy", color="yellow")
            return {"copied": False}
        sh = cands[0]
        ts = sh.panel.targets
        if n:
            t = ts[min(len(ts), max(1, int(n))) - 1]
        elif self.last_copy and self.last_copy[0] is sh and time.monotonic() - self.last_copy[2] < 6 and self.last_copy[1] in ts:
            t = ts[(ts.index(self.last_copy[1]) + 1) % len(ts)]
        else:
            t = ts[0]
        self.apply(sh, sh.panel.act(("copy", t)))
        return {"copied": True, "target": t if t == "all" else ts.index(t) + 1, "of": len(ts)}

    KEYS_IDLE = 90   # seconds without a key before the keyboard goes back to their window by itself

    def grab_keys(self, sh):
        """Give one panel the keyboard. It has to be exclusive (Sway drops an on-demand layer's focus the
        moment it switches over), so while it lasts the overlay also takes clicks everywhere on that screen:
        a click outside the panel hands the keyboard back. So do Esc, Super+; again, answering, and idling."""
        if self.keys_for is not None and self.keys_for is not sh:
            self.keys_for.panel.focused = False
            self.keys_for.panel.touch()
        first = self.keys_for is None
        self.keys_for, self.keys_t = sh, time.monotonic()
        sh.panel.focused = True
        sh.panel.touch()
        win = self.window_for(sh)
        for w in self.windows:
            GtkLayerShell.set_keyboard_mode(w, GtkLayerShell.KeyboardMode.EXCLUSIVE if w is win else GtkLayerShell.KeyboardMode.NONE)
        if first:
            GLib.timeout_add_seconds(5, self.keys_idle)
        self.redraw()

    def keys_idle(self):
        sh = self.keys_for
        if sh is None:
            return False
        if time.monotonic() - sh.panel.touched > self.KEYS_IDLE:
            self.release_keys()
            return False
        return True

    def release_keys(self):
        sh, self.keys_for = self.keys_for, None
        for w in self.windows:
            GtkLayerShell.set_keyboard_mode(w, GtkLayerShell.KeyboardMode.NONE)
        if sh is not None:
            sh.panel.focused = False
            sh.panel.touch()
        self.redraw()

    def window_for(self, sh):
        r = self.panel_rect(sh) or (0, 0, 1, 1)
        cx, cy = r[0] + r[2] / 2, r[1] + r[3] / 2
        for w in self.windows:
            g = w.geom
            if g.x <= cx < g.x + g.width and g.y <= cy < g.y + g.height:
                return w
        return self.windows[0]

    def on_key(self, win, ev):
        sh = self.keys_for
        if sh is None:
            return False
        name = Gdk.keyval_name(ev.keyval) or ""
        cp = Gdk.keyval_to_unicode(ev.keyval)
        ctrl = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(ev.state & Gdk.ModifierType.SHIFT_MASK)
        self.apply(sh, sh.panel.key(name, chr(cp) if cp else "", ctrl, shift))
        return True

    def on_focus_out(self, win, ev):
        # nothing to do: an exclusive surface only loses the keyboard to another one for a moment (rofi on
        # Super+/), and gets it back when that closes
        return False

    def focus_op(self):
        """Super+;: jump into the newest panel (the one waiting on them first), or back out of it."""
        if self.keys_for is not None:
            self.release_keys()
            return {"keys": False}
        with self.lock:
            cands = [s for s in reversed(self.shapes) if s.panel and s.fading_at is None and not s.away]
        cands.sort(key=lambda s: not s.panel.pending)   # stable: newest waiting one first
        if not cands:
            self.toast("Nothing to answer here", color="yellow")
            return {"keys": False}
        sh = cands[0]
        if sh.docked:
            self.undock(sh)
        self.grab_keys(sh)
        return {"keys": True, "tag": sh.spec.get("tag")}

    def post(self, sh, result):
        """A panel's result, for whoever waits on its tag (`chalk panel --wait`)."""
        if sh.reported:
            return
        sh.reported = True
        tag = sh.spec.get("tag")
        if not tag:
            return
        with self.cond:
            self.results[tag] = dict(result, tag=tag)
            self.cond.notify_all()

    def wait(self, tag, timeout):
        """Block (on a socket thread) until the panel with this tag reports, or the timeout."""
        with self.lock:
            alive = any(s.spec.get("tag") == tag and not s.reported for s in self.shapes)
        with self.cond:
            if tag not in self.results and not alive:
                return {"tag": tag, "status": "gone"}
            self.cond.wait_for(lambda: tag in self.results, timeout=timeout)
            return self.results.pop(tag, None) or {"tag": tag, "status": "waiting"}

    def redraw(self):
        """Repaint without telling the Tracker anything changed (hover, typing: all inside a panel)."""
        for w in self.windows:
            w.area.queue_draw()
        if not self.ticking:
            self.ticking = True
            GLib.timeout_add(16, self.tick)


def rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


# ---------- keeping up with the screen ----------

class Tracker(threading.Thread):
    """Watches Sway (workspace and window events) and, while anything is drawn, the screen itself
    (a small frame every PERIOD s, ~85 ms each), and tells the overlay what to hide, drop or move."""
    PERIOD = 0.7
    HEAT = 6          # polls a changed cell stays "where they're working" (~4 s)
    STALE = 0.3       # share of a mark's target (its filled cells) that has to change, twice in a row, to drop it

    def __init__(self, ov):
        super().__init__(daemon=True)
        self.ov = ov
        self.dirty = threading.Event()
        self.dirty.set()
        self.prev = self.heat = self.last = None

    def watch_sway(self):
        while True:
            try:
                p = subprocess.Popen(["swaymsg", "-m", "-r", "-t", "subscribe", '["workspace", "window"]'],
                                     env=self.ov.env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                for _ in p.stdout:
                    self.dirty.set()
            except OSError:
                pass
            time.sleep(5)

    def run(self):
        threading.Thread(target=self.watch_sway, daemon=True).start()
        while True:
            time.sleep(self.PERIOD)
            try:
                self.step()
            except Exception as e:  # never take the overlay down over this
                print(f"chalk track: {e!r}", file=sys.stderr)
                self.prev = None

    def step(self):
        import numpy as np
        ov = self.ov
        if DEBUG:
            print(f"chalk track: step dirty={self.dirty.is_set()} ticking={ov.ticking} prev={self.prev is not None} epoch={getattr(ov, 'epoch', 0)}", file=sys.stderr, flush=True)
        with ov.lock:
            live = [s for s in ov.shapes if s.fading_at is None and s.kind != "busy"]
        if not live:
            self.prev = self.heat = self.last = None
            return
        if self.dirty.is_set():
            self.dirty.clear()
            st = track.sway_state(ov.env)
            if st:
                # title changes (a terminal, a playing video's tab) fire events too; only a change in what's
                # shown where restarts the comparison
                sig = (sorted(st["visible"]), sorted((k, w["rect"]) for k, w in st["windows"].items() if w["visible"]))
                if sig != getattr(self, "sig", None):
                    self.sig = sig
                    GLib.idle_add(ov.apply_context, st)
                    self.prev = None
                    return
        epoch = getattr(ov, "epoch", 0)
        if ov.hidden or ov.ticking or epoch != getattr(self, "epoch", None):
            # its own drawing changed (maybe between two polls): the frame-to-frame comparison starts over, and
            # marks whose target a panel covered or uncovered look again; the others keep what they saw
            with ov.lock:
                shapes = [s for s in ov.shapes if s.fading_at is None]
                panels = [b for s in shapes if s.panel_key for b in ov.footprint(s)]
            moved = panels + getattr(self, "panels", [])
            self.panels = panels
            for s in shapes:
                t = ov.target_box(s)
                if t and any(track.overlap(t, b) for b in moved) and not s.panel_key:
                    s.base = None
            self.epoch, self.prev = epoch, None
            return
        with ov.lock:
            # every mark followed by the Follower and no card or diagram up: nothing here needs the screen
            idle = all(s.follow and not s.panel_key for s in ov.shapes if s.fading_at is None and s.kind != "busy")
        if idle:
            self.prev = None
            return
        rect = ov.screen()
        frame = track.grab(ov.env, rect)
        if self.dirty.is_set() or ov.ticking or getattr(ov, "epoch", 0) != epoch:
            self.prev = None
            return
        origin = rect[:2]
        now = time.monotonic()
        with ov.lock:
            live = [s for s in ov.shapes if s.fading_at is None and s.kind != "busy" and not s.away and not s.lost]
            prints = {id(s): ov.footprint(s) for s in live}
        detail = track.detail(frame)
        if self.prev is None or self.prev.shape != frame.shape:
            self.prev = frame
            if self.heat is None or self.heat.shape != detail.shape:
                self.heat = np.zeros(detail.shape, np.int8)
            self.last = (None, detail, self.heat, origin)
            return
        active = track.changed(frame, self.prev)
        self.prev = frame
        ours = np.zeros_like(active)  # what the overlay itself covers says nothing about their screen
        for boxes in prints.values():
            for b in boxes:
                sl = track.cells(b, ours.shape, origin, pad=4)
                if sl:
                    ours[sl] = True
        active &= ~ours
        self.heat = np.maximum(self.heat - 1, 0).astype(np.int8)
        self.heat[active] = self.HEAT
        self.last = (None, detail, self.heat, origin)
        # chalk's own strokes sit inside the things they mark and never change: leave them out
        ink = np.zeros(active.shape, bool)
        for s in live:
            key = (ov.shift(s), active.shape)
            if getattr(s, "ink_key", None) != key:
                s.ink_key, s.ink = key, track.ink_mask(s.strokes, active.shape, origin, key[0])
            ink |= s.ink
        stale, move, dock = [], [], []
        for s in live:
            if now < s.t0 + s.dur + 0.3 or s.docked:
                continue
            if s.panel and (s.panel.focused or s.panel.hover or now - s.panel.touched < 5):
                continue  # they're using it: it stays put
            dx, dy = ov.shift(s)
            # the thing it marks or explains: once that's changed (scrolled away, another tab), the mark goes
            # and a card or diagram about it tucks itself away as a tab
            t = ov.target_box(s)
            sl = track.cells((t[0] + dx, t[1] + dy, t[2], t[3]), active.shape, origin, pad=2) if t else None
            if sl and not s.follow:  # (a mark the Follower keeps on its target hides and comes back by itself)
                now_piece = track.piece(frame, sl)
                if s.base is None or s.base.shape != now_piece.shape:
                    s.base, s.strikes = now_piece.copy(), 0
                else:
                    frac = track.changed_share(now_piece, s.base, ink[sl])
                    if DEBUG:
                        print(f"chalk track: {s.kind} target {t} changed {frac:.2f}", file=sys.stderr, flush=True)
                    s.strikes = s.strikes + 1 if frac > self.STALE else 0
                    if s.strikes >= 2:
                        if DEBUG:
                            print(f"chalk track: {s.kind}: its target changed ({frac:.2f})", file=sys.stderr, flush=True)
                        (dock if s.panel_key else stale).append(s)
                        continue
            if not s.panel_key or now < s.grace_until:
                continue
            # a card or diagram right where they're working (typing next to it, scrolling the window under it)
            # moves to empty screen, or docks when there's none
            x, y, w, h = s.placed[s.panel_key]
            r = (x + dx, y + dy, w, h)
            ring = track.cells(r, active.shape, origin, pad=48)
            near = int(active[ring].sum()) if ring else 0
            big = False
            if s.ctx and s.ctx[2]:
                wx, wy, ww, wh = s.ctx[2]
                wsl = track.cells((wx + dx, wy + dy, ww, wh), active.shape, origin)
                big = bool(wsl) and float(active[wsl].mean()) > 0.25
            if near >= 4 or big:
                if DEBUG:
                    print(f"chalk track: {s.kind}: activity near={near} big={big}", file=sys.stderr, flush=True)
                others = [b for o in live if o is not s for b in prints[id(o)]]
                pos = track.place_panel(w, h, ov.screen_for(r), detail, self.heat, origin, avoid=others, cur=(x, y))
                (move.append((s, pos)) if pos else dock.append(s))
        if stale or move or dock:
            GLib.idle_add(ov.adapt, stale, move, dock)


def grid(scale):
    """The smallest step in layout px that's a whole number of physical px at this scale (1.5 -> 2)."""
    return next((k for k in range(1, 9) if abs(k * scale - round(k * scale)) < 1e-6), 1)


class Follower(threading.Thread):
    """Keeps marks on what they mark while it scrolls, in any app (follow.py has the matching).

    Frames come from one long-lived `chalk-cap serve`: one grab per output covering every mark's search area,
    answered only once something there was redrawn, so nothing runs while the screen is still. A mark whose
    target scrolls out of its window (or under a toolbar, or into another tab) fades out and comes back when
    the target does. The helper is only running while there are marks to follow."""
    WAIT = 400        # ms a grab waits for the screen to change before the mark list is checked again
    BAND_EVERY = 0    # ms between searches for a lost target (its whole column; ~2 ms with libchalkmatch)
    LOOK_AGE = 120    # s a `chalk look` screenshot stays the reference for marks drawn after it

    def __init__(self, ov):
        super().__init__(daemon=True)
        self.ov = ov
        self.wake = threading.Event()
        self.proc = None
        self.outputs = None
        self.look = (None, None)    # (mtime, (gray image, origin)) of the last look's screenshot

    # --- chalk-cap ---

    def helper(self):
        if self.proc and self.proc.poll() is None:
            return self.proc
        exe = os.environ.get("CHALK_CAP") or os.path.join(HERE, "cap", "chalk-cap")
        if not os.access(exe, os.X_OK):
            import shutil
            exe = shutil.which("chalk-cap")
        if not exe:
            return None
        self.proc = subprocess.Popen([exe, "serve"], env=self.ov.env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return self.proc

    def stop_helper(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None

    def grab(self, rect, damage, wait):
        """(gray frame, its time in ms, the rect it covers) for a layout rect inside one output, or None (nothing
        changed / failed). The rect grows to whole physical px (at 1.5x, to even layout px): a piece that
        started half a physical px off would average other px than `chalk look`'s picture and the other grabs."""
        import numpy as np
        out = next((o for o in self.outputs if track.inside(o[1], rect[0], rect[1])), None)
        p = self.helper()
        if out is None or p is None:
            return None
        name, (ox, oy, ow, oh), k = out
        x0, y0 = (int(rect[0]) - ox) // k * k, (int(rect[1]) - oy) // k * k
        x1, y1 = -(-(int(math.ceil(rect[0] + rect[2])) - ox) // k) * k, -(-(int(math.ceil(rect[1] + rect[3])) - oy) // k) * k
        x1, y1 = min(x1, ow), min(y1, oh)
        x, y, w, h = x0, y0, x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            return None
        p.stdin.write(f"{name} {x} {y} {w} {h} {int(damage)} {wait}\n".encode())
        p.stdin.flush()
        head = p.stdout.readline().split()
        if len(head) != 3:
            if not head:
                self.stop_helper()
            return None
        W, H, t = int(head[0]), int(head[1]), int(head[2])
        buf = p.stdout.read(W * H)
        return np.frombuffer(buf, np.uint8).reshape(H, W), t, (x + ox, y + oy, W, H)

    def load_outputs(self):
        r = subprocess.run(["swaymsg", "-r", "-t", "get_outputs"], env=self.ov.env, capture_output=True, text=True)
        try:
            self.outputs = [(o["name"], tuple(o["rect"][k] for k in ("x", "y", "width", "height")), grid(o.get("scale", 1)))
                            for o in json.loads(r.stdout) if o.get("active")]
        except ValueError:
            self.outputs = []

    # --- anchors ---

    def win(self, sh):
        """The window a mark belongs to, where it is now (layout px), clipped to its output."""
        x, y, w, h = sh.ctx[2]
        x, y = x + sh.offset[0], y + sh.offset[1]
        for _, (ox, oy, ow, oh), _ in self.outputs:
            if track.inside((ox, oy, ow, oh), x + w / 2, y + h / 2):
                x0, y0, x1, y1 = max(x, ox), max(y, oy), min(x + w, ox + ow), min(y + h, oy + oh)
                return int(x0), int(y0), int(x1 - x0), int(y1 - y0)
        return None

    def look_image(self, born):
        """chalk look's screenshot as a gray layout-px image and its origin, if it was taken shortly before
        `born` (wall time): it's the screen exactly as Claude saw it, with no ink on it."""
        import numpy as np
        state = chalkconf.state_dir()
        try:
            meta = json.load(open(os.path.join(state, "look.json")))
            mt = os.path.getmtime(meta["shot"])
        except (OSError, ValueError, KeyError):
            return None
        if not 0 <= born - meta.get("time", 0) <= self.LOOK_AGE:
            return None
        gray = os.path.join(state, "look-gray.npy")  # filtered like chalk-cap's frames (chalk save_gray)
        try:
            gt = os.path.getmtime(gray)
        except OSError:
            return None
        if gt < mt - 1:
            return None
        if self.look[0] != gt:
            try:
                self.look = (gt, (np.load(gray), tuple(meta.get("origin", (0, 0)))))
            except (OSError, ValueError):
                return None
        return self.look[1]

    def ink_mask(self, rect, also=(), t=None):
        """Where chalk itself paints inside a layout rect (bool, True = don't look there): every live shape's
        strokes, labels, cards and tabs where they're drawn now, or for a frame shown at time t (ms), where
        they were painted then (the last paint before it, or the one just after if that's still on its way)."""
        import numpy as np
        x, y, w, h = rect
        m = np.zeros((h, w), bool)
        ov = self.ov
        with ov.lock:
            shapes = [s for s in ov.shapes if s.fading_at is None and not s.away]
        for s in shapes:
            shifts = {sh for o, sh in also if o is s}
            log = getattr(s, "painted", None)
            if t is None or not log:
                shifts.add(ov.shift(s))
            else:
                i = max([k for k, (pt, _) in enumerate(log) if pt <= t - 4] or [0])
                shifts |= {d for _, d in log[i:i + 2]}
            for dx, dy in shifts:
                if s.strokes:
                    bm = getattr(s, "ink_bm", None)
                    if bm is None:  # the strokes as a bitmap, made once
                        bx, by, bw, bh = s.ink_box
                        bm = np.zeros((int(bh) + 16, int(bw) + 16), bool)
                        for st in s.strokes:
                            follow.stamp(bm, st, s.width / 2 + 4, bx - 8, by - 8)
                        s.ink_bm = bm = (int(bx) - 8, int(by) - 8, bm)
                    bx, by, b = bm
                    px, py = int(round(bx + dx - x)), int(round(by + dy - y))
                    y0, x0 = max(0, py), max(0, px)
                    y1, x1 = min(h, py + b.shape[0]), min(w, px + b.shape[1])
                    if y0 < y1 and x0 < x1:
                        m[y0:y1, x0:x1] |= b[y0 - py:y1 - py, x0 - px:x1 - px]
                for k, r in s.placed.items():
                    if not (s.docked and k == s.panel_key):
                        follow.fill(m, (r[0] + dx, r[1] + dy, r[2], r[3]), x, y, pad=6)
                if s.label and "label" in s.placed and s.kind != "label":
                    tgt = s.ink_box if s.kind != "arrow" else s.anchor
                    ln = layout.leader(s.placed["label"], tgt) if tgt else None
                    if ln:
                        follow.stamp(m, [(ln[0][0] + dx, ln[0][1] + dy), (ln[1][0] + dx, ln[1][1] + dy)], 3, x, y)
                if s.kind == "point":
                    px0, py0 = s.point
                    follow.fill(m, (px0 - 12 + dx, py0 - 12 + dy, 44, 44), x, y)
                if s.kind == "sketch" and not s.sketch["board"] and s.anchor:
                    follow.fill(m, tuple(s.anchor[:2]) + (s.sketch["w"], s.sketch["h"]), x, y, pad=8)
            if s.docked and s.panel_key:
                follow.fill(m, self.ov.pill_rect(s), x, y, pad=4)
        return m

    def make_anchor(self, s):
        """Picture what a new mark points at: from the look Claude drew it for when there's a recent one
        (then also find where that has scrolled to since), else from the screen right now."""
        import numpy as np
        ov = self.ov
        box, win = ov.target_box(s), self.win(s) if s.ctx and s.ctx[2] else None
        if not box or not win:
            s.follow = False
            return None
        dx, dy = ov.shift(s)
        box = (box[0] + dx, box[1] + dy, box[2], box[3])
        rect = follow.template_rect(box, win)
        if rect is None:
            s.follow = False
            return None
        x, y, w, h = rect
        a = None
        dx2 = dy2 = 0
        look = self.look_image(s.born)
        if look is not None:
            img, (lx, ly) = look
            piece = img[y - ly:y - ly + h, x - lx:x - lx + w]
            if piece.shape == (h, w):
                # the screen as Claude saw it, without any ink: the cleanest picture of the target
                a = follow.Anchor(piece, np.ones((h, w), bool), rect)
                # the user may have scrolled while Claude was thinking: find it on the screen as it is now
                band = self.clip((x - follow.REACH // 2 - 10, win[1], w + follow.REACH + 20, win[3]), win)
                got = self.grab(band, False, 0) if band and a.usable else None
                r = None
                if got:
                    frame, t, band = got
                    ok = ~self.ink_mask(band)
                    r = a.near(frame, ok, band[0], band[1], t)
                    if r is None or r[2] > follow.GOOD:
                        r = a.band(frame, ok, band[0], band[1], x)
                if r:
                    a.fix(r[0], r[1], r[2], t)
                    dx2, dy2 = r[0] - x, r[1] - y
                    a.rect0 = (r[0], r[1], w, h)  # (shift() counts from where it is now)
                else:
                    a = None  # not on screen as it was: trust the coordinates and picture it as it is
        if a is None:
            got = self.grab((x, y, w, h), False, 0)
            if not got:
                return None
            img, t, rect = got
            ok = ~self.ink_mask(rect)
            a = follow.Anchor(img, ok, rect, smoothed=True)
            a.fix(rect[0], rect[1], 0.0, t)
        moved = (dx2, dy2) if (dx2, dy2) != (0, 0) else None
        if DUMP:
            np.savez_compressed(os.path.join(DUMP, f"anchor-{id(s)}.npz"), rect=np.array(rect), t=t,
                                moved=np.array(moved or (0, 0)), py=a.py, px=a.px, val=a.val, tau=a.tau,
                                contrast=a.contrast, rect0=np.array(a.rect0))
        a.win_off, a.base = s.offset, (dx + dx2, dy + dy2)
        s.follow = a if a.usable else False
        if DEBUG:
            print(f"chalk follow: {s.kind} {box} anchor {rect} usable={a.usable} pts={len(a.py)} moved={moved}",
                  file=sys.stderr, flush=True)
        if s.follow and moved:
            GLib.idle_add(self.ov.follow_update, [(s, a.total(), False)])
        return a

    @staticmethod
    def clip(r, c):
        x0, y0 = max(r[0], c[0]), max(r[1], c[1])
        x1, y1 = min(r[0] + r[2], c[0] + c[2]), min(r[1] + r[3], c[1] + c[3])
        return (int(x0), int(y0), int(x1 - x0), int(y1 - y0)) if x1 - x0 >= 8 and y1 - y0 >= 8 else None

    # --- the loop ---

    def run(self):
        while True:
            self.wake.wait()
            self.wake.clear()
            try:
                while self.step():
                    pass
            except Exception as e:  # never take the overlay down over this
                import traceback
                print(f"chalk follow: {e!r}", file=sys.stderr)
                if DEBUG:
                    traceback.print_exc()
                self.stop_helper()
                time.sleep(1)
                self.wake.set()

    def step(self):
        """One frame for every followed mark. False when there's nothing to follow (the thread sleeps)."""
        import numpy as np
        ov = self.ov
        with ov.lock:
            marks = [s for s in ov.shapes if s.fading_at is None and s.kind in FOLLOW_KINDS and s.follow is not False
                     and not s.away and s.ctx and s.ctx[1]]
        if not marks:
            self.stop_helper()
            return False
        if ov.hidden:
            time.sleep(0.05)
            return True
        if self.outputs is None or self.wake.is_set():
            self.wake.clear()
            self.load_outputs()
        for s in marks:
            if s.follow is None:
                if not hasattr(s, "born"):
                    s.born = time.time()
                self.make_anchor(s)
        marks = [s for s in marks if s.follow]
        if not marks:
            self.stop_helper()
            return False
        now = time.monotonic() * 1000
        areas = []
        for s in marks:
            a, win = s.follow, self.win(s)
            if win is None:
                continue
            if s.offset != a.win_off:  # its window moved: so did the target
                a.x += s.offset[0] - a.win_off[0]
                a.y += s.offset[1] - a.win_off[1]
                a.win_off = s.offset
            w, h = a.size
            side = follow.REACH // 2 + 10
            column = a.lost or a.misses > 0 or getattr(a, "recheck", 0) > 0
            if column:  # its whole column of the window: after a jump too big to follow, or to spot it coming back
                if a.lost and now - getattr(a, "band_t", 0) < self.BAND_EVERY:
                    continue
                a.band_t = now
                area = (a.x - side, win[1], w + 2 * side, win[3])
            else:
                rx0, ry0, rx1, ry1 = a.reach(now)
                area = (rx0 - side, ry0 - 2, rx1 - rx0 + w + 2 * side, ry1 - ry0 + h + 4)
            area = self.clip(area, win)
            if area:
                areas.append((s, area, win, column))
        if not areas:  # only lost marks, searched for a moment ago
            time.sleep(self.BAND_EVERY / 1000)
            return True
        x0 = min(a[0] for _, a, _, _ in areas); y0 = min(a[1] for _, a, _, _ in areas)
        x1 = max(a[0] + a[2] for _, a, _, _ in areas); y1 = max(a[1] + a[3] for _, a, _, _ in areas)
        got = self.grab((x0, y0, x1 - x0, y1 - y0), True, self.WAIT)
        if got is None:
            return True
        img, t, rect = got
        t_got = time.monotonic() * 1000
        frame = img
        x0, y0, x1, y1 = rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]
        free = ~self.ink_mask(rect, t=t)
        updates = []
        for s, area, win, column in areas:
            a = s.follow
            ok = np.zeros_like(free)  # only its own window counts (the rest is other windows, bars)
            wx0, wy0 = max(0, win[0] - x0), max(0, win[1] - y0)
            ok[wy0:win[1] + win[3] - y0, wx0:win[0] + win[2] - x0] = True
            ok &= free
            before = (a.x, a.y, a.lost, a.misses)
            what = follow.track(a, frame, ok, x0, y0, t, column)
            if DUMP:  # what the follower saw and decided, for replaying it offline
                n = getattr(self, "dumped", 0)
                self.dumped = n + 1
                np.savez_compressed(os.path.join(DUMP, f"f{n:05d}.npz"), img=img, ok=ok, x0=x0, y0=y0, t=t,
                                    before=np.array(before, float), after=np.array((a.x, a.y, a.lost, a.misses), float),
                                    column=column, what=str(what), shown=np.array(ov.shift(s), float),
                                    got_ms=t_got - t, done_ms=time.monotonic() * 1000 - t)
            if what in ("moved", "found"):
                updates.append((s, a.total(), False))
            elif what == "lost":
                updates.append((s, None, True))
            if DEBUG and what in ("found", "lost"):
                print(f"chalk follow: {s.kind} {what} at {a.total()}", file=sys.stderr, flush=True)
        if updates:
            GLib.idle_add(ov.follow_update, updates)
        return True


# ---------- socket server ----------

def serve(ov):
    if os.path.exists(SOCK):
        try:
            probe = socket.socket(socket.AF_UNIX); probe.connect(SOCK); probe.close()
            print("chalk: already running", file=sys.stderr)
            os._exit(0)
        except OSError:
            os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)
    srv.listen(8)

    def handle(conn):
        with conn:
            f = conn.makefile("rwb")
            for line in f:
                try:
                    msg = json.loads(line)
                except ValueError:
                    f.write(b'{"ok":false,"error":"bad json"}\n'); f.flush(); continue
                if msg.get("op") == "wait":  # blocks this connection's thread, not the GTK one
                    r = ov.wait(msg.get("tag"), min(60.0, float(msg.get("timeout", 30))))
                    f.write((json.dumps({"ok": True, **r}) + "\n").encode()); f.flush()
                    continue
                done = threading.Event()
                box = {}

                def run():
                    op = msg.get("op")
                    if op == "draw":
                        box["r"] = {"ok": True, "ids": ov.draw(msg.get("shapes", []), msg.get("replace", True), msg.get("busy"), msg.get("seed"))}
                    elif op == "clear":
                        ov.clear(msg.get("all", True)); box["r"] = {"ok": True}
                    elif op == "remove":
                        ov.remove(set(msg.get("tags", []))); box["r"] = {"ok": True}
                    elif op in ("hide", "show"):
                        # hidden while chalk takes a screenshot, so old ink never lands in the next look
                        ov.hidden = op == "hide"; ov.kick(); box["r"] = {"ok": True}
                    elif op == "stats":
                        box["r"] = {"ok": True, **ov.stats, "shapes": len(ov.shapes), "screen": ov.screen(),
                                    "animating": ov.ticking, **ov.counts()}
                    elif op == "tags":
                        box["r"] = {"ok": True, "tags": ov.tags()}
                    elif op == "peek":
                        box["r"] = {"ok": True, **ov.peek()}
                    elif op == "focus":
                        box["r"] = {"ok": True, **ov.focus_op()}
                    elif op == "copy":
                        box["r"] = {"ok": True, **ov.copy_op(msg.get("n"))}
                    elif op == "toast":
                        ov.toast(str(msg.get("text", "")), msg.get("at"), msg.get("color") or "green"); box["r"] = {"ok": True}
                    elif op == "panel_act":  # tests: what a click on that target does, without a pointer
                        sh = next((s for s in ov.live_panels() if s.spec.get("tag") == msg.get("tag")), None)
                        if sh is None:
                            box["r"] = {"ok": False, "error": "no such panel"}
                        else:
                            a = msg.get("action")
                            ov.apply(sh, sh.panel.act(tuple(a) if isinstance(a, list) else a))
                            box["r"] = {"ok": True}
                    elif op == "panel_info":  # tests: where a panel's targets are on screen
                        out = []
                        for s in ov.live_panels():
                            r = ov.panel_rect(s)
                            hits = [] if s.docked else [[[round(r[0] + b[0]), round(r[1] + b[1]), round(b[2]), round(b[3])], a]
                                                         for b, a in s.panel.snapshot()[3]]
                            out.append({"tag": s.spec.get("tag"), "rect": [round(v) for v in r] if r else None, "docked": s.docked,
                                        "page": s.panel.page, "inputs": s.panel.inputs, "answers": s.panel.answers,
                                        "status": s.panel.status, "focused": s.panel.focused, "hits": hits,
                                        "values": getattr(s.panel, "values", None), "step": getattr(s.panel, "step", None),
                                        "objects": len(getattr(s.panel, "objects", None) or [])})
                        box["r"] = {"ok": True, "panels": out, "keys": ov.keys_for is not None}
                    elif op == "ping":
                        box["r"] = {"ok": True, "pid": os.getpid()}
                    elif op == "quit":
                        box["r"] = {"ok": True}; GLib.idle_add(Gtk.main_quit)
                    else:
                        box["r"] = {"ok": False, "error": f"unknown op {op!r}"}
                    done.set()
                    return False
                GLib.idle_add(run)
                done.wait(5)
                f.write((json.dumps(box.get("r", {"ok": False, "error": "timeout"})) + "\n").encode()); f.flush()

    def loop():
        while True:
            c, _ = srv.accept()
            threading.Thread(target=handle, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()


def main():
    load_font()
    ov = Overlay()
    serve(ov)
    ov.tracker.start()
    if ov.follower:
        ov.follower.start()
    try:
        Gtk.main()
    finally:
        try:
            os.unlink(SOCK)
        except OSError:
            pass


if __name__ == "__main__":
    main()
