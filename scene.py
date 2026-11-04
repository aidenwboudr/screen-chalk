"""Scene boards: a diagram that moves and that the user can poke, in the spirit of 3Blue1Brown's Manim.

The model describes a small scene as data (objects in canvas units, named parameters, steps); this module
animates it in (strokes draw themselves, formulas are written glyph by glyph, things fade or grow), runs
anything that depends on time `t` (a wave travelling, dots flowing round a circuit), and turns the parameters
into sliders and toggles the user can drag: every expression is re-evaluated, so curves, dots, bars and numbers
follow the knob. Steps add objects one at a time behind a Next button, each with a one-line caption.
Formulas go through MathJax (tex/texd.js), so they are real TeX, drawn as outlines.

A scene is a kind of panel: the overlay places it, tucks it, follows its window and sends it clicks and
drags exactly as it does a panel (`panel.Panel`'s interface). Nothing here touches GTK windows.

Spec ({"kind": "panel", "scene": {...}} or {"kind": "scene", ...}):
  {"title": "...", "size": [w, h] canvas units, "theme": "dark|paper",
   "params": [{"name": "f", "label": "input frequency", "min": 10, "max": 1e5, "value": 1000, "log": true,
               "unit": "Hz"} | {"name": "on", "label": "capacitor", "toggle": true, "value": 1}],
   "let": {"fc": "1/(2*pi*R*C)", "g": "1/sqrt(1+(f/fc)^2)"},   derived values, in order
   "objects": [ {"id": "ax", "axes": [x, y, w, h], "x": [10, 1e5], "y": [0, 1.1], "xlog": true,
                 "xlabel": "f", "ylabel": "gain", "xticks": [...], "yticks": [...]},
                {"plot": "1/sqrt(1+(x/fc)^2)", "on": "ax"},  {"dot": ["f", "g"], "on": "ax", "label": "..."},
                {"line"|"arrow": [x1, y1, x2, y2]}, {"rect": [x, y, w, h]}, {"circle": [cx, cy, r]},
                {"path": [[x, y], ...], "smooth": true, "closed": true},
                {"tex": "V_{out} = {g:.2f}\\,V_{in}", "at": [x, y], "size": 22, "align": "center"},
                {"text": "...", "at": [x, y]},
                {"resistor"|"capacitor"|"inductor"|"source"|"diode": [x1, y1, x2, y2], "label": "R"},
                {"wire": [[x, y], ...]}, {"ground": [x, y]},
                {"flow": [[x, y], ...], "speed": "expr", "n": 6}  dots moving along a path
              ]  every number may be an expression string over params, let-values, t (seconds) and,
                 in a plot, x. Common keys: id, color, width, fill (colour or true), opacity, dash,
                 enter ("draw"|"write"|"fade"|"grow"|"none"), show (expression: hidden when 0).
   "steps": [{"show": ["ax", "curve"], "say": "caption", "focus": ["dot"]}, ...]}
Objects no step names appear with the first step.
"""
import ast, json, math, os, re, subprocess, threading, time

import cairo
import gi
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo

import panel

HERE = os.path.dirname(os.path.realpath(__file__))
clock = time.monotonic          # the bench swaps this for a virtual clock
MAX_W, MAX_H = 600, 400         # the canvas is shown at most this big (layout px)
PAD = 18
ENTER = 0.75                    # seconds one object takes to come in
LAG = 0.32                      # and the next one starts this much later
TIME_RUN = 25.0                 # time-driven objects keep moving this long after the last touch
ST_RUN = 8.0                    # objects driven by st (seconds into the step) move for this long after a step starts

THEMES = {
    # Manim's defaults: near-black ground, the 3b1b blue/yellow/teal family, white text
    "dark": {"bg": (0.110, 0.110, 0.118), "edge": (0.26, 0.26, 0.28), "ink": (0.93, 0.93, 0.93),
             "dim": (0.60, 0.60, 0.62), "header": (0.15, 0.15, 0.16), "track": (0.32, 0.32, 0.35),
             "colors": {"blue": "#58C4DD", "yellow": "#FFFF00", "teal": "#5CD0B3", "green": "#83C167",
                        "red": "#FC6255", "gold": "#F0AC5F", "orange": "#FF862F", "purple": "#9A72AC",
                        "pink": "#D147BD", "grey": "#888888", "gray": "#888888", "white": "#EEEEEE"}},
    "paper": {"bg": (0.985, 0.972, 0.94), "edge": (0.83, 0.79, 0.71), "ink": (0.15, 0.15, 0.17),
              "dim": (0.45, 0.44, 0.42), "header": (0.93, 0.91, 0.86), "track": (0.78, 0.75, 0.68),
              "colors": {"blue": "#2E66D1", "yellow": "#CC8C00", "teal": "#11867A", "green": "#218C4D",
                         "red": "#D44538", "gold": "#B8741A", "orange": "#E0731A", "purple": "#7A4DC7",
                         "pink": "#CC408C", "grey": "#77736C", "gray": "#77736C", "white": "#26262B"}},
}
TEXT_FONT = "CMU Serif, Latin Modern Roman, DejaVu Serif, serif"


def hex_rgb(s):
    s = s.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return tuple(int(s[i:i + 2], 16) / 255 for i in (0, 2, 4))


# --- expressions ---

FUNCS = {"sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos,
         "atan": math.atan, "atan2": math.atan2, "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
         "exp": math.exp, "log": math.log, "ln": math.log, "log10": math.log10, "log2": math.log2,
         "sqrt": math.sqrt, "abs": abs, "min": min, "max": max, "floor": math.floor, "ceil": math.ceil,
         "round": round, "sign": lambda v: (v > 0) - (v < 0), "hypot": math.hypot,
         "clip": lambda v, a, b: max(a, min(b, v)), "step": lambda v: 1.0 if v >= 0 else 0.0,
         "mod": lambda a, b: a % b, "deg": math.degrees, "rad": math.radians,
         "square": lambda v: 1.0 if (v % 1.0) < 0.5 else -1.0, "saw": lambda v: 2 * (v % 1.0) - 1,
         "ease": lambda v: (lambda p: p * p * (3 - 2 * p))(max(0.0, min(1.0, v))),
         "lerp": lambda a, b, f: a + (b - a) * max(0.0, min(1.0, f))}
CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}
_OK = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.Name,
       ast.Load, ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub,
       ast.UAdd, ast.Not, ast.And, ast.Or, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)
_CACHE = {}
_GLOBALS = {"__builtins__": {}, **FUNCS}


def compile_expr(src):
    """'1/sqrt(1+(f/fc)^2)' -> (fn(env) -> float, names it reads). Only arithmetic, comparisons, the
    functions above and names; anything else is refused (the model writes these)."""
    if isinstance(src, (int, float)):
        v = float(src)
        return (lambda env: v), frozenset()
    if src in _CACHE:
        return _CACHE[src]
    text = str(src).replace("^", "**").replace("×", "*").replace("·", "*").replace("−", "-")
    tree = ast.parse(text, mode="eval")
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, _OK):
            raise ValueError(f"not allowed in an expression: {type(node).__name__}")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in FUNCS):
            raise ValueError("unknown function")
        if isinstance(node, ast.Name) and node.id not in FUNCS:
            names.add(node.id)
    code = compile(tree, "<scene>", "eval")

    def fn(env):
        try:
            v = eval(code, _GLOBALS, env)
            v = float(v)
            return v if math.isfinite(v) else float("nan")
        except (ArithmeticError, ValueError, TypeError, NameError, OverflowError):
            return float("nan")
    _CACHE[src] = (fn, frozenset(names))
    return _CACHE[src]


_FMT = re.compile(r"\{([^{}:]+?)(?::([^{}]*))?\}")


def interpolate(s, env, tex=False):
    """'gain {g:.2f}' -> 'gain 0.71'. In TeX only {name} or {name:fmt} with a known name counts, so
    TeX's own braces are left alone."""
    def sub(m):
        expr, fmt = m[1].strip(), m[2]
        if tex and not fmt and expr not in env:
            return m[0]
        try:
            fn, names = compile_expr(expr)
        except (SyntaxError, ValueError):
            return m[0]
        if not names <= set(env):
            return m[0]
        v = fn(env)
        out = nice(v)
        if fmt:
            try:
                out = format(v, fmt)
            except ValueError:
                pass
        return "{%s}" % out if tex else out   # in TeX the braces it replaced may be \frac's argument: keep a group
    return _FMT.sub(sub, str(s))


PREFIX = {"G": 1e9, "M": 1e6, "k": 1e3, "m": 1e-3, "µ": 1e-6, "u": 1e-6, "n": 1e-9, "p": 1e-12}


SI = ("Hz", "V", "A", "s", "Ω", "F", "H", "W", "m", "g", "B", "b", "bps", "Hz/s", "V/s", "V/µs")


def with_unit(v, unit):
    """A readout in engineering form: 1500 'kHz' -> '1.5 MHz' (not '1.5k kHz'), 0.002 's' -> '2 ms'. Units
    that aren't SI ('%', 'min', 'pts') are left alone."""
    unit = (unit or "").strip()
    if len(unit) > 1 and unit[0] in PREFIX and unit[1:] in SI:
        v, unit = v * PREFIX[unit[0]], unit[1:]
    if unit not in SI or not math.isfinite(v) or v == 0:
        return f"{nice(v)} {unit}".strip()
    for k, pre in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
        if abs(v) >= k * 0.9995:
            return f"{v / k:.3g} {pre}{unit}"
    return f"{v / 1e-12:.3g} p{unit}"


def nice(v):
    """A number the way you'd write it on a board: 995, 0.707, 1.2k, 47n."""
    if not math.isfinite(v):
        return "–"
    a = abs(v)
    if a >= 1e3 or (0 < a < 1e-3):
        for k, suf in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
            if a >= k:
                return f"{v / k:.3g}{suf}"
    return f"{v:.3g}"


# --- TeX through MathJax ---

