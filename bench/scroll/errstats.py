#!/usr/bin/python3
"""How well does the target's picture match at its true spot vs the best look-alike elsewhere in its column?
(sets follow.py's thresholds; runs on replay.py's recordings)"""
import glob, os, sys
import numpy as np
sys.path.insert(0, os.environ.get("CHALK_DIR", "/chalk"))
import follow  # noqa: E402

for path in sorted(glob.glob("/out/rec-*.npz")):
    d = np.load(path)
    frames, truth = d["frames"], d["truth"]
    box, band, win = [tuple(map(float, d[k])) for k in ("box", "band", "win")]
    bx, by = int(band[0]), int(band[1])
    x, y, w, h = follow.template_rect(box, win)
    a = follow.Anchor(frames[0][y - by:y + h - by, x - bx:x + w - bx], np.ones((h, w), bool), (x, y, w, h))
    t0 = truth[0]
    true_e, rival_e = [], []
    for f, tr in zip(frames[1:], truth[1:]):
        if tr[2] < 0.9 * t0[2]:
            continue
        fr = follow.blur(f)
        ty = int(round(y + tr[1] - t0[1]))
        ys = np.arange(by - h // 2, by + f.shape[0] - h // 2)
        e = a.errors(fr, np.ones(f.shape, bool), bx, by, ys, [x])[:, 0]
        near = np.abs(ys - ty) <= 2
        far = np.abs(ys - ty) > 8
        true_e.append(e[near].min())
        rival_e.append(e[far].min())
    te, re_ = np.array(true_e), np.array(rival_e)
    print(f"{os.path.basename(path):14s} n={len(te):3d} true p50={np.median(te):.2f} p95={np.percentile(te, 95):.2f} max={te.max():.2f}"
          f" | rival min={re_.min():.2f} p5={np.percentile(re_, 5):.2f} p50={np.median(re_):.2f}"
          f" | gap p5={np.percentile(re_ - te, 5):.2f}")
