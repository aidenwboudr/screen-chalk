"""Where labels and cards go: next to their mark, on screen, not over other labels, and as far
as possible over empty screen instead of on top of text the user is trying to read.

`busy` is an occupancy grid built from the screenshot (how much detail each CELL x CELL square
has, 0..255); without one, placement only avoids the screen edge and other labels.
Pure Python so both the overlay (system python + GTK) and the bench can use it.
"""
import base64, os

CELL = 16


def occupancy(img):
    """Screenshot (path or PIL image) -> {"cell", "cols", "rows", "data"(base64 bytes)}:
    per-cell share of edge pixels."""
    import numpy as np
    from PIL import Image
    if isinstance(img, str):
        img = Image.open(img)
    g = np.asarray(img.convert("L"), dtype=np.int16)
    edges = np.zeros_like(g, dtype=bool)
    edges[:, 1:] |= np.abs(np.diff(g, axis=1)) > 24
    edges[1:, :] |= np.abs(np.diff(g, axis=0)) > 24
    h, w = edges.shape
    rows, cols = h // CELL, w // CELL
    e = edges[:rows * CELL, :cols * CELL].reshape(rows, CELL, cols, CELL).mean(axis=(1, 3))
    # any text stroke in a cell makes it "busy": scale so ~12% edge pixels already counts as full
    occ = np.clip(e / 0.12, 0, 1)
    # and how bright each cell is, so text drawn straight on the screen gets a halo that contrasts
    lum = g[:rows * CELL, :cols * CELL].reshape(rows, CELL, cols, CELL).mean(axis=(1, 3))
    return {"cell": CELL, "cols": cols, "rows": rows,
            "data": base64.b64encode((occ * 255).astype(np.uint8).tobytes()).decode(),
            "lum": base64.b64encode(np.clip(lum, 0, 255).astype(np.uint8).tobytes()).decode()}


GRID_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf")


def with_grid(img, step=100):
    """A copy of the screenshot with faint labelled lines every `step` px. On the hard bench this
    lifted Sonnet's hit rate (it reads positions off the labels instead of estimating them)."""
    from PIL import ImageDraw, ImageFont
    img = img.convert("RGB")
    d = ImageDraw.Draw(img, "RGBA")
    font = next((ImageFont.truetype(p, 13) for p in GRID_FONTS if os.path.exists(p)), None) or ImageFont.load_default(13)
    w, h = img.size
    for x in range(0, w, step):
        d.line([(x, 0), (x, h)], fill=(255, 0, 160, 70), width=1)
        d.text((x + 2, 2), str(x), fill=(255, 0, 160, 220), font=font)
    for y in range(0, h, step):
        d.line([(0, y), (w, y)], fill=(255, 0, 160, 70), width=1)
        d.text((2, y + 2), str(y), fill=(255, 0, 160, 220), font=font)
    return img