class Tex:
    """A long-running node texd.js; replies cached by source. Unavailable (no node, no mathjax) -> None."""

    def __init__(self):
        self.proc, self.lock, self.cache, self.dead = None, threading.Lock(), {}, False

    def start(self):
        js = os.path.join(HERE, "tex", "texd.js")
        if self.dead or not os.path.isdir(os.path.join(HERE, "tex", "node_modules")):
            self.dead = True
            return None
        try:
            self.proc = subprocess.Popen(["node", js], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError:
            self.dead = True
        return self.proc

    def get_many(self, sources):
        """Look several up at once (pipelined): {source: parsed or None}."""
        want = [s for s in dict.fromkeys(sources) if s not in self.cache]
        if want:
            with self.lock:
                if (self.proc is None or self.proc.poll() is not None) and not self.start():
                    return {s: None for s in sources}
                try:
                    for s in want:
                        self.proc.stdin.write(json.dumps({"tex": s}) + "\n")
                    self.proc.stdin.flush()
                    for s in want:
                        self.cache[s] = parse_tex(json.loads(self.proc.stdout.readline()))
                except (OSError, ValueError):
                    self.proc = None
                    for s in want:
                        self.cache.setdefault(s, None)
        return {s: self.cache.get(s) for s in sources}

    def get(self, source):
        return self.get_many([source])[source]


TEX = Tex()


def svg_path(cr, d):
    """Trace SVG path data onto cr (the subset MathJax writes: M L H V Q T C S Z, absolute or relative)."""
    toks = re.findall(r"[MmLlHhVvQqTtCcSsZz]|-?(?:\d+\.?\d*|\.\d+)(?:e-?\d+)?", d)
    i, cmd = 0, None
    x = y = sx = sy = 0.0
    qx = qy = None      # last quadratic control point (for T)
    cx2 = cy2 = None    # last cubic second control point (for S)

    def num():
        nonlocal i
        v = float(toks[i]); i += 1
        return v

    while i < len(toks):
        if toks[i].isalpha():
            cmd = toks[i]; i += 1
            if cmd in "Zz":
                cr.close_path(); x, y = sx, sy; qx = cx2 = None
                continue
        rel = cmd.islower()
        c = cmd.upper()
        ox, oy = (x, y) if rel else (0.0, 0.0)
        if c == "M":
            x, y = ox + num(), oy + num(); sx, sy = x, y
            cr.move_to(x, y); cmd = "l" if rel else "L"; qx = cx2 = None
        elif c == "L":
            x, y = ox + num(), oy + num(); cr.line_to(x, y); qx = cx2 = None
        elif c == "H":
            x = ox + num(); cr.line_to(x, y); qx = cx2 = None
        elif c == "V":
            y = (y if rel else 0.0) + num(); cr.line_to(x, y); qx = cx2 = None
        elif c in "QT":
            if c == "Q":
                px, py = ox + num(), oy + num()
            else:
                px, py = (2 * x - qx, 2 * y - qy) if qx is not None else (x, y)
            ex, ey = ox + num(), oy + num()
            cr.curve_to(x + 2 / 3 * (px - x), y + 2 / 3 * (py - y), ex + 2 / 3 * (px - ex), ey + 2 / 3 * (py - ey), ex, ey)
            qx, qy, x, y, cx2 = px, py, ex, ey, None
        elif c in "CS":
            if c == "C":
                ax, ay = ox + num(), oy + num()
            else:
                ax, ay = (2 * x - cx2, 2 * y - cy2) if cx2 is not None else (x, y)
            bx, by, ex, ey = ox + num(), oy + num(), ox + num(), oy + num()
            cr.curve_to(ax, ay, bx, by, ex, ey)
            cx2, cy2, x, y, qx = bx, by, ex, ey, None
        else:
            i += 1


_SCRATCH = None


def scratch():
    global _SCRATCH
    if _SCRATCH is None:
        _SCRATCH = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 4, 4))
    return _SCRATCH


def flat_len(cr):
    """Length of cr's current path, flattened."""
    total, px, py, sx, sy = 0.0, 0.0, 0.0, 0.0, 0.0
    for kind, pts in cr.copy_path_flat():
        if kind == cairo.PATH_MOVE_TO:
            px, py = sx, sy = pts
        elif kind == cairo.PATH_LINE_TO:
            total += math.hypot(pts[0] - px, pts[1] - py); px, py = pts
        elif kind == cairo.PATH_CLOSE_PATH:
            total += math.hypot(sx - px, sy - py); px, py = sx, sy
    return total


def parse_tex(r):
    """texd's reply -> {"w", "h", "y0", "glyphs": [(cairo.Path, matrix, color, length)]} in TeX units."""
    if not r or r.get("error") or not r.get("items"):
        return None
    cr = scratch()
    glyphs = []
    for it in r["items"]:
        cr.new_path()
        cr.save()
        cr.transform(cairo.Matrix(*it["m"]))
        if "rect" in it:
            x, y, w, h = it["rect"]
            cr.rectangle(x, y, w, h)
        else:
            svg_path(cr, it["d"])
        cr.restore()
        # the path was traced under the glyph's matrix, so it is already in TeX units
        path = cr.copy_path()
        glyphs.append((path, it.get("color"), flat_len(cr)))
    cr.new_path()
    return {"w": r["w"], "h": r["h"], "y0": r["y0"], "glyphs": glyphs}


# --- geometry helpers ---

def ease(p):
    """Manim's 'smooth': a gentle start and a gentle landing."""
    p = max(0.0, min(1.0, p))
    return p * p * (3 - 2 * p)


def poly_len(pts):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))


