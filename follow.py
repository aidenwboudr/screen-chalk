"""Marks that move with what they mark (system python + numpy, no GTK).

A mark is drawn for the screen as Claude saw it. When the user scrolls, the thing it circles slides away under
it. This module keeps each mark on its target in any app (browser, editor, terminal, PDF) by watching the
pixels rather than asking the app:

- `Anchor`: a small picture of the target and some context around it (a couple of lines above and below,
  so repeated lines can be told apart), cut down to ~1000 sample points (edges and the quiet background
  between them). It comes from `chalk look`'s screenshot when there's a recent one (the overlay was hidden
  for it, and it's from before the user had time to scroll), else from the screen as the mark goes up.
- `Anchor.near`: where the target went since the last frame, searched around where its motion says it
  should be, first with a few hundred of the points, then all of them around the best spots, ~1-2 ms.
- `Anchor.band`: finding it anywhere up and down its column of the window: after a jump too big for `near`,
  or when it comes back into view. Only a clear match that no other spot comes close to counts, so a mark
  never jumps onto a look-alike line.

Frames come from `chalk-cap` (cap/cap.c), which only answers once the region changed: nothing runs while
the screen is still. chalk's own ink is in those frames too, so the pixels under it are left out.
"""
import ctypes, os
import numpy as np

MARGIN_X, MARGIN_Y = 10, 30   # context around the target the match uses (two lines of text above and below)
MIN_W, MIN_H = 64, 48
REACH = 150                   # px searched around where the target should be (a flick from rest moves ~100/frame)
# Whether a spot *is* the target: the share of sample points that are clearly off there (by more than half
# the template's contrast). On the bench's recordings the true spot never went above 0.024 while the best
# look-alike (a neighbouring `ls -la` line) never got below 0.066; the mean error had them at 0.21 vs 0.32.
GOOD = 0.04                   # this or better counts as the target
LOST = 0.05                   # worse than this while tracking and it's not there (scrolled off, covered, another tab)
UNIQUE = 0.03                 # a match found by searching the whole column must beat any other spot by this much
MIN_VALID = 0.55              # share of the sample points that have to be on screen (not under ink) to judge
COARSE = 320                  # points used to rank candidate spots before all of them check the best few
STILL = 0.67                  # moves smaller than this (px) don't count: redrawing the mark for them would only
                              # make the next frame differ again, and so on


def _load_match():
    """cap/libchalkmatch.so (Anchor.errors in C, ~20x faster), or None to use numpy."""
    for d in (os.environ.get("CHALK_CAP_DIR"), os.path.join(os.path.dirname(os.path.abspath(__file__)), "cap")):
        path = d and os.path.join(d, "libchalkmatch.so")
        if path and os.path.exists(path):
            try:
                lib = ctypes.CDLL(path)
            except OSError:
                continue
            P = ctypes.c_void_p
            lib.chalk_match.argtypes = [P, P, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, P, P, P,
                                        ctypes.c_int, P, ctypes.c_int, P, ctypes.c_int, ctypes.c_float,
                                        ctypes.c_float, ctypes.c_float, P, P]
            lib.chalk_match.restype = None
            return lib
    return None


_C = None if os.environ.get("CHALK_MATCH_NUMPY") else _load_match()


def blur(a):
    """3x3 [1 2 1] smoothing (uint8, same size; the 1 px border is left as it was), so text compares well at
    fractional scroll positions. chalk-cap's frames come smoothed this way already (cap.c blur)."""
    a = np.asarray(a, np.uint8)
    out = a.copy()
    if a.shape[0] < 3 or a.shape[1] < 3:
        return out
    u = a.astype(np.uint16)
    b = u[1:-1] * 2 + u[:-2] + u[2:]
    out[1:-1, 1:-1] = (b[:, 1:-1] * 2 + b[:, :-2] + b[:, 2:] + 8) // 16
    return out


def template_rect(box, win):
    """The target box grown by the context margins (and to a minimum size), clipped to its window."""
    x, y, w, h = box
    gx, gy = max(MARGIN_X, (MIN_W - w) / 2), max(MARGIN_Y, (MIN_H - h) / 2)
    x0, y0, x1, y1 = x - gx, y - gy, x + w + gx, y + h + gy
    if win:
        x0, y0 = max(x0, win[0]), max(y0, win[1])
        x1, y1 = min(x1, win[0] + win[2]), min(y1, win[1] + win[3])
    x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
    if x1 - x0 < 16 or y1 - y0 < 12:
        return None
    return x0, y0, x1 - x0, y1 - y0


