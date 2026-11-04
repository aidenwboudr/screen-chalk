"""Keeping chalk's drawing in step with a screen that keeps changing (system python + numpy, no GTK).

The overlay draws marks for the screen as it was when Claude looked. Then the user scrolls, switches tabs or
workspaces, or starts typing where a card sits. This module gives the overlay what it needs to react:

- `grab`: the screen as small grayscale cells (CELL layout px each) plus how detailed each cell is, ~85 ms a
  frame on the laptop (grim's own scaling and PIL's resize cost ~400 ms; numpy sampling of the native frame
  doesn't).
- `sway_state` / `context_at`: which workspace and window a point belongs to, so marks hide with their window
  and come back with it.
- `place_panel`: where a card or diagram can go so it's over empty screen and away from where they're working;
  None when there's no such place (the panel docks as a small tab instead).
`chalk watch` uses `grab` too.
"""
import json, subprocess
import numpy as np

CELL = 8          # layout px per cell
LEVEL = 14        # mean gray-level difference over a cell's points for it to count as changed


class Cap:
    """A long-lived `chalk-cap serve` (cap/cap.c) for grabbing one output's pixels: no grim process and no
    PPM per frame, ~15 ms of CPU for the whole screen instead of ~85."""

    def __init__(self, env):
        self.env, self.proc, self.outputs = env, None, None

    def gray(self, rect):
        """Plain gray (green channel) of a layout rect inside one output, one byte per layout px (the mean of
        the physical px it covers), or None when chalk-cap can't do it (not built, spans outputs)."""
        import os, shutil
        if self.outputs is None:
            r = subprocess.run(["swaymsg", "-r", "-t", "get_outputs"], env=self.env, capture_output=True, text=True)
            try:
                self.outputs = [(o["name"], tuple(o["rect"][k] for k in ("x", "y", "width", "height")))
                                for o in json.loads(r.stdout) if o.get("active")]
            except ValueError:
                self.outputs = []
        x, y, w, h = map(int, rect)
        out = next((o for o in self.outputs if o[1][0] <= x and o[1][1] <= y and x + w <= o[1][0] + o[1][2]
                    and y + h <= o[1][1] + o[1][3]), None)
        if out is None:
            return None
        if self.proc is None or self.proc.poll() is not None:
            exe = os.environ.get("CHALK_CAP") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cap", "chalk-cap")
            exe = exe if os.access(exe, os.X_OK) else shutil.which("chalk-cap")
            if not exe:
                return None
            self.proc = subprocess.Popen([exe, "serve"], env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL)
        name, (ox, oy, _, _) = out
        try:
            self.proc.stdin.write(f"{name} {x - ox} {y - oy} {w} {h} 0 2000 2\n".encode())
            self.proc.stdin.flush()
            head = self.proc.stdout.readline().split()
            if len(head) != 3:
                return None
            W, H = int(head[0]), int(head[1])
            return np.frombuffer(self.proc.stdout.read(W * H), np.uint8).reshape(H, W)
        except (OSError, ValueError):
            self.proc = None
            return None


_caps = {}