def poly_prefix(pts, frac):
    """The first `frac` of a polyline (by length)."""
    if frac >= 1 or len(pts) < 2:
        return pts
    goal = poly_len(pts) * max(0.0, frac)
    out, run = [pts[0]], 0.0
    for a, b in zip(pts, pts[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if run + seg >= goal:
            k = (goal - run) / seg if seg else 0
            out.append((a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k))
            return out
        out.append(b)
        run += seg
    return out


def point_at(pts, dist):
    """The point `dist` along a polyline, wrapping round."""
    total = poly_len(pts)
    if total <= 0:
        return pts[0]
    dist %= total
    for a, b in zip(pts, pts[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if dist <= seg:
            k = dist / seg if seg else 0
            return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)
        dist -= seg
    return pts[-1]


def catmull(pts, n=10):
    if len(pts) < 3:
        return pts
    out = []
    ext = [pts[0]] + list(pts) + [pts[-1]]
    for i in range(1, len(ext) - 2):
        p0, p1, p2, p3 = ext[i - 1], ext[i], ext[i + 1], ext[i + 2]
        for s in range(n):
            t = s / n
            t2, t3 = t * t, t * t * t
            out.append(tuple(0.5 * (2 * p1[k] + (-p0[k] + p2[k]) * t + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
                                    + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3) for k in (0, 1)))
    out.append(tuple(pts[-1]))
    return out


def nice_ticks(lo, hi, log=False, n=5):
    if log:
        if lo <= 0 or hi <= 0:
            return []
        a, b = math.floor(math.log10(lo) + 1e-9), math.ceil(math.log10(hi) - 1e-9)
        step = max(1, (b - a) // 6 + 1)
        return [10.0 ** k for k in range(a, b + 1, step) if lo * 0.999 <= 10.0 ** k <= hi * 1.001]
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=mag * 10)
    start = math.ceil(lo / step - 1e-9) * step
    out, v = [], start
    while v <= hi + step * 1e-6:
        out.append(round(v, 12))
        v += step
    return out


# --- the scene ---

KINDS = ("axes", "plot", "dot", "line", "arrow", "rect", "circle", "path", "tex", "text", "resistor", "capacitor",
         "inductor", "source", "diode", "wire", "ground", "flow", "bar", "gate", "timing", "table", "cells")


def kind_of(o):
    return next((k for k in KINDS if k in o), None)


class Scene(panel.Panel):
    def __init__(self, spec):
        sc = spec.get("scene") if isinstance(spec.get("scene"), dict) else spec
        self.spec, self.sc = spec, sc
        self.title, self.sub = sc.get("title") or spec.get("title"), None
        self.blocks, self.questions, self.buttons, self.intro, self.reveal = [], [], [], None, None
        self.answers, self.targets, self.copied, self.inputs = [], [], {}, []
        self.status = self.result = None
        self.focused, self.hover, self.sel = False, None, None
        self.touched, self.version, self.prev, self.cache = 0.0, 0, None, None
        self.holding, self.page = False, 0
        self.T = THEMES.get(os.environ.get("CHALK_SCENE_THEME") or sc.get("theme") or "dark", THEMES["dark"])
        self.cw, self.ch = [float(v) for v in (sc.get("size") or [480, 300])[:2]]
        self.k = min(1.5, MAX_W / self.cw, MAX_H / self.ch)
        self.w = self.cw * self.k + 2 * PAD
        self.params = []
        self.values = {}
        for p in sc.get("params") or []:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            p = dict(p)
            if p.get("toggle"):
                p["min"], p["max"] = 0.0, 1.0
            try:
                lo, hi = float(p.get("min", 0)), float(p.get("max", 1))
                v = float(p.get("value", lo))
            except (TypeError, ValueError):
                continue
            if hi <= lo or (p.get("log") and lo <= 0):
                p["log"] = False if lo <= 0 else p.get("log")
                if hi <= lo:
                    continue
            p["min"], p["max"] = lo, hi
            self.values[p["name"]] = max(lo, min(hi, v))
            self.params.append(p)
        self.initial = dict(self.values)
        self.lets = []
        for name, src in (sc.get("let") or {}).items():
            try:
                self.lets.append((name, compile_expr(src)[0]))
            except (SyntaxError, ValueError):
                continue
        self.objects = [o for o in sc.get("objects") or [] if isinstance(o, dict) and kind_of(o)]
        for i, o in enumerate(self.objects):
            o.setdefault("id", f"o{i}")
        ids = [o["id"] for o in self.objects]
        steps = [s for s in sc.get("steps") or [] if isinstance(s, dict)]
        named = {i for s in steps for i in s.get("show") or []}
        first = [i for i in ids if i not in named]
        self.streaming = bool(sc.get("streaming"))   # objects still arriving (add_objects): keep ids not seen yet
        known = (lambda i: True) if self.streaming else (lambda i: i in ids)
        if not steps:
            steps = [{"show": ids}]
        else:
            steps[0] = dict(steps[0], show=first + [i for i in steps[0].get("show") or [] if known(i)])
        self.steps = [dict(s, show=[i for i in s.get("show") or [] if known(i)]) for s in steps]
        self.step = 0
        self.arrived = {}   # replace(): when kept/changed objects come in, overriding the step's own timing
        self.byid = {o["id"]: o for o in self.objects}
        self.uses_t = any("t" in self.names(o) for o in self.objects) or "t" in self.let_names()
        self.uses_st = any("st" in self.names(o) for o in self.objects) or "st" in self.let_names()
        self.t0 = clock()
        self.step_t = [self.t0] + [None] * (len(self.steps) - 1)
        self.drag = None
        self.changed_at = -1e9
        self.tweens = {}
        self.start_tweens(0, self.t0)
        self.focus = None
        self.warm_tex()

    # --- data ---

    def names(self, o):
        out = set()
        for k, v in o.items():
            if k in ("id", "color", "label", "enter", "align", "fill", "on", "tex", "text") and k != "show":
                continue
            for s in (v if isinstance(v, list) else [v]):
                for s2 in (s if isinstance(s, list) else [s]):
                    if isinstance(s2, str):
                        try:
                            out |= compile_expr(s2)[1]
                        except (SyntaxError, ValueError):
                            pass
        for k in ("tex", "text", "label"):
            if isinstance(o.get(k), str):
                for m in _FMT.finditer(o[k]):
                    try:
                        out |= compile_expr(m[1].strip())[1]
                    except (SyntaxError, ValueError):
                        pass
        return out

    def let_names(self):
        out = set()
        for src in (self.sc.get("let") or {}).values():
            try:
                out |= compile_expr(src)[1]
            except (SyntaxError, ValueError):
                pass
        return out

    def env(self, now):
        self.run_tweens(now)
        e = dict(CONSTS)
        e.update(self.values)
        e["t"] = self.time(now)
        e["step"] = self.step + 1                                    # 1-based, as the dots show it
        e["st"] = max(0.0, now - (self.step_t[self.step] or self.t0))  # seconds into this step
        for name, fn in self.lets:
            e[name] = fn(e)
        return e

    def start_tweens(self, si, now):
        """A step's "set": {"f": 5000} or {"f": [20000, 1000]} moves the knob itself, leg by leg (like 3b1b turning
        a dial on screen), so the effect shows without them dragging. Starts once the step's objects are in."""
        self.tweens = {}
        sets = self.steps[si].get("set") or {}
        t = now + 0.15 + len(self.steps[si]["show"]) * LAG + ENTER * 0.6
        dur = float(self.steps[si].get("dur", 1.8))
        for name, target in sets.items():
            p = next((q for q in self.params if q["name"] == name), None)
            if not p:
                continue
            legs, v0, tt = [], self.values[name], t
            for v in (target if isinstance(target, list) else [target]):
                try:
                    v = max(p["min"], min(p["max"], float(v)))
                except (TypeError, ValueError):
                    continue
                legs.append((v0, v, tt, dur))
                v0, tt = v, tt + dur + 0.35
            if legs:
                self.tweens[name] = legs

    def run_tweens(self, now):
        for name, legs in list(self.tweens.items()):
            p = next(q for q in self.params if q["name"] == name)
            for v0, v1, t, dur in legs:
                if now < t:
                    break
                f = ease((now - t) / dur)
                if p.get("log") and v0 > 0 and v1 > 0:
                    self.values[name] = math.exp(math.log(v0) + f * (math.log(v1) - math.log(v0)))
                else:
                    self.values[name] = v0 + f * (v1 - v0)
                self.changed_at = now
            if now >= legs[-1][2] + legs[-1][3]:
                self.values[name] = legs[-1][1]
                del self.tweens[name]

    def time(self, now):
        """Seconds the time-driven objects have run: they stop TIME_RUN s after the last touch."""
        last = max(self.t0, self.touched)
        return min(now, last + TIME_RUN) - self.t0

    def num(self, v, env, default=0.0):
        try:
            r = compile_expr(v)[0](env)
        except (SyntaxError, ValueError):
            return default
        return default if math.isnan(r) else r

    def warm_tex(self):
        srcs = []
        for o in self.objects:
            if "tex" in o:
                srcs.append(self.tex_source(o, self.env(self.t0)))
            if o.get("label") and TEX_LABELS:
                srcs.append(self.label_source(o["label"], self.env(self.t0)))
        for p in self.params:
            if p.get("tex"):
                srcs.append(p["tex"])
        if srcs:
            TEX.get_many(srcs)

    def tex_source(self, o, env):
        s = interpolate(o["tex"], env, tex=True)
        c = o.get("color")
        if c:
            s = r"\color{%s}{%s}" % (self.hex(c), s)
        return s

    def label_source(self, label, env):
        out = []
        for part in re.split(r"(\$[^$]+\$)", str(label)):
            if len(part) > 1 and part.startswith("$") and part.endswith("$"):
                out.append(interpolate(part[1:-1], env, tex=True))
            elif part:
                out.append(r"\text{%s}" % tex_escape(interpolate(part, env).replace("$", "")))
        return "".join(out)

    def hex(self, c):
        c = str(c)
        if c.startswith("#"):
            return c
        return self.T["colors"].get(c.lower(), self.T["colors"]["white"] if c.lower() in ("ink", "chalk") else c)

    def rgb(self, c, default=None):
        if c is None or c is True:
            return default or self.T["ink"]
        h = self.hex(c)
        try:
            return hex_rgb(h) if h.startswith("#") else (default or self.T["ink"])
        except ValueError:
            return default or self.T["ink"]

    # --- time ---

    def shown_at(self, oid):
        """When an object starts coming in (None: not yet)."""
        for si, s in enumerate(self.steps):
            if oid in s["show"]:
                t = self.step_t[si]
                if t is None:
                    return None
                here = [i for i in s["show"] if i in self.byid]
                at = t + 0.15 + (here.index(oid) if oid in here else 0) * LAG
                got = self.arrived.get(oid)
                # replace() marks kept objects as in already (negative); a streamed object comes in when it arrives
                return at if got is None else got if got < 0 else max(at, got)
        return None

    def entry_end(self):
        ends = [self.shown_at(i) for i in self.steps[self.step]["show"] if i in self.byid]
        return max([e for e in ends if e is not None] + [self.step_t[self.step] or self.t0]) + ENTER

    def replace(self, spec):
        """A corrected version of this board (the check pass) takes its place without starting over: same step,
        their slider values kept if the user moved them, objects it already showed stay put, new or changed ones draw in."""
        old = self.__dict__.copy()
        self.__init__(spec)
        now = clock()
        self.step = min(old["step"], len(self.steps) - 1)
        self.step_t = [now - 60 if i <= self.step else None for i in range(len(self.steps))]
        shown = {i for st in old["steps"][:old["step"] + 1] for i in st["show"]}
        self.arrived = {i: (-1e9 if i in shown and old["byid"].get(i) == o else now + 0.15)
                        for i, o in self.byid.items()}
        if old["touched"]:
            self.tweens = {}
            for name, v in old["values"].items():
                p = next((q for q in self.params if q["name"] == name), None)
                if p:
                    self.values[name] = max(p["min"], min(p["max"], v))
        self.t0, self.touched, self.version = old["t0"], old["touched"], old["version"] + 1

    def set_steps(self, steps, at_end=False):
        """Steps that arrived after the objects (a streamed board). at_end: everything is on the board already, so it
        stands on the last step (whose knob starts turning); otherwise what's not in the first step waits for Next."""
        steps = [dict(st, show=list(st.get("show") or [])) for st in steps if isinstance(st, dict)]
        if not steps:
            return
        named = {i for st in steps for i in st["show"]}
        steps[0]["show"] = [o["id"] for o in self.objects if o["id"] not in named] + steps[0]["show"]
        self.steps = steps
        if at_end:
            now = clock()
            self.step = len(steps) - 1
            self.step_t = [self.step_t[0]] + [now - 0.01] * (len(steps) - 1)
            self.arrived.update({i: -1e9 for i in self.byid})   # already drawn in: no re-entry
            self.start_tweens(self.step, now)
        else:
            self.step = min(self.step, len(steps) - 1)
            self.step_t = self.step_t[:1] + [None] * (len(steps) - 1)
        self.version += 1

    def add_objects(self, objs):
        """More objects while the model is still writing them: each draws itself in as it arrives."""
        now = clock()
        for o in objs:
            if not (isinstance(o, dict) and kind_of(o)):
                continue
            o.setdefault("id", f"o{len(self.objects)}")
            self.objects.append(o)
            self.byid[o["id"]] = o
            if not any(o["id"] in st["show"] for st in self.steps):
                self.steps[0]["show"].append(o["id"])
            self.arrived[o["id"]] = now
            self.uses_t = self.uses_t or "t" in self.names(o)
            if "tex" in o:
                TEX.get_many([self.tex_source(o, self.env(now))])
        self.version += 1

    def animating(self, now):
        if now < self.entry_end() + 0.05 or self.tweens:
            return True
        if self.uses_t and now - max(self.t0, self.touched) < TIME_RUN:
            return True
        if self.uses_st and now - (self.step_t[self.step] or self.t0) < ST_RUN:
            return True
        return now - self.changed_at < 0.6

    # --- layout + painting ---

    def snapshot(self):
        now = clock()
        live = self.animating(now)
        key = (self.version, self.hover, self.step, self.focused, round(now, 3) if live else None)
        if self.cache and self.cache[4] == key:
            return self.cache[:4]
        h, hits = self.flow(None, now)
        M = panel.MARGIN
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(math.ceil((self.w + 2 * M) * panel.SCALE)),
                                  int(math.ceil((h + 2 * M) * panel.SCALE)))
        surf.set_device_scale(panel.SCALE, panel.SCALE)
        cr = cairo.Context(surf)
        cr.translate(M, M)
        self.flow(cr, now)
        self.cache = (surf, self.w, h, hits, key)
        return self.cache[:4]

    def flow(self, cr, now):
        """Lay the board out (and paint it when cr is given): header, title, canvas, caption, controls."""
        T, w = self.T, self.w
        hits = []
        y = panel.HEADER
        title_h = 0
        mc = cr or panel.measure_ctx()
        if self.title:
            lay = text_layout(mc, self.title, TITLE_FONT, 19, w - 2 * PAD, bold=True)
            title_h = lay.get_pixel_extents()[1].height + 10
        cap = interpolate(self.steps[self.step].get("say") or "", self.env(now)).replace("$", "")
        cap_lay = text_layout(mc, cap, TEXT_FONT, 15, w - 2 * PAD) if cap else None
        cap_h = cap_lay.get_pixel_extents()[1].height + 12 if cap_lay else 0
        ctl_h = len(self.params) * 34 + (40 if len(self.steps) > 1 else 0) + (8 if self.params else 0)
        canvas_y = y + 10 + title_h
        h = canvas_y + self.ch * self.k + 10 + cap_h + ctl_h + 12
        if cr is not None:
            panel.rounded(cr, 2, 5, w, h, 14)
            cr.set_source_rgba(0, 0, 0, 0.28); cr.fill()
            panel.rounded(cr, 0, 0, w, h, 14)
            cr.set_source_rgb(*T["bg"]); cr.fill_preserve()
            cr.set_source_rgb(*T["edge"]); cr.set_line_width(1.2); cr.stroke()
            cr.save(); panel.rounded(cr, 0, 0, w, h, 14); cr.clip()
            cr.set_source_rgb(*T["header"]); cr.rectangle(0, 0, w, panel.HEADER - 2); cr.fill()
            cr.restore()
        for i, (act, c) in enumerate((("close", (0.93, 0.45, 0.40)), ("dock", (0.95, 0.75, 0.30)), (None, (0.45, 0.75, 0.45)))):
            cx = 16 + i * 14
            if cr is not None:
                cr.new_sub_path(); cr.arc(cx, 12, 4.4 if self.hover == act and act else 4, 0, 2 * math.pi)
                cr.set_source_rgb(*c); cr.fill()
            if act:
                hits.append(((cx - 7, 4, 14, 16), act))
        if cr is not None and self.title:
            cr.move_to(PAD, y + 8)
            cr.set_source_rgb(*T["ink"])
            PangoCairo.show_layout(cr, text_layout(cr, self.title, TITLE_FONT, 19, w - 2 * PAD, bold=True))
        if cr is not None:
            cr.save()
            cr.translate(PAD, canvas_y)
            cr.rectangle(-4, -4, self.cw * self.k + 8, self.ch * self.k + 8); cr.clip()
            cr.scale(self.k, self.k)
            self.paint_canvas(cr, now)
            cr.restore()
        y = canvas_y + self.ch * self.k + 10
        if cap_lay:
            if cr is not None:
                p = ease((now - (self.step_t[self.step] or self.t0)) / 0.5)
                cr.move_to(PAD, y + 4 * (1 - p))
                cr.set_source_rgba(*T["ink"], 0.9 * p)
                PangoCairo.show_layout(cr, cap_lay)
            y += cap_h
        for p in self.params:
            hits += self.paint_param(cr, p, y, now)
            y += 34
        if len(self.steps) > 1:
            hits += self.paint_steps(cr, y + 6)
        return h, hits

    def paint_param(self, cr, p, y, now):
        T, w = self.T, self.w
        name, v = p["name"], self.values[p["name"]]
        label = p.get("label") or name
        unit = p.get("unit") or ""
        hits = []
        if p.get("toggle"):
            txt = f"{label}: {'on' if v >= 0.5 else 'off'}"
            lay = text_layout(cr or panel.measure_ctx(), txt, UI_FONT, 14)
            ext = lay.get_pixel_extents()[1]
            bw, bh = ext.width + 34, 24
            if cr is not None:
                panel.rounded(cr, PAD, y + 4, bw, bh, bh / 2)
                col = self.rgb(p.get("color") or "blue")
                if v >= 0.5:
                    cr.set_source_rgba(*col, 0.85 if self.hover != ("toggle", name) else 1.0); cr.fill()
                    cr.set_source_rgb(*T["bg"])
                else:
                    cr.set_source_rgba(*T["track"], 0.6); cr.fill()
                    cr.set_source_rgb(*T["ink"])
                cr.move_to(PAD + 17, y + 4 + (bh - ext.height) / 2 - ext.y)
                PangoCairo.show_layout(cr, lay)
            hits.append(((PAD, y + 4, bw, bh), ("toggle", name)))
            return hits
        lab = text_layout(cr or panel.measure_ctx(), label, UI_FONT, 14)
        if lab.get_pixel_extents()[1].width > 158:  # a long label is cut short, not run into the track
            lab.set_width(158 * Pango.SCALE)
            lab.set_ellipsize(Pango.EllipsizeMode.END)
        lw = min(170, lab.get_pixel_extents()[1].width + 12)
        val = text_layout(cr or panel.measure_ctx(), with_unit(v, unit), UI_FONT, 14, bold=True)
        vw = 78
        x0, x1 = PAD + lw, w - PAD - vw
        f = self.frac(p, v)
        kx = x0 + f * (x1 - x0)
        ty = y + 16
        if cr is not None:
            col = self.rgb(p.get("color") or "yellow")
            cr.move_to(PAD, ty - lab.get_pixel_extents()[1].height / 2 - 1)
            cr.set_source_rgb(*T["dim"]); PangoCairo.show_layout(cr, lab)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_width(4); cr.set_source_rgb(*T["track"])
            cr.move_to(x0, ty); cr.line_to(x1, ty); cr.stroke()
            cr.set_source_rgb(*col); cr.move_to(x0, ty); cr.line_to(kx, ty); cr.stroke()
            hot = self.hover == ("slider", name) or (self.drag and self.drag[0] == name)
            if now - self.t0 < 3.5 and not self.touched and self.drag is None:  # a nudge: it can be dragged
                pulse = 0.5 + 0.5 * math.sin((now - self.t0) * 5)
                cr.new_sub_path(); cr.arc(kx, ty, 9 + 5 * pulse, 0, 2 * math.pi)
                cr.set_source_rgba(*col, 0.25 * (1 - pulse)); cr.fill()
            cr.new_sub_path(); cr.arc(kx, ty, 9 if hot else 7.5, 0, 2 * math.pi)
            cr.set_source_rgb(*col); cr.fill_preserve()
            cr.set_source_rgb(*T["bg"]); cr.set_line_width(2); cr.stroke()
            cr.move_to(x1 + 12, ty - val.get_pixel_extents()[1].height / 2 - 1)
            cr.set_source_rgb(*col); PangoCairo.show_layout(cr, val)
        hits.append(((x0 - 12, ty - 14, x1 - x0 + 24, 28), ("slider", name, x0, x1)))
        return hits

    def paint_steps(self, cr, y):
        T, w = self.T, self.w
        hits = []
        n = len(self.steps)
        for i in range(n):
            if cr is not None:
                cr.new_sub_path(); cr.arc(PAD + 6 + i * 16, y + 13, 4.5, 0, 2 * math.pi)
                if i <= self.step:
                    cr.set_source_rgb(*self.rgb("blue")); cr.fill()
                else:
                    cr.set_source_rgb(*T["track"]); cr.set_line_width(1.4); cr.stroke()
        last = self.step >= n - 1
        label = "Replay ↺" if last else "Next  ›"
        lay = text_layout(cr or panel.measure_ctx(), label, UI_FONT, 14, bold=True)
        ext = lay.get_pixel_extents()[1]
        bw, bh = ext.width + 28, 26
        bx = w - PAD - bw
        if cr is not None:
            panel.rounded(cr, bx, y, bw, bh, bh / 2)
            col = self.rgb("blue")
            cr.set_source_rgba(*col, 1.0 if self.hover == "next" else 0.85); cr.fill()
            cr.set_source_rgb(*T["bg"])
            cr.move_to(bx + 14, y + (bh - ext.height) / 2 - ext.y); PangoCairo.show_layout(cr, lay)
        hits.append(((bx, y, bw, bh), "next"))
        if self.step > 0:
            lay = text_layout(cr or panel.measure_ctx(), "‹ Back", UI_FONT, 14)
            ext = lay.get_pixel_extents()[1]
            b2 = ext.width + 24
            if cr is not None:
                cr.set_source_rgb(*T["dim"])
                cr.move_to(bx - b2 - 6 + 12, y + (bh - ext.height) / 2 - ext.y); PangoCairo.show_layout(cr, lay)
            hits.append(((bx - b2 - 6, y, b2, bh), "back"))
        return hits

    def frac(self, p, v):
        lo, hi = p["min"], p["max"]
        if p.get("log"):
            return (math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo))
        return (v - lo) / (hi - lo)

    def from_frac(self, p, f):
        f = max(0.0, min(1.0, f))
        lo, hi = p["min"], p["max"]
        if f in (0.0, 1.0):
            return lo if f == 0 else hi
        v = math.exp(math.log(lo) + f * (math.log(hi) - math.log(lo))) if p.get("log") else lo + f * (hi - lo)
        if p.get("step"):
            v = round(v / float(p["step"])) * float(p["step"])
        elif p.get("int"):
            v = round(v)
        return max(lo, min(hi, v))

    # --- the canvas ---

    def paint_canvas(self, cr, now):
        env = self.env(now)
        focus = set(self.steps[self.step].get("focus") or [])
        axes = {}
        for o in self.objects:
            if "axes" in o:
                axes[o["id"]] = self.axes_frame(o, env)
        self.placed = []      # label rects already on the canvas: later labels move off them
        self.deferred = []    # labels, painted after every shape so lines and dots never cross them
        ordered = [o for o in self.objects if kind_of(o) not in ("tex", "text")] + \
                  [o for o in self.objects if kind_of(o) in ("tex", "text")]
        for o in ordered:
            start = self.shown_at(o["id"])
            if start is None or now < start:
                continue
            if "show" in o and self.num(o["show"], env, 1.0) < 0.5:
                continue
            p = ease((now - start) / ENTER)
            alpha = max(0.0, min(1.0, self.num(o.get("opacity", 1.0), env, 1.0)))
            if focus and o["id"] not in focus and "axes" not in o:
                fstart = self.step_t[self.step] or self.t0
                alpha *= 1 - 0.6 * ease((now - fstart) / 0.5)
            cr.save()
            try:
                self.paint_object(cr, o, env, axes, p, alpha, now)
            except (TypeError, ValueError, IndexError, KeyError, ZeroDivisionError):
                pass
            cr.restore()
        labels, self.deferred = self.deferred, None
        for args in labels:
            cr.save()
            try:
                self.paint_label(cr, *args, place=True)
            except (TypeError, ValueError, IndexError, KeyError, ZeroDivisionError):
                pass
            cr.restore()
        self.deferred = []

    def axes_frame(self, o, env):
        x, y, w, h = (self.num(v, env) for v in o["axes"][:4])
        xr = [self.num(v, env) for v in (o.get("x") or [0, 1])[:2]]
        yr = [self.num(v, env) for v in (o.get("y") or [0, 1])[:2]]
        return {"rect": (x, y, w, h), "x": xr, "y": yr, "xlog": bool(o.get("xlog")) and xr[0] > 0,
                "ylog": bool(o.get("ylog")) and yr[0] > 0}

    @staticmethod
    def to_px(ax, xv, yv):
        x, y, w, h = ax["rect"]
        def f(v, r, log):
            if log:
                if v <= 0:
                    return float("nan")
                return (math.log10(v) - math.log10(r[0])) / (math.log10(r[1]) - math.log10(r[0]))
            return (v - r[0]) / (r[1] - r[0])
        return x + f(xv, ax["x"], ax["xlog"]) * w, y + h - f(yv, ax["y"], ax["ylog"]) * h

    def pt(self, o, env, axes, xy):
        """A point of an object: canvas units, or data units when it sits on axes."""
        xv, yv = self.num(xy[0], env), self.num(xy[1], env)
        ax = axes.get(o.get("on"))
        return self.to_px(ax, xv, yv) if ax else (xv, yv)

    def stroke_poly(self, cr, pts, color, width, p, dash=False, alpha=1.0, closed=False):
        pts = [q for q in pts if math.isfinite(q[0]) and math.isfinite(q[1])]
        if len(pts) < 2:
            return
        part = poly_prefix(pts + ([pts[0]] if closed else []), p)
        cr.set_line_cap(cairo.LINE_CAP_ROUND); cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.move_to(*part[0])
        for q in part[1:]:
            cr.line_to(*q)
        cr.set_source_rgba(*color, alpha)
        cr.set_line_width(width)
        if dash:
            cr.set_dash([6, 6])
        cr.stroke()
        cr.set_dash([])

    def fill_of(self, o, col):
        f = o.get("fill")
        if not f:
            return None
        return col if f is True else self.rgb(f, col)

    def level_rgb(self, o, v):
        """A net's colour from its level (Branch Education's voltage colouring): grey at 0, `hi` at vmax."""
        f = max(0.0, min(1.0, v / max(1e-9, self.num(o.get("vmax", 1), {}, 1.0))))
        lo, hi = self.rgb(o.get("lo") or "grey"), self.rgb(o.get("hi") or "red")
        if o.get("ramp") == "volt":   # 0 V red, half amber, full green
            lo, mid, hi = self.rgb("red"), self.rgb("gold"), self.rgb("green")
            return tuple(a + (b - a) * (f * 2) for a, b in zip(lo, mid)) if f < 0.5 else \
                tuple(a + (b - a) * (f * 2 - 1) for a, b in zip(mid, hi))
        return tuple(a + (b - a) * f for a, b in zip(lo, hi))

    def paint_object(self, cr, o, env, axes, p, alpha, now):
        k = kind_of(o)
        T = self.T
        col = self.rgb(o.get("color"), T["ink"] if k in ("axes", "wire", "resistor", "capacitor", "inductor", "source",
                                                          "diode", "ground", "text", "tex") else self.rgb("blue"))
        if "v" in o and k in ("wire", "resistor", "capacitor", "inductor", "diode", "line", "path"):
            col = self.level_rgb(o, self.num(o["v"], env))
        width = self.num(o.get("width", 3.0 if k in ("plot", "path") else 2.4), env, 2.4)
        enter = o.get("enter") or ("write" if k in ("tex", "text") else "fade" if k in ("dot", "flow") else "draw")
        if enter == "none":
            p = 1.0
        if enter == "fade":
            alpha *= p
            p_draw = 1.0
        elif enter == "grow":
            p_draw = 1.0
        else:
            p_draw = p
        if enter == "grow":
            c = self.center(o, env, axes)
            if c:
                s = 0.3 + 0.7 * p
                cr.translate(*c); cr.scale(s, s); cr.translate(-c[0], -c[1])
                alpha *= min(1, p * 2)
        if k == "axes":
            self.paint_axes(cr, o, env, axes[o["id"]], p_draw, alpha)
        elif k == "plot":
            ax = axes.get(o.get("on"))
            if not ax:
                return
            fn = compile_expr(o["plot"])[0]
            lo, hi = [self.num(v, env) for v in (o.get("x") or ax["x"])[:2]]
            n = int(o.get("samples", 160))
            pts = []
            e = dict(env)
            for i in range(n + 1):
                f = i / n
                xv = math.exp(math.log(lo) + f * (math.log(hi) - math.log(lo))) if ax["xlog"] and lo > 0 else lo + f * (hi - lo)
                e["x"] = xv
                yv = fn(e)
                if math.isfinite(yv):
                    pts.append(self.to_px(ax, xv, yv))
            x, y, w, h = ax["rect"]
            cr.rectangle(x - 2, y - 6, w + 8, h + 8); cr.clip()
            fill = self.fill_of(o, col)
            if fill and len(pts) > 1 and p_draw >= 1:
                base = self.to_px(ax, lo, max(ax["y"][0], 0) if not ax["ylog"] else ax["y"][0])[1]
                cr.move_to(pts[0][0], base)
                for q in pts:
                    cr.line_to(*q)
                cr.line_to(pts[-1][0], base); cr.close_path()
                cr.set_source_rgba(*fill, 0.22 * alpha); cr.fill()
            self.stroke_poly(cr, pts, col, width, p_draw, o.get("dash"), alpha)
        elif k == "dot":
            x, y = self.pt(o, env, axes, o["dot"])
            if not (math.isfinite(x) and math.isfinite(y)):
                return
            ax = axes.get(o.get("on"))
            if ax and o.get("guides", True):
                bx, by, bw, bh = ax["rect"]
                cr.set_source_rgba(*col, 0.45 * alpha); cr.set_line_width(1.2); cr.set_dash([4, 4])
                cr.move_to(x, y); cr.line_to(x, by + bh); cr.move_to(x, y); cr.line_to(bx, y); cr.stroke(); cr.set_dash([])
            r = self.num(o.get("r", 6), env, 6)
            cr.new_sub_path(); cr.arc(x, y, r, 0, 2 * math.pi)
            cr.set_source_rgba(*col, alpha); cr.fill()
            if o.get("label"):
                self.paint_label(cr, o["label"], env, x + r + 6, y - r - 4, col, alpha, o.get("label_size", 15), "left")
        elif k in ("line", "arrow"):
            v = o[k]
            a = self.pt(o, env, axes, v[0:2]); b = self.pt(o, env, axes, v[2:4])
            pts = [a, b]
            self.stroke_poly(cr, pts, col, width, p_draw, o.get("dash"), alpha)
            if k == "arrow" and p_draw > 0.6:
                end = poly_prefix(pts, p_draw)[-1]
                ang = math.atan2(b[1] - a[1], b[0] - a[0])
                hl = self.num(o.get("head", 11), env, 11)
                cr.move_to(*end)
                for side in (-1, 1):
                    q = (end[0] + math.cos(ang + math.pi + side * 0.42) * hl, end[1] + math.sin(ang + math.pi + side * 0.42) * hl)
                    cr.line_to(*q)
                cr.close_path()
                cr.set_source_rgba(*col, alpha * min(1, (p_draw - 0.6) / 0.3)); cr.fill()
            if o.get("label"):
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                self.paint_label(cr, o["label"], env, mx + 6, my - 8, col, alpha * p, o.get("label_size", 15), "left")
        elif k in ("rect", "bar"):
            v = o[k]
            if k == "bar":  # [x, base_y, width, height] growing up from the base
                x, by, bw, bh = (self.num(q, env) for q in v[:4])
                if o.get("on") in axes:
                    ax = axes[o["on"]]
                    (x0, base), (x1, tip) = self.to_px(ax, x - bw / 2, by), self.to_px(ax, x + bw / 2, by + bh)
                    x, bw = x0, x1 - x0
                else:
                    base, tip = by, by - bh
                tip = base + (tip - base) * p          # grows out of its base
                rx, ry, rw, rh = x, min(base, tip), bw, abs(base - tip)
                fill = self.fill_of(o, col) or col
                cr.rectangle(rx, ry, rw, rh); cr.set_source_rgba(*fill, 0.85 * alpha); cr.fill()
                if o.get("label"):
                    self.paint_label(cr, o["label"], env, rx + rw / 2, ry - 6, col, alpha, o.get("label_size", 14), "center")
                return
            a = self.pt(o, env, axes, v[0:2])
            b = self.pt(o, env, axes, (f"({v[0]})+({v[2]})", f"({v[1]})+({v[3]})")) if o.get("on") in axes else \
                (a[0] + self.num(v[2], env), a[1] + self.num(v[3], env))
            x0, y0, x1, y1 = min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])
            fill = self.fill_of(o, col)
            r = self.num(o.get("radius", 6), env, 6)
            if fill:
                panel.rounded(cr, x0, y0, x1 - x0, y1 - y0, r)
                cr.set_source_rgba(*fill, self.num(o.get("fill_opacity", 0.22), env, 0.22) * alpha * p); cr.fill()
            pts = rounded_pts(x0, y0, x1 - x0, y1 - y0, r)
            self.stroke_poly(cr, pts, col, width, p_draw, o.get("dash"), alpha)
            if o.get("label"):
                size = self.fit_size(o["label"], env, self.num(o.get("label_size", 15), env, 15), x1 - x0 - 10)
                self.paint_label(cr, o["label"], env, (x0 + x1) / 2, (y0 + y1) / 2 + size / 3, o.get("label_color") and self.rgb(o["label_color"]) or T["ink"],
                                 alpha * p, size, "center")
        elif k == "circle":
            cx, cy = self.pt(o, env, axes, o["circle"][0:2])
            r = self.num(o["circle"][2], env)
            fill = self.fill_of(o, col)
            if fill:
                cr.new_sub_path(); cr.arc(cx, cy, r, 0, 2 * math.pi)
                cr.set_source_rgba(*fill, self.num(o.get("fill_opacity", 0.25), env, 0.25) * alpha * p); cr.fill()
            pts = [(cx + r * math.cos(-math.pi / 2 + i / 60 * math.tau), cy + r * math.sin(-math.pi / 2 + i / 60 * math.tau)) for i in range(61)]
            self.stroke_poly(cr, pts, col, width, p_draw, o.get("dash"), alpha)
            if o.get("label"):
                self.paint_label(cr, o["label"], env, cx, cy + 5, T["ink"], alpha * p, o.get("label_size", 15), "center")
        elif k == "path":
            pts = [self.pt(o, env, axes, q) for q in o["path"]]
            if o.get("smooth"):
                pts = catmull(pts)
            fill = self.fill_of(o, col)
            if fill and o.get("closed") and len(pts) > 2:
                cr.move_to(*pts[0])
                for q in pts[1:]:
                    cr.line_to(*q)
                cr.close_path(); cr.set_source_rgba(*fill, self.num(o.get("fill_opacity", 0.25), env, 0.25) * alpha * p); cr.fill()
            self.stroke_poly(cr, pts, col, width, p_draw, o.get("dash"), alpha, bool(o.get("closed")))
        elif k == "tex":
            x, y = self.pt(o, env, axes, o.get("at") or [0, 0])
            self.paint_tex(cr, self.tex_source(o, env), x, y, self.num(o.get("size", 22), env, 22), o.get("align", "center"),
                           col, alpha, p if enter == "write" else 1.0, interpolate(o["tex"], env, tex=True))
        elif k == "text":
            x, y = self.pt(o, env, axes, o.get("at") or [0, 0])
            self.paint_label(cr, o["text"], env, x, y, col, alpha * (p if enter == "write" else 1.0),
                             self.num(o.get("size", 16), env, 16), o.get("align", "left"), wrap=o.get("wrap"))
        elif k in ("resistor", "capacitor", "inductor", "source", "diode"):
            v = o[k]
            a = self.pt(o, env, axes, v[0:2]); b = self.pt(o, env, axes, v[2:4])
            for seg in component(k, a, b, o):
                self.stroke_poly(cr, seg, col, width, p_draw, False, alpha)
            if o.get("label"):
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                horiz = abs(b[0] - a[0]) >= abs(b[1] - a[1])
                lx, ly, al = (mx, my - 18, "center") if horiz else (mx + 18, my + 5, "left")
                self.paint_label(cr, o["label"], env, lx, ly, self.rgb(o.get("label_color")) if o.get("label_color") else col,
                                 alpha * p, o.get("label_size", 15), al)
        elif k == "wire":
            pts = [self.pt(o, env, axes, q) for q in o["wire"]]
            self.stroke_poly(cr, pts, col, width, p_draw, o.get("dash"), alpha)
            for q in o.get("dots") or []:
                x, y = self.pt(o, env, axes, q)
                cr.new_sub_path(); cr.arc(x, y, 3.6, 0, 2 * math.pi); cr.set_source_rgba(*col, alpha * p); cr.fill()
        elif k == "gate":
            self.paint_gate(cr, o, env, p_draw, alpha, width)
        elif k == "timing":
            self.paint_timing(cr, o, env, p_draw, alpha)
        elif k == "table":
            self.paint_table(cr, o, env, p, alpha)
        elif k == "cells":
            self.paint_cells(cr, o, env, p, alpha, now)
        elif k == "ground":
            x, y = self.pt(o, env, axes, o["ground"])
            for i, half in enumerate((12, 8, 4)):
                self.stroke_poly(cr, [(x - half, y + i * 5), (x + half, y + i * 5)], col, width, p_draw, False, alpha)
        elif k == "flow":
            pts = [self.pt(o, env, axes, q) for q in o["flow"]]
            if len(pts) < 2:
                return
            speed = self.num(o.get("speed", 40), env, 40)
            n = int(o.get("n", 6))
            total = poly_len(pts)
            r = self.num(o.get("r", 4), env, 4)
            tt = env["t"]
            for i in range(n):
                q = point_at(pts, i * total / n + speed * tt)
                cr.new_sub_path(); cr.arc(q[0], q[1], r, 0, 2 * math.pi)
                cr.set_source_rgba(*col, alpha * 0.95); cr.fill()

    # --- EE primitives (Branch Education's vocabulary: research/branch.md) ---

    GATES = {"and": lambda a: all(a), "or": lambda a: any(a), "xor": lambda a: sum(a) % 2 == 1,
             "not": lambda a: not a[0], "buf": lambda a: bool(a[0])}

    def gate_out(self, o, env, ins):
        if "out" in o:
            return self.num(o["out"], env) >= 0.5
        g = str(o["gate"]).lower()
        neg = g in ("nand", "nor", "xnor")
        base = {"nand": "and", "nor": "or", "xnor": "xor"}.get(g, g)
        v = self.GATES.get(base, self.GATES["buf"])([x >= 0.5 for x in ins] or [False])
        return (not v) if neg else v

    def paint_gate(self, cr, o, env, p, alpha, width):
        """A logic gate (distinctive shapes) whose input and output stubs take their level's colour."""
        x, y = (self.num(v, env) for v in (o.get("at") or [0, 0])[:2])
        s = self.num(o.get("size", 40), env, 40)
        h = s * 0.8
        g = str(o["gate"]).lower()
        base = {"nand": "and", "nor": "or", "xnor": "xor"}.get(g, g)
        ins = [self.num(e, env) for e in (o.get("in") or [])[:2]]
        out = self.gate_out(o, env, ins)
        ink = self.rgb(o.get("color"), self.T["ink"])
        if base in ("not", "buf"):
            body = [(x, y - h / 2), (x + s * 0.8, y), (x, y + h / 2), (x, y - h / 2)]
            tip = x + s * 0.8
        elif base == "and":
            body = [(x, y + h / 2), (x, y - h / 2), (x + s / 2, y - h / 2)] + \
                [(x + s / 2 + h / 2 * math.cos(a), y + h / 2 * math.sin(a)) for a in
                 [-math.pi / 2 + i * math.pi / 16 for i in range(17)]] + [(x, y + h / 2)]
            tip = x + s / 2 + h / 2
        else:  # or / xor: a concave back, two curves to the tip
            def quad(a, c, b, n=12):
                return [((1 - t) ** 2 * a[0] + 2 * (1 - t) * t * c[0] + t * t * b[0],
                         (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * c[1] + t * t * b[1]) for t in [i / n for i in range(n + 1)]]
            back = quad((x, y + h / 2), (x + s * 0.25, y), (x, y - h / 2))
            body = back + quad((x, y - h / 2), (x + s * 0.65, y - h / 2), (x + s, y))[1:] + \
                quad((x + s, y), (x + s * 0.65, y + h / 2), (x, y + h / 2))[1:]
            tip = x + s
            if base == "xor":
                self.stroke_poly(cr, [(q[0] - s * 0.12, q[1]) for q in back], ink, width, p, False, alpha)
        bubble = g in ("nand", "nor", "xnor", "not")
        cr.move_to(*body[0])
        for q in body[1:]:
            cr.line_to(*q)
        cr.close_path(); cr.set_source_rgba(*self.T["bg"], alpha); cr.fill()
        self.stroke_poly(cr, body, ink, width, p, False, alpha)
        if bubble:
            r = s * 0.08
            cr.new_sub_path(); cr.arc(tip + r, y, r, 0, 2 * math.pi)
            cr.set_source_rgba(*ink, alpha * p); cr.set_line_width(width); cr.stroke()
            tip += 2 * r
        ys = [y] if len(ins) == 1 or base in ("not", "buf") else [y - h * 0.25, y + h * 0.25]
        for v, yy in zip(ins, ys):
            x0 = x - s * 0.4
            x1 = x + (s * 0.12 if base in ("or", "xor") else 0)
            self.stroke_poly(cr, [(x0, yy), (x1, yy)], self.level_rgb(o, v), width + 0.6, p, False, alpha)
        self.stroke_poly(cr, [(tip, y), (tip + s * 0.4, y)], self.level_rgb(o, 1.0 if out else 0.0), width + 0.6, p, False, alpha)
        if o.get("label"):
            self.paint_label(cr, o["label"], env, x + s * 0.4, y + h / 2 + 16, self.T["dim"], alpha * p, 13, "center")

    def paint_timing(self, cr, o, env, p, alpha):
        """Lanes of signals over a time window (a ModelSim-style waveform), each lane an expression in t_, with a
        cursor across them all. 0/1 lanes draw as logic levels; "bus": true lanes as value bars that change."""
        x, y, w, h = (self.num(v, env) for v in o["timing"][:4])
        t0, t1 = (self.num(v, env) for v in (o.get("window") or [0, 8])[:2])
        lanes = [l for l in o.get("lanes") or [] if isinstance(l, dict) and l.get("expr") is not None]
        if not lanes or t1 <= t0:
            return
        name_w = 50
        lx, lw = x + name_w, w - name_w
        lh = h / len(lanes)
        n = int(o.get("samples", 240))
        tx = lambda tt: lx + (tt - t0) / (t1 - t0) * lw
        for i, ln in enumerate(lanes):
            top = y + i * lh + lh * 0.18
            bot = y + (i + 1) * lh - lh * 0.18
            col = self.rgb(ln.get("color") or ("yellow" if i == 0 else "blue"))
            self.paint_label(cr, ln.get("name", ""), env, x + name_w - 8, (top + bot) / 2 + 5, col, alpha * p, 14, "right", tex=False)
            fn = compile_expr(ln["expr"])[0]
            e = dict(env)
            ts = [t0 + (t1 - t0) * j / n for j in range(n + 1)]
            vals = []
            for tt in ts:
                e["t_"] = tt
                vals.append(fn(e))
            fin = [v for v in vals if math.isfinite(v)]
            if not fin:
                continue
            if ln.get("bus"):
                runs, start = [], 0
                for j in range(1, len(vals) + 1):
                    if j == len(vals) or vals[j] != vals[start]:
                        runs.append((ts[start], ts[min(j, n)], vals[start])); start = j
                k = 4
                for a, b, v in runs:
                    xa, xb = tx(a), tx(b)
                    poly = [(xa, (top + bot) / 2), (min(xa + k, xb), top), (max(xb - k, xa), top), (xb, (top + bot) / 2),
                            (max(xb - k, xa), bot), (min(xa + k, xb), bot), (xa, (top + bot) / 2)]
                    self.stroke_poly(cr, poly, col, 2, p, False, alpha)
                    if xb - xa > 26 and math.isfinite(v):
                        txt = format(int(v), "X" if ln.get("hex", True) else "d") if float(v).is_integer() else nice(v)
                        self.paint_label(cr, txt, env, (xa + xb) / 2, (top + bot) / 2 + 5, col, alpha * p, 13, "center", tex=False)
                continue
            digital = all(v in (0.0, 1.0) for v in fin)
            lo, hi = (0.0, 1.0) if digital else (min(fin), max(fin))
            if hi - lo < 1e-12:
                hi = lo + 1
            pts = []
            for j, (tt, v) in enumerate(zip(ts, vals)):
                if not math.isfinite(v):
                    continue
                yy = bot - (v - lo) / (hi - lo) * (bot - top)
                if digital and pts and abs(pts[-1][1] - yy) > 0.5:
                    pts.append((tx(tt), pts[-1][1]))   # square edges
                pts.append((tx(tt), yy))
            self.stroke_poly(cr, pts, col, 2.6, p, False, alpha)
            cr.set_source_rgba(*self.T["track"], 0.5 * alpha); cr.set_line_width(1)
            cr.move_to(lx, y + (i + 1) * lh); cr.line_to(lx + lw, y + (i + 1) * lh); cr.stroke()
        if o.get("cursor") is not None and p >= 1:
            ct = self.num(o["cursor"], env, float("nan"))
            if math.isfinite(ct) and t0 <= ct <= t1:
                cx = tx(ct)
                cr.set_source_rgba(*self.rgb("yellow"), 0.85 * alpha); cr.set_line_width(1.6)
                cr.move_to(cx, y); cr.line_to(cx, y + h); cr.stroke()

    def paint_table(self, cr, o, env, p, alpha):
        """A truth/state table; the row `hot` picks lights up, so a gate and its table stay in step."""
        rows = [r for r in o["table"] if isinstance(r, list)]
        if not rows:
            return
        x, y = (self.num(v, env) for v in (o.get("at") or [0, 0])[:2])
        size = self.num(o.get("size", 15), env, 15)
        rh = size + 9
        ncol = max(len(r) for r in rows)
        widths = [max(text_layout(panel.measure_ctx(), str(r[c]) if c < len(r) else "", TEXT_FONT, size)
                      .get_pixel_extents()[1].width for r in rows) + 16 for c in range(ncol)]
        hot = self.num(o["hot"], env, float("nan")) if o.get("hot") is not None else float("nan")
        hdr = [str(v) for v in rows[0]]
        dim_except = hdr.index(o["col"]) if o.get("col") in hdr else None
        shown = max(1, int(round(len(rows) * p + 0.5)))
        for ri, r in enumerate(rows[:shown]):
            yy = y + ri * rh
            if ri > 0 and math.isfinite(hot) and int(round(hot)) == ri - 1:
                cr.rectangle(x - 2, yy - size - 3, sum(widths) + 4, rh); cr.set_source_rgba(*self.rgb("yellow"), 0.22 * alpha); cr.fill()
            cx = x
            for c in range(ncol):
                v = r[c] if c < len(r) else ""
                if ri == 0:
                    col = self.T["ink"]
                elif str(v) in ("0", "1") and o.get("bits", True):
                    col = self.rgb("blue" if str(v) == "0" else "red")
                else:
                    col = self.T["ink"]
                a = alpha * (0.4 if dim_except is not None and c != dim_except and ri > 0 else 1.0)
                self.paint_label(cr, str(v), env, cx + widths[c] / 2, yy, col, a, size, "center", tex=False)
                cx += widths[c]
            if ri == 0:
                cr.set_source_rgba(*self.T["dim"], alpha); cr.set_line_width(1.2)
                cr.move_to(x, yy + 6); cr.line_to(x + sum(widths), yy + 6); cr.stroke()

    def paint_cells(self, cr, o, env, p, alpha, now):
        """A register or memory row: n boxes holding the bits (or digits) of `value` (or one expression each in
        `values`); `hot` frames one cell; a cell that changes flashes."""
        x, y, w, h = (self.num(v, env) for v in o["cells"][:4])
        n = max(1, int(self.num(o.get("n", 8), env, 8)))
        base = int(self.num(o.get("base", 2), env, 2)) or 2
        if o.get("values"):
            vals = [self.num(v, env) for v in o["values"][:n]]
            digits = [nice(v) if not float(v).is_integer() else str(int(v)) if math.isfinite(v) else "–" for v in vals]
        else:
            v = self.num(o.get("value", 0), env)
            iv = int(v) if math.isfinite(v) else 0
            digits = []
            for _ in range(n):
                digits.append("0123456789ABCDEF"[iv % base] if base <= 16 else str(iv % base))
                iv //= base
            if o.get("msb", "left") == "left":
                digits.reverse()
        n = len(digits)
        cw = w / max(1, n)
        hot = self.num(o["hot"], env, float("nan")) if o.get("hot") is not None else float("nan")
        prev = self.__dict__.setdefault("_cells", {}).get(o["id"])
        flashes = self.__dict__.setdefault("_flash", {}).setdefault(o["id"], {})
        if prev and len(prev) == len(digits):
            for i, (a, b) in enumerate(zip(prev, digits)):
                if a != b:
                    flashes[i] = now
                    self.changed_at = now
        self._cells[o["id"]] = digits
        col = self.rgb(o.get("color"), self.rgb("blue"))
        for i, d in enumerate(digits[:max(1, int(math.ceil(n * p)))]):
            cx = x + i * cw
            f = flashes.get(i)
            if f is not None and now - f < 0.45:
                cr.rectangle(cx, y, cw, h); cr.set_source_rgba(*self.rgb("yellow"), 0.45 * (1 - (now - f) / 0.45) * alpha); cr.fill()
            on = d not in ("0", "–")
            cr.rectangle(cx + 0.5, y + 0.5, cw - 1, h - 1)
            cr.set_source_rgba(*col, (0.28 if on else 0.08) * alpha); cr.fill_preserve()
            cr.set_source_rgba(*col, 0.8 * alpha); cr.set_line_width(1.3); cr.stroke()
            self.paint_label(cr, d, env, cx + cw / 2, y + h / 2 + 5, self.T["ink"] if on else self.T["dim"], alpha, min(16, h * 0.55), "center", tex=False)
            if o.get("addr"):
                self.paint_label(cr, str(i), env, cx + cw / 2, y + h + 13, self.T["dim"], alpha, 11, "center", tex=False)
        if math.isfinite(hot) and 0 <= int(hot) < n:
            cx = x + int(hot) * cw
            cr.rectangle(cx - 1.5, y - 1.5, cw + 3, h + 3); cr.set_source_rgba(*self.rgb("yellow"), alpha); cr.set_line_width(2.4); cr.stroke()
        if o.get("label"):
            self.paint_label(cr, o["label"], env, x - 8, y + h / 2 + 5, self.T["ink"], alpha * p, 14, "right")

    def center(self, o, env, axes):
        k = kind_of(o)
        v = o.get(k)
        try:
            if k in ("rect",):
                a = self.pt(o, env, axes, v[0:2])
                return a[0] + self.num(v[2], env) / 2, a[1] + self.num(v[3], env) / 2
            if k in ("circle", "dot"):
                return self.pt(o, env, axes, v[0:2])
            if k in ("tex", "text"):
                return self.pt(o, env, axes, o.get("at") or [0, 0])
        except (TypeError, IndexError):
            pass
        return None

    def paint_axes(self, cr, o, env, ax, p, alpha):
        T = self.T
        x, y, w, h = ax["rect"]
        col = self.rgb(o.get("color"), T["dim"])
        if o.get("grid", True) and p >= 1:
            cr.set_line_width(1)
            cr.set_source_rgba(*self.rgb("blue"), 0.16 * alpha)
            for v in self.ticks(o, ax, "x"):
                px = self.to_px(ax, v, ax["y"][0])[0]
                cr.move_to(px, y); cr.line_to(px, y + h)
            for v in self.ticks(o, ax, "y"):
                py = self.to_px(ax, ax["x"][0], v)[1]
                cr.move_to(x, py); cr.line_to(x + w, py)
            cr.stroke()
        self.stroke_poly(cr, [(x, y + h), (x + w + 6, y + h)], col, 2, p, False, alpha)
        self.stroke_poly(cr, [(x, y + h), (x, y - 6)], col, 2, p, False, alpha)
        if p < 0.5:
            return
        a2 = alpha * min(1, (p - 0.5) * 2)
        for v in self.ticks(o, ax, "x"):
            px = self.to_px(ax, v, ax["y"][0])[0]
            cr.set_source_rgba(*col, a2); cr.set_line_width(1.5)
            cr.move_to(px, y + h); cr.line_to(px, y + h + 5); cr.stroke()
            self.paint_label(cr, nice(v), env, px, y + h + 19, T["dim"], a2, 12, "center", tex=False)
        for v in self.ticks(o, ax, "y"):
            py = self.to_px(ax, ax["x"][0], v)[1]
            cr.set_source_rgba(*col, a2); cr.set_line_width(1.5)
            cr.move_to(x - 5, py); cr.line_to(x, py); cr.stroke()
            self.paint_label(cr, nice(v), env, x - 8, py + 4, T["dim"], a2, 12, "right", tex=False)
        if o.get("xlabel"):
            self.paint_label(cr, o["xlabel"], env, x + w, y + h - 8, T["ink"], a2, 15, "right")
        if o.get("ylabel"):
            self.paint_label(cr, o["ylabel"], env, x + 8, y + 2, T["ink"], a2, 15, "left")

    def ticks(self, o, ax, which):
        key = which + "ticks"
        if key in o:
            return [self.num(v, {}) for v in o[key] or []]
        lo, hi = ax[which]
        return nice_ticks(min(lo, hi), max(lo, hi), ax[which + "log"], 4)

    def paint_tex(self, cr, src, x, y, size, align, col, alpha, p, fallback):
        """TeX at (x, baseline y), `size` = font px. p < 1: written in glyph by glyph (outline, then fill)."""
        r = TEX.get(src)
        if not r:
            return self.paint_label(cr, fallback, {}, x, y, col, alpha * p, size, align, tex=False)
        s = size / 1000.0
        wpx = r["w"] * s
        x0 = x - (wpx / 2 if align == "center" else wpx if align == "right" else 0)
        x0 = max(2.0, min(x0, self.cw - wpx - 2))   # never past the canvas edge
        cr.save()
        cr.translate(x0, y)
        cr.scale(s, s)
        n = len(r["glyphs"])
        if p >= 1:  # a dark halo first, so lines and dots behind the formula never cut through it
            for path, _, _ in r["glyphs"]:
                cr.new_path(); cr.append_path(path)
            cr.set_source_rgba(*self.T["bg"], 0.85 * alpha); cr.set_line_width(3.5 / s)
            cr.set_line_join(cairo.LINE_JOIN_ROUND); cr.stroke()
        for i, (path, color, length) in enumerate(r["glyphs"]):
            g = 1.0 if p >= 1 else max(0.0, min(1.0, p * (1 + 0.6) - 0.6 * i / max(1, n - 1)) / 1.0)
            if g <= 0:
                continue
            rgb = col
            if color:
                try:
                    rgb = hex_rgb(color) if color.startswith("#") else self.rgb(color)
                except ValueError:
                    pass
            cr.new_path(); cr.append_path(path)
            if g < 1:
                # outline drawn first, then the fill comes up (Manim's Write)
                cr.set_dash([length * min(1, g * 1.6) + 0.01, length + 1])
                cr.set_line_width(28); cr.set_source_rgba(*rgb, alpha)
                cr.stroke_preserve(); cr.set_dash([])
                cr.set_source_rgba(*rgb, alpha * max(0, (g - 0.45) / 0.55)); cr.fill()
            else:
                cr.set_source_rgba(*rgb, alpha); cr.fill()
        cr.restore()

    def paint_label(self, cr, s, env, x, y, col, alpha, size=15, align="left", tex=None, wrap=None, place=False):
        """A label at (x, baseline y): TeX for $...$ parts (or all of it when TEX_LABELS), else the serif font.
        Labels of shapes wait until every shape is down (place=True), then step off labels already placed."""
        if self.deferred is not None and not place and tex is not False:
            self.deferred.append((s, env, x, y, col, alpha, size, align, tex, wrap))
            return
        raw, s = s, interpolate(s, env)
        if tex is None:
            tex = TEX_LABELS or "$" in s
        if tex and not wrap:
            src = self.label_source(raw, env)
            r = TEX.get(r"\color{%s}{%s}" % (rgb_hex(col), src))
            if r:
                k = size * 1.05 / 1000.0
                w, h, top = r["w"] * k, r["h"] * k, r["y0"] * k
                x, y = self.free_spot(x, y, w, h, top, align, place)
                return self.paint_tex(cr, r"\color{%s}{%s}" % (rgb_hex(col), src), x, y, size * 1.05, align, col, alpha, 1.0, s)
        lay = text_layout(cr, s.replace("$", ""), TEXT_FONT, size, float(wrap) if wrap else None)
        ext = lay.get_pixel_extents()[1]
        base = lay.get_baseline() / Pango.SCALE
        x, y = self.free_spot(x, y, ext.width, ext.height, -base, align, place)
        dx = -ext.width / 2 if align == "center" else -ext.width if align == "right" else 0
        x0 = max(2.0, min(x + dx, self.cw - ext.width - 2))   # never past the canvas edge
        cr.move_to(x0 - ext.x, y - base)
        PangoCairo.layout_path(cr, lay)
        cr.set_source_rgba(*self.T["bg"], 0.85 * alpha); cr.set_line_width(3.5)
        cr.set_line_join(cairo.LINE_JOIN_ROUND); cr.stroke_preserve()
        cr.set_source_rgba(*col, alpha); cr.fill()

    def fit_size(self, label, env, size, width):
        """The label's size, shrunk (down to 10) until it fits `width` (a box's inside)."""
        s_ = interpolate(label, env)
        tex = TEX_LABELS or "$" in s_
        for _ in range(8):
            if tex:
                r = TEX.get(self.label_source(label, env))
                w = r["w"] * size * 1.05 / 1000.0 if r else 0
            else:
                w = text_layout(panel.measure_ctx(), s_.replace("$", ""), TEXT_FONT, size).get_pixel_extents()[1].width
            if w <= width or size <= 10:
                return size
            size = max(10.0, size * max(0.6, width / max(w, 1)) * 0.98)
        return size

    def free_spot(self, x, y, w, h, top, align, place):
        """(x, baseline y) for a w x h label (top = its top relative to the baseline), moved the least it takes
        to clear the labels already placed; remembers where it went."""
        dx = -w / 2 if align == "center" else -w if align == "right" else 0
        def rect(xx, yy):
            x0 = max(2.0, min(xx + dx, self.cw - w - 2))
            return (x0 - 2, yy + top - 1, w + 4, h + 2)
        def clear(r):
            return not any(r[0] < q[0] + q[2] and q[0] < r[0] + r[2] and r[1] < q[1] + q[3] and q[1] < r[1] + r[3]
                           for q in self.placed)
        best = (x, y)
        if place:
            for ox, oy in ((0, 0), (0, -h - 3), (0, h + 3), (w * 0.6, 0), (-w * 0.6, 0), (0, -2 * h - 6), (0, 2 * h + 6),
                           (w * 0.6, -h - 3), (-w * 0.6, h + 3)):
                if clear(rect(x + ox, y + oy)):
                    best = (x + ox, y + oy)
                    break
        if self.placed is not None:
            self.placed.append(rect(*best))
        return best

    # --- input ---

    def act(self, action):
        self.touched = clock()
        if action == "close":
            return self.finish({"status": "dismissed"}) + ["close"]
        if action == "dock":
            return ["dock"]
        if action == "next":
            if self.step < len(self.steps) - 1:
                self.step += 1
                self.step_t[self.step] = clock()
            else:  # replay from the top
                self.step = 0
                self.t0 = self.step_t[0] = clock()
                self.step_t[1:] = [None] * (len(self.steps) - 1)
                self.values.update(self.initial)
            self.start_tweens(self.step, clock())
            self.version += 1
            return ["redraw"]
        if action == "back" and self.step > 0:
            self.tweens = {}
            self.step_t[self.step] = None
            self.step -= 1
            self.version += 1
            return ["redraw"]
        if isinstance(action, tuple):
            if action[0] == "toggle":
                name = action[1]
                self.values[name] = 0.0 if self.values.get(name, 0) >= 0.5 else 1.0
                self.changed_at = clock()
                self.version += 1
                return ["redraw"]
            if action[0] == "slider":
                return ["drag"]
        return []

    def drag_to(self, action, x, y):
        """A press or drag on a slider at panel-local x: the knob follows."""
        if not (isinstance(action, tuple) and action[0] == "slider"):
            return []
        _, name, x0, x1 = action
        p = next((q for q in self.params if q["name"] == name), None)
        if not p:
            return []
        self.tweens.pop(name, None)   # their hand wins over a step moving the knob
        self.values[name] = self.from_frac(p, (x - x0) / max(1.0, x1 - x0))
        self.drag = (name, action)
        self.touched = self.changed_at = clock()
        self.version += 1
        return ["redraw"]

    def end_drag(self):
        self.drag = None
        self.version += 1

    def key(self, name, char, ctrl=False, shift=False):
        self.touched = clock()
        if name == "Escape":
            return ["unfocus"]
        if name in ("Right", "Return", "space"):
            return self.act("next")
        if name == "Left":
            return self.act("back")
        return []

    @property
    def pending(self):
        return False

    def touch(self, major=False):
        self.touched = clock()   # a scene repaints every frame it changes: no crossfade between states
        self.version += 1

    def paste(self, text):
        pass


TEX_LABELS = os.environ.get("CHALK_SCENE_TEXLABELS", "1") != "0"
TITLE_FONT = "CMU Sans Serif, Latin Modern Sans, DejaVu Sans, sans"
UI_FONT = "Inter, DejaVu Sans, sans"


def tex_escape(s):
    return re.sub(r"([#$%&_{}])", r"\\\1", s).replace("~", r"\~{}").replace("^", r"\^{}")


def rgb_hex(c):
    return "#%02X%02X%02X" % tuple(int(max(0, min(1, v)) * 255) for v in c[:3])


def text_layout(cr, s, font, size, width=None, bold=False):
    lay = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription.from_string(font)
    fd.set_absolute_size(size * Pango.SCALE)
    if bold:
        fd.set_weight(Pango.Weight.SEMIBOLD)
    lay.set_font_description(fd)
    if width:
        lay.set_width(int(width * Pango.SCALE))
        lay.set_wrap(Pango.WrapMode.WORD_CHAR)
    lay.set_text(str(s), -1)
    return lay


def rounded_pts(x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    pts = []
    for cx, cy, a0 in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180)):
        for i in range(7):
            a = math.radians(a0 + i * 15)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts + [pts[0]]