class Busy:
    def __init__(self, occ, origin=(0, 0)):
        self.cell = occ["cell"]; self.cols = occ["cols"]; self.rows = occ["rows"]
        self.grid = base64.b64decode(occ["data"])
        self.lum = base64.b64decode(occ["lum"]) if occ.get("lum") else None
        self.ox, self.oy = origin

    def _mean(self, grid, x, y, w, h):
        c = self.cell
        c0 = max(0, int((x - self.ox) // c)); c1 = min(self.cols, int((x - self.ox + w) // c) + 1)
        r0 = max(0, int((y - self.oy) // c)); r1 = min(self.rows, int((y - self.oy + h) // c) + 1)
        if c0 >= c1 or r0 >= r1:
            return None
        tot = 0
        for r in range(r0, r1):
            tot += sum(grid[r * self.cols + c0: r * self.cols + c1])
        return tot / (255.0 * (r1 - r0) * (c1 - c0))

    def cover(self, x, y, w, h):
        """Mean busyness (0..1) under a box in layout px."""
        return self._mean(self.grid, x, y, w, h) or 0.0

    def light(self, x, y, w, h):
        """Is the screen under this box light (so ink needs a white halo, not a dark one)?"""
        if self.lum is None:
            return False
        m = self._mean(self.lum, x, y, w, h)
        return m is not None and m > 0.6


def overlaps(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def gap_between(a, b):
    """Edge-to-edge distance between two boxes (0 when they touch or overlap)."""
    dx = max(0, max(a[0], b[0]) - min(a[0] + a[2], b[0] + b[2]))
    dy = max(0, max(a[1], b[1]) - min(a[1] + a[3], b[1] + b[3]))
    return (dx * dx + dy * dy) ** 0.5


def leader(box, anchor):
    """Endpoints for a short connector from a far-off label back to its mark, or None if it's close."""
    if gap_between(box, anchor) < 28:
        return None
    def nearest(b, p):
        return (min(max(p[0], b[0]), b[0] + b[2]), min(max(p[1], b[1]), b[1] + b[3]))
    ac = (anchor[0] + anchor[2] / 2, anchor[1] + anchor[3] / 2)
    p0 = nearest(box, ac)
    p1 = nearest(anchor, p0)
    return p0, p1


def place_board(w, h, screen, anchor=None, taken=(), busy=None, keep_clear=(), want=None):
    """Top-left for a big panel (a sketch's chalkboard): scan the whole screen for the emptiest
    spot, a little biased toward its anchor. `want` is the model's own pick; it wins unless it
    covers clearly more of the screen than the best spot found."""
    sx, sy, sw, sh = screen

    def cost(x, y):
        box = (x, y, w, h)
        c = 16.0 * (busy.cover(*box) if busy else 0.0)  # 8 let boards land on the evidence
        if anchor:
            c += gap_between(box, anchor) / 500.0
        c += sum(50 for t in taken if overlaps(box, t)) + sum(20 for k in keep_clear if overlaps(box, k))
        return c

    def clamp(x, y):
        return min(max(x, sx + 4), sx + sw - w - 4), min(max(y, sy + 4), sy + sh - h - 4)

    step = 40
    best, best_c = None, None
    for y in range(int(sy + 4), int(sy + sh - h - 3), step):
        for x in range(int(sx + 4), int(sx + sw - w - 3), step):
            c = cost(x, y)
            if best_c is None or c < best_c:
                best, best_c = (x, y), c
    if best is None:
        best = clamp(sx + (sw - w) / 2, sy + (sh - h) / 2)
        best_c = cost(*best)
    if want:
        wx, wy = clamp(*want)
        if cost(wx, wy) <= best_c + 1.2:  # ~15% more busy screen than the best spot is fine
            return wx, wy
    return best


def place(anchor, w, h, screen, taken=(), busy=None, pref=None, keep_clear=()):
    """Best top-left (x, y) for a w x h box beside anchor [x, y, w, h].

    taken: boxes of labels already placed; keep_clear: boxes of marks/targets that must stay visible."""
    ax, ay, aw, ah = anchor
    sx, sy, sw, sh = screen
    cands = []
    for gap in (10, 34, 70):
        cands += [
            ("above", ax + aw / 2 - w / 2, ay - h - gap),
            ("below", ax + aw / 2 - w / 2, ay + ah + gap),
            ("right", ax + aw + gap + 4, ay + ah / 2 - h / 2),
            ("left", ax - w - gap - 4, ay + ah / 2 - h / 2),
            ("above-right", ax + aw - 10 + gap / 2, ay - h - gap),
            ("below-right", ax + aw - 10 + gap / 2, ay + ah + gap),
            ("above-left", ax - w + 10 - gap / 2, ay - h - gap),
            ("below-left", ax - w + 10 - gap / 2, ay + ah + gap),
        ]
    best, best_cost = None, None
    for name, x, y in cands:
        x = min(max(x, sx + 4), sx + sw - w - 4)
        y = min(max(y, sy + 4), sy + sh - h - 4)
        box = (x, y, w, h)
        cost = gap_between(box, anchor) / 100 + (0.0 if name == pref else 0.4)
        if busy is not None:
            cost += 6.0 * busy.cover(*box)
        for t in taken:
            if overlaps(box, t):
                cost += 50
        for k in keep_clear:
            if overlaps(box, k):
                cost += 20
        if best_cost is None or cost < best_cost:
            best, best_cost = (x, y), cost
    return best