def grab(env, rect):
    """Frame of layout rect (x, y, w, h): float32 samples (rows, 4, cols, 4), 4x4 gray points per CELL px cell.

    From chalk-cap when it's built (layout px, each the mean of the physical px it covers), else grim at
    native resolution (no scaling in grim). `mean`/`detail` summarize a frame; `changed` compares two point
    by point, which tells one line of text from another (their cell means barely differ)."""
    x, y, w, h = rect
    rows, cols = h // CELL, w // CELL
    cap = _caps.setdefault(id(env), Cap(env))
    g = cap.gray(rect)
    if g is not None and g.shape == (h, w):
        off = ((np.arange(4) + 0.5) * CELL / 4).astype(int)
        ri = (np.arange(rows)[:, None] * CELL + off[None, :]).reshape(-1)
        ci = (np.arange(cols)[:, None] * CELL + off[None, :]).reshape(-1)
        return g[ri][:, ci].astype(np.float32).reshape(rows, 4, cols, 4)
    raw = subprocess.run(["grim", "-g", f"{x},{y} {w}x{h}", "-t", "ppm", "-"], env=env,
                         capture_output=True, check=True).stdout
    magic, dims, _maxval, body = raw.split(b"\n", 3)
    W, H = map(int, dims.split())
    px = np.frombuffer(body, np.uint8, count=W * H * 3).reshape(H, W, 3)[:, :, 1]
    rows, cols = h // CELL, w // CELL
    sy, sx = H / h, W / w
    off = (np.arange(4) + 0.5) * CELL / 4
    ri = ((np.arange(rows)[:, None] * CELL + off[None, :]) * sy).astype(int).clip(0, H - 1)   # rows x 4
    ci = ((np.arange(cols)[:, None] * CELL + off[None, :]) * sx).astype(int).clip(0, W - 1)   # cols x 4
    return px[ri.reshape(-1)][:, ci.reshape(-1)].astype(np.float32).reshape(rows, 4, cols, 4)


def mean(f):
    return f.mean(axis=(1, 3))


def detail(f):
    """How much is going on in each cell (text, edges): the spread of its points."""
    return f.max(axis=(1, 3)) - f.min(axis=(1, 3))


def changed(a, b):
    """Cells (rows, cols) that differ between two frames (or two same-sized pieces of frames)."""
    return np.abs(a - b).mean(axis=(1, 3)) > LEVEL


def changed_share(a, b, skip=None):
    """Share of the cells with something in them (before or after) that changed: blank background that
    stays blank doesn't dilute it, so "line 69" -> "other 5069" under a circle reads as all-new. `skip`:
    cells to leave out (chalk's own ink, which never changes)."""
    c = changed(a, b)
    filled = (detail(a) > 30) | (detail(b) > 30) | c
    if skip is not None:
        c, filled = c & ~skip, filled & ~skip
    return float(c.sum()) / max(1, int(filled.sum()))