def component(kind, a, b, o):
    """A schematic symbol between two points -> polylines (leads included)."""
    ax, ay = a
    bx, by = b
    L = math.hypot(bx - ax, by - ay) or 1
    ux, uy = (bx - ax) / L, (by - ay) / L
    nx, ny = -uy, ux
    body = min(L * 0.6, float(o.get("body", 44)))
    s0, s1 = (L - body) / 2, (L + body) / 2

    def P(d, off=0.0):
        return (ax + ux * d + nx * off, ay + uy * d + ny * off)
    leads = [[P(0), P(s0)], [P(s1), P(L)]]
    if kind == "resistor":
        n = 6
        zig = [P(s0)] + [P(s0 + (i + 0.5) * body / n, 7 * (1 if i % 2 == 0 else -1)) for i in range(n)] + [P(s1)]
        return leads + [zig]
    if kind == "capacitor":
        g = 5
        m = L / 2
        return [[P(0), P(m - g)], [P(m + g), P(L)], [P(m - g, -13), P(m - g, 13)], [P(m + g, -13), P(m + g, 13)]]
    if kind == "inductor":
        n = 4
        coil = []
        for i in range(n):
            for j in range(13):
                th = math.pi * j / 12
                c = s0 + (i + 0.5) * body / n
                coil.append(P(c - math.cos(th) * body / (2 * n), -math.sin(th) * 9))
        return leads + [coil]
    if kind == "diode":
        m = L / 2
        tri = [P(m - 8, -9), P(m - 8, 9), P(m + 8, 0), P(m - 8, -9)]
        return [[P(0), P(m - 8)], [P(m + 8), P(L)], tri, [P(m + 8, -9), P(m + 8, 9)]]
    if kind == "source":
        r = min(18, body / 2)
        m = L / 2
        circ = [P(m + r * math.cos(t), r * math.sin(t)) for t in [i / 40 * math.tau for i in range(41)]]
        out = [[P(0), P(m - r)], [P(m + r), P(L)], circ]
        if o.get("ac", True):
            out.append([P(m - r * 0.6 + r * 1.2 * i / 20, -r * 0.35 * math.sin(i / 20 * math.tau)) for i in range(21)])
        else:
            out += [[P(m - r * 0.5, -4), P(m - r * 0.5, 4)], [P(m + r * 0.5 - 4, 0), P(m + r * 0.5 + 4, 0)],
                    [P(m + r * 0.5, -4), P(m + r * 0.5, 4)]]
        return out
    return leads