class Anchor:
    """Sample points of a target's picture, and where it is now (top-left of the template, screen px)."""

    def __init__(self, img, valid, rect, smoothed=False):
        """img: gray (h, w) of `rect` (screen px), smoothed if it's from chalk-cap; valid: bool (h, w), False
        under chalk's own ink."""
        self.rect0 = rect
        self.x, self.y = float(rect[0]), float(rect[1])
        self.vx = self.vy = 0.0       # px per ms, from the last fixes
        self.t = None                 # time of the last fix (ms)
        self.lost = False
        self.misses = 0               # frames in a row it wasn't found
        self.err = 0.0
        g = (np.asarray(img, np.uint8) if smoothed else blur(img)).astype(np.float32)
        h, w = g.shape
        grad = np.zeros_like(g)
        grad[1:-1, 1:-1] = np.abs(g[2:, 1:-1] - g[:-2, 1:-1]) + np.abs(g[1:-1, 2:] - g[1:-1, :-2])
        ok = valid.copy()
        ok[[0, -1], :] = False
        ok[:, [0, -1]] = False
        pts, strong = [], []
        # per 6x6 cell, its strongest edge and its quietest point: spread over the whole template, and the
        # quiet ones keep a match from landing on any busy patch
        for cy in range(1, h - 1, 6):
            for cx in range(1, w - 1, 6):
                c = grad[cy:cy + 6, cx:cx + 6]
                m = ok[cy:cy + 6, cx:cx + 6]
                if not m.any():
                    continue
                cv = np.where(m, c, -1)
                i = np.unravel_index(int(np.argmax(cv)), c.shape)
                if cv[i] > 12:
                    pts.append((cy + i[0], cx + i[1]))
                    strong.append(float(cv[i]))
                cq = np.where(m, c, 1e9)
                j = np.unravel_index(int(np.argmin(cq)), c.shape)
                if cq[j] < 1e9 and (not pts or (cy + j[0], cx + j[1]) != pts[-1]):
                    pts.append((cy + j[0], cx + j[1]))
                    strong.append(0.0)
        p = np.array(pts or [(h // 2, w // 2)], np.int32)
        self.py, self.px = p[:, 0], p[:, 1]
        self.val = g[self.py, self.px]
        st = np.array(strong or [0.0])
        self.texture = float((st > 12).mean())
        self.contrast = max(10.0, float(np.abs(self.val - np.median(g[ok] if ok.any() else g)).mean()))
        lo, hi = np.percentile(self.val, (2, 98))  # (of the sample points: half of them sit on edges)
        self.tau = min(40.0, max(16.0, 0.25 * float(hi - lo)))   # a point this far off is wrong, not blur
        self.size = (w, h)
        # the coarse set: an even spread of the points (edges and quiet ones alike)
        k = max(1, len(p) // COARSE)
        self.sub = np.arange(0, len(p), k)

    @property
    def usable(self):
        """Enough going on in the picture to follow it (a blank patch matches anywhere)."""
        return len(self.py) >= 30 and self.texture > 0.12

    def shift(self):
        """How far the target moved since it was pictured (screen px)."""
        return self.x - self.rect0[0], self.y - self.rect0[1]

    def total(self):
        """How far the target is from where the mark was first put (`base`: how far it was already then)."""
        bx, by = getattr(self, "base", (0, 0))
        return self.x - self.rect0[0] + bx, self.y - self.rect0[1] + by

    def predict(self, t):
        if self.t is None or self.lost:
            return self.x, self.y
        dt = min(max(t - self.t, 0.0), 100.0)
        return self.x + self.vx * dt, self.y + self.vy * dt

    def reach(self, t):
        """(x0, y0, x1, y1): the template positions (top-left) `near` will consider at time t."""
        px, py = self.predict(t)
        r = REACH + 0.5 * abs(py - self.y)
        return (min(px, self.x) - 10, min(py, self.y) - r, max(px, self.x) + 10, max(py, self.y) + r)

    def errors(self, frame, fmask, ox, oy, ys, xs, idx=None, bad=False):
        """Match error of the template with its top-left at each (y, x) for y in ys, x in xs (screen px):
        the mean difference over its contrast (smooth, for ranking spots), and with bad=True also the share
        of points clearly off (for deciding). frame: smoothed gray (H, W; uint8 from chalk-cap) whose top-left
        is screen (ox, oy); fmask: where frame px can be judged; idx: which sample points to use (default all)."""
        H, W = frame.shape
        py, px, val = (self.py, self.px, self.val) if idx is None else (self.py[idx], self.px[idx], self.val[idx])
        ys, xs = np.asarray(ys, np.float32), np.asarray(xs, np.float32)
        if _C is not None and frame.dtype == np.uint8:
            frame = np.ascontiguousarray(frame)
            fm = np.ascontiguousarray(fmask)
            fm = fm.view(np.uint8) if fm.dtype == bool else fm.astype(np.uint8)
            py, px = np.ascontiguousarray(py, np.int32), np.ascontiguousarray(px, np.int32)
            val = np.ascontiguousarray(val, np.float32)
            ys, xs = np.ascontiguousarray(ys), np.ascontiguousarray(xs)
            err = np.empty((len(ys), len(xs)), np.float32)
            frac = np.empty_like(err)
            _C.chalk_match(frame.ctypes.data, fm.ctypes.data, H, W, int(ox), int(oy), py.ctypes.data, px.ctypes.data,
                           val.ctypes.data, len(py), ys.ctypes.data, len(ys), xs.ctypes.data, len(xs),
                           self.contrast, self.tau, MIN_VALID, err.ctypes.data, frac.ctypes.data)
            return (err, frac) if bad else err
        Y = np.floor(ys).astype(np.int32)[:, None, None] - oy
        X = np.floor(xs).astype(np.int32)[None, :, None] - ox
        r = Y + py[None, None, :]
        c = X + px[None, None, :]
        inside = (r >= 0) & (r < H - 1) & (c >= 0) & (c < W - 1)
        flat = np.where(inside, r * W + c, 0)
        fl = frame.ravel()
        v = fl[flat].astype(np.float32)
        fy, fx = (ys - np.floor(ys))[:, None, None], (xs - np.floor(xs))[None, :, None]
        if fx.any():  # between pixels (content scrolls by thirds of a px at 1.5x): bilinear
            v = (v * (1 - fx) + fl[flat + 1] * fx) * (1 - fy) + (fl[flat + W] * (1 - fx) + fl[flat + W + 1] * fx) * fy
        elif fy.any():
            v = v * (1 - fy) + fl[flat + W] * fy
        ok = inside & fmask.ravel()[flat]
        n = ok.sum(axis=2)
        diff = np.where(ok, np.abs(v - val[None, None, :]), 0)
        err = diff.sum(axis=2) / np.maximum(n, 1) / self.contrast
        few = n < MIN_VALID * len(py)
        err[few] = 9.0
        if not bad:
            return err
        frac = (diff > self.tau).sum(axis=2) / np.maximum(n, 1)
        frac[few] = 1.0
        return err, frac

    def refine(self, frame, fmask, ox, oy, ys, x, xr=1, sub_x=False):
        """All points, around the best few rows of a coarse pass: (x, y, share of points off) of the best
        spot (best by mean error, then to a third of a px: content at 1.5x scale moves in thirds; sideways
        too with sub_x)."""
        best = None
        for y in ys:
            e = self.errors(frame, fmask, ox, oy, np.arange(y - 1, y + 2), np.arange(x - xr, x + xr + 1))
            k = np.unravel_index(int(np.argmin(e)), e.shape)
            cand = (x - xr + int(k[1]), y - 1 + int(k[0]), float(e[k]))
            if best is None or cand[2] < best[2]:
                best = cand
        bx, by = best[0], best[1]
        steps = np.array([-2, -1, 0, 1, 2], np.float32) / 3
        sx = steps if sub_x else np.zeros(1, np.float32)
        e, f = self.errors(frame, fmask, ox, oy, by + steps, bx + sx, bad=True)
        k = np.unravel_index(int(np.argmin(e)), e.shape)
        return float(bx + sx[k[1]]), float(by + steps[k[0]]), float(f[k])

    def near(self, frame, fmask, ox, oy, t):
        """Track it from the last fix: (x, y, err), or None when it isn't within reach."""
        x0, y0, x1, y1 = self.reach(t)
        bx = int(round(self.predict(t)[0]))
        ys = np.arange(int(y0), int(y1) + 1)
        e = self.errors(frame, fmask, ox, oy, ys, [bx], self.sub)[:, 0]
        top = ys[np.argsort(e)[:2]]
        best = self.refine(frame, fmask, ox, oy, top, bx, sub_x=abs(self.vx) > 0.01)
        if best[2] > GOOD:  # not straight up or down: try sideways too (horizontal scroll, a reflow)
            xs = np.arange(int(x0) - REACH // 2, int(x1) + REACH // 2 + 1, 2)
            e2 = self.errors(frame, fmask, ox, oy, [int(round(best[1]))], xs, self.sub)[0]
            sx = int(xs[int(np.argmin(e2))])
            side = self.refine(frame, fmask, ox, oy, [int(round(best[1]))], sx, xr=2, sub_x=True)
            if side[2] < best[2]:
                ys = np.arange(int(side[1]) - 20, int(side[1]) + 21)
                e = self.errors(frame, fmask, ox, oy, ys, [side[0]], self.sub)[:, 0]
                best = min(side, self.refine(frame, fmask, ox, oy, ys[np.argsort(e)[:2]], int(round(side[0])), sub_x=True),
                           key=lambda b: b[2])
        return best if best[2] <= LOST else None

    def band(self, frame, fmask, ox, oy, x_hint, strict=True, good=GOOD):
        """Look for it anywhere up and down the frame (its column of the window): (x, y, err) for a clear
        match. strict: no other spot may come close (UNIQUE), so it never lands on a look-alike."""
        H, W = frame.shape
        h = self.size[1]
        x = int(round(x_hint))
        ys = np.arange(oy - h // 2, oy + H - h // 2)
        if len(ys) == 0:
            return None
        e = self.errors(frame, fmask, ox, oy, ys, [x], self.sub)[:, 0]
        order = np.argsort(e)
        top = ys[order[:4]]
        best = self.refine(frame, fmask, ox, oy, top, x)
        if best[2] > good:
            return None
        if strict:
            far = np.abs(ys - best[1]) > 8
            if far.any():
                # the best other spot, checked with every point like the winner was
                other = ys[far][np.argsort(e[far])[:3]]
                rival = self.refine(frame, fmask, ox, oy, other, x)
                if rival[2] < best[2] + UNIQUE:
                    return None
        return best

    def fix(self, x, y, err, t):
        """Record where it was found at time t (ms)."""
        if self.t is not None and not self.lost and t > self.t:
            dt = t - self.t
            a = 0.5 if dt < 120 else 0.0   # smooth the velocity over a few frames, forget it after a gap
            self.vx = a * (x - self.x) / dt + (1 - a) * self.vx
            self.vy = a * (y - self.y) / dt + (1 - a) * self.vy
            if abs(self.vx) < 0.02 and abs(self.vy) < 0.02:  # (under 20 px/s: it has stopped)
                self.vx = self.vy = 0.0
        else:
            self.vx = self.vy = 0.0
        self.x, self.y, self.err, self.t, self.lost, self.misses = float(x), float(y), err, t, False, 0

    def miss(self, t):
        """Not found this frame. True once that's twice in a row (then it's lost: hide the mark)."""
        self.misses += 1
        if self.misses >= 2 and not self.lost:
            self.lost, self.vx, self.vy = True, 0.0, 0.0
        return self.lost


def track(a, frame, fmask, ox, oy, t, column):
    """One frame for one anchor: near its expected spot, then (if that fails, or it's lost) anywhere in
    `column` (the frame covers it). Returns "moved", "lost", "found" or None (nothing new)."""
    was = (a.x, a.y, a.lost)
    r = None
    if not a.lost:
        r = a.near(frame, fmask, ox, oy, t)
    if r is None and column:
        r = a.band(frame, fmask, ox, oy, a.x)
    if r:
        a.recheck = max(0, getattr(a, "recheck", 0) - 1)
        still = not was[2] and abs(r[0] - a.x) < STILL and abs(r[1] - a.y) < STILL
        if still:
            r = (a.x, a.y, r[2])  # a third of a px either way is the matching wobbling, not the page moving
        a.fix(*r, t)
        if still:  # it has stopped: drawing it ahead any further would overshoot
            a.vx = a.vy = 0.0
        if was[2]:
            a.recheck = 2  # (its speed is unknown again: look in the whole column for the next frames)
            return "found"
        return "moved" if (a.x, a.y) != was[:2] else None
    if was[2]:
        return None
    return "lost" if a.miss(t) else None


def stamp(mask, pts, radius, ox, oy):
    """Mark the px within `radius` of a polyline (screen px) in a bool mask whose top-left is (ox, oy)."""
    H, W = mask.shape
    if not pts:
        return
    a = np.asarray(pts, np.float32)
    if len(a) > 1:  # points every px along it
        seg = np.diff(a, axis=0)
        n = np.maximum(1, np.ceil(np.hypot(seg[:, 0], seg[:, 1])).astype(int))
        a = np.concatenate([a[i] + seg[i] * (np.arange(n[i])[:, None] / n[i]) for i in range(len(seg))] + [a[-1:]])
    r = int(np.ceil(radius))
    cx = np.round(a[:, 0] - ox).astype(int)
    cy = np.round(a[:, 1] - oy).astype(int)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= radius * radius:
                yy, xx = cy + dy, cx + dx
                ok = (yy >= 0) & (yy < H) & (xx >= 0) & (xx < W)
                mask[yy[ok], xx[ok]] = True


def fill(mask, rect, ox, oy, pad=0):
    H, W = mask.shape
    x, y, w, h = rect
    x0, y0 = max(0, int(x - pad - ox)), max(0, int(y - pad - oy))
    x1, y1 = min(W, int(x + w + pad - ox) + 1), min(H, int(y + h + pad - oy) + 1)
    if x0 < x1 and y0 < y1:
        mask[y0:y1, x0:x1] = True