def ink_mask(strokes, shape, origin, offset=(0, 0)):
    """Cells of a (rows, cols) frame that a shape's own strokes pass through, as a bool array."""
    m = np.zeros(shape, bool)
    ox, oy = origin[0] - offset[0], origin[1] - offset[1]
    for st in strokes:
        for (x0, y0), (x1, y1) in zip(st, st[1:] or st):
            n = max(1, int(max(abs(x1 - x0), abs(y1 - y0)) // 3))
            for i in range(n + 1):
                r = int((y0 + (y1 - y0) * i / n - oy) // CELL)
                c = int((x0 + (x1 - x0) * i / n - ox) // CELL)
                if 0 <= r < shape[0] and 0 <= c < shape[1]:
                    m[r, c] = True
    return m


def piece(f, sl):
    """The part of a frame under (row slice, col slice) from `cells`."""
    return f[sl[0], :, sl[1], :]


def cells(box, shape, origin=(0, 0), pad=0):
    """Box in layout px -> (row slice, col slice) into a frame, clipped; None if it's off the frame."""
    x, y, w, h = box
    ox, oy = origin
    c0, r0 = int((x - pad - ox) // CELL), int((y - pad - oy) // CELL)
    c1, r1 = int((x + w + pad - ox) // CELL) + 1, int((y + h + pad - oy) // CELL) + 1
    c0, r0, c1, r1 = max(0, c0), max(0, r0), min(shape[1], c1), min(shape[0], r1)
    if c0 >= c1 or r0 >= r1:
        return None
    return slice(r0, r1), slice(c0, c1)


# ---------- sway: which workspace and window a mark belongs to ----------

def sway_state(env):
    """{"visible": set of visible workspace names, "outputs": [(rect, ws)], "windows": {id: {...}}}."""
    r = subprocess.run(["swaymsg", "-r", "-t", "get_tree"], env=env, capture_output=True, text=True, timeout=3)
    st = {"visible": set(), "outputs": [], "windows": {}}
    try:
        tree = json.loads(r.stdout)
    except ValueError:
        return None
    ws_r = subprocess.run(["swaymsg", "-r", "-t", "get_workspaces"], env=env, capture_output=True, text=True, timeout=3)
    for w in json.loads(ws_r.stdout or "[]"):
        if w.get("visible"):
            st["visible"].add(w["name"])
            st["outputs"].append((tuple(w["rect"][k] for k in ("x", "y", "width", "height")), w["name"]))

    def walk(n, ws):
        if n.get("type") == "workspace":
            ws = n.get("name")
        if n.get("pid") and n.get("type") in ("con", "floating_con"):
            rect = tuple(n["rect"][k] for k in ("x", "y", "width", "height"))
            st["windows"][n["id"]] = {"rect": rect, "ws": ws, "visible": bool(n.get("visible")),
                                      "name": n.get("name") or "", "floating": n.get("type") == "floating_con"}
        for c in n.get("nodes", []) + n.get("floating_nodes", []):
            walk(c, ws)
    walk(tree, None)
    return st


def inside(rect, x, y):
    return rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]


def context_at(st, x, y):
    """(workspace, window id or None, window rect or None) for the point, as it's shown right now."""
    if not st:
        return None, None, None
    ws = next((name for rect, name in st["outputs"] if inside(rect, x, y)), None)
    hits = [(wid, w) for wid, w in st["windows"].items() if w["visible"] and inside(w["rect"], x, y)]
    if not hits:
        return ws, None, None
    wid, w = max(hits, key=lambda h: h[1]["floating"])  # a floating window sits on top of tiled ones
    return w["ws"] or ws, wid, w["rect"]


def where_now(st, ctx):
    """How a mark drawn in context `ctx` = (ws, win, rect) should show now:
    ("show", (dx, dy)) — its window is visible (moved by dx, dy); ("away", None) — its workspace or window
    isn't on screen (it comes back with it); ("gone", None) — the window closed or changed size."""
    ws, win, rect = ctx
    if not st:
        return "show", (0, 0)
    if win is None:
        return ("show", (0, 0)) if ws is None or ws in st["visible"] else ("away", None)
    w = st["windows"].get(win)
    if w is None:
        return "gone", None
    if not w["visible"] or (w["ws"] and w["ws"] not in st["visible"]):
        return "away", None
    if (w["rect"][2], w["rect"][3]) != (rect[2], rect[3]):
        return "gone", None
    return "show", (w["rect"][0] - rect[0], w["rect"][1] - rect[1])


# ---------- where a panel can go ----------

def place_panel(w, h, screen, detail, heat, origin, avoid=(), cur=None, step=40):
    """Top-left for a w x h panel on `screen` over the emptiest screen that isn't where they're been working
    (`heat`: cells that changed lately), off `avoid` boxes; None when every spot would cover their work."""
    sx, sy, sw, sh = screen
    busy = (detail > 40).astype(np.float32)
    hot = (heat > 0).astype(np.float32)
    best, best_c = None, None
    for y in range(int(sy + 8), int(sy + sh - h - 7), step):
        for x in range(int(sx + 8), int(sx + sw - w - 7), step):
            sl = cells((x, y, w, h), busy.shape, origin)
            if sl is None:
                continue
            ring = cells((x, y, w, h), busy.shape, origin, pad=48)
            c = 12.0 * float(busy[sl].mean()) + 60.0 * float(hot[ring].mean())
            c += sum(50 for b in avoid if overlap((x, y, w, h), b))
            if cur:
                c += ((x - cur[0]) ** 2 + (y - cur[1]) ** 2) ** 0.5 / 1500.0
            if best_c is None or c < best_c:
                best, best_c = (x, y), c
    # over a lot of detail, or next to where they're working: better as a tab than in the way
    if best is None or best_c > 3.0:
        return None
    return best


def overlap(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]