# --- storyboards (no clock, no window: for the verify pass and the bench) ---

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def card(sc):
    surf, w, h, hits = sc.snapshot()
    return surf


def save(surf, path, bg=(0.82, 0.82, 0.84)):
    """The card on a plain ground, at 1x layout size (the snapshot is 2x)."""
    w, h = surf.get_width(), surf.get_height()
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(out)
    cr.set_source_rgb(*bg); cr.paint()
    cr.scale(panel.SCALE, panel.SCALE)  # the snapshot is SCALE x: keep every pixel
    cr.set_source_surface(surf, 0, 0); cr.paint()
    out.write_to_png(path)
    return out


def storyboard(spec, out, times=(0.6, 1.6, 8), play=True):
    """Frames of a scene on a virtual clock: while it comes in, each step after Next (mid-entry and settled), each
    control at its ends; plus out/strip.png, a numbered contact sheet of them. The bench's judges and the
    verify pass look at the strip, since a still can't show motion or what a slider does."""
    global clock
    real, clk = clock, Clock()
    clock = clk
    try:
        return _storyboard(spec, out, times, play, clk)
    finally:
        clock = real


def _storyboard(spec, out, times, play, clk):
    os.makedirs(out, exist_ok=True)
    sc = Scene(spec)
    shots = []

    def shot(name, label):
        shots.append((save(card(sc), os.path.join(out, name + ".png")), label))

    t0 = clk.now
    for t in times:
        clk.now = t0 + t
        shot(f"t{t:g}", f"{t:g} s")
    if play:
        settle = ENTER + 0.4 + LAG * 12 + 4
        for i in range(1, len(sc.steps)):
            sc.act("next")
            clk.now += 0.6
            shot(f"step{i + 1}-mid", f"step {i + 1}, 0.6 s")
            if sc.steps[i].get("set"):   # the knob moving on its own: catch it mid-way
                clk.now += 1.9
                shot(f"step{i + 1}-move", f"step {i + 1}, 2.5 s (knob moving)")
            clk.now += settle
            shot(f"step{i + 1}", f"step {i + 1}")
        for p in sc.params:
            for f, tag in ((0.0, "min"), (1.0, "max")):
                if p.get("toggle"):
                    sc.act(("toggle", p["name"]))
                else:
                    sc.values[p["name"]] = sc.from_frac(p, f)
                    sc.touched = sc.changed_at = clk.now
                    sc.version += 1
                clk.now += 0.8
                shot(f"param-{p['name']}-{tag}", f"{p.get('label') or p['name']}: {tag if not p.get('toggle') else 'toggled'}")
                if p.get("toggle"):
                    break
            sc.values[p["name"]] = float(p.get("value", p["min"])) if not p.get("toggle") else sc.values[p["name"]]
    strip(shots, os.path.join(out, "strip.png"))
    return [os.path.join(out, n) for n in sorted(os.listdir(out))]


def strip(shots, path, cols=3):
    """A contact sheet: shots in reading order, each with its label above."""
    if not shots:
        return
    cw = max(s.get_width() for s, _ in shots) // 2
    chh = max(s.get_height() for s, _ in shots) // 2
    rows = (len(shots) + cols - 1) // cols
    W, H = cols * (cw + 16) + 16, rows * (chh + 40) + 16
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(out)
    cr.set_source_rgb(1, 1, 1); cr.paint()
    for i, (s, label) in enumerate(shots):
        x, y = 16 + (i % cols) * (cw + 16), 16 + (i // cols) * (chh + 40)
        cr.set_source_rgb(0.2, 0.2, 0.2)
        cr.select_font_face("sans"); cr.set_font_size(15)
        cr.move_to(x, y + 15); cr.show_text(f"{i + 1}. {label}")
        cr.save(); cr.translate(x, y + 24); cr.scale(0.5, 0.5)  # shots are 2x
        cr.set_source_surface(s, 0, 0); cr.paint(); cr.restore()
    out.write_to_png(path)
