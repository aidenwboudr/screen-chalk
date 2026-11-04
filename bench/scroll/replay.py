#!/usr/bin/python3
"""Offline half of the scroll bench: record what each scene's scroll looks like once, then replay those
frames through follow.py as often as needed (seconds per try instead of a minute of live apps).

  replay.py record [--scenes ...]        in the container (run.sh-style): frames of the target's column
                                          while the bench script scrolls, plus where the marker was -> /out/rec-<scene>.npz
  replay.py eval [files...] [--min F]    follow.py on them: share of frames tracked within 4 px, error, ms per frame
                                          (--min / --max-ms: fail below / above; bench/scroll/check.sh)

No overlay runs while recording, so this scores the matching alone; bench.py scores the whole thing live.
"""
import argparse, glob, json, os, subprocess, sys, threading, time
import numpy as np

sys.path.insert(0, os.environ.get("CHALK_DIR", "/chalk"))
import follow  # noqa: E402


def record(args):
    import bench
    os.makedirs(bench.WORK, exist_ok=True)
    for name in args.scenes.split(","):
        bench.kill_apps()
        try:
            box, sign = bench.SCENES[name]()
        except (SystemExit, subprocess.CalledProcessError) as e:
            print(name, "failed:", e, flush=True)
            continue
        bench.drive([("move", bench.W // 2, bench.H // 2 + 40), ("sleep", 300)])
        tree = json.loads(subprocess.run(["swaymsg", "-r", "-t", "get_tree"], capture_output=True, text=True).stdout)
        wins = []

        def walk(n):
            if n.get("pid") and n.get("visible"):
                wins.append(n["rect"])
            for c in n.get("nodes", []) + n.get("floating_nodes", []):
                walk(c)
        walk(tree)
        win = [wins[0][k] for k in ("x", "y", "width", "height")]
        x0 = int(max(win[0], box[0] - 180)); x1 = int(min(win[0] + win[2], box[0] + box[2] + 180))
        band = (x0, win[1], x1 - x0, win[3])
        p = subprocess.Popen(["chalk-cap", "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        frames, times, truth = [], [], []
        done = threading.Event()
        threading.Thread(target=lambda: (bench.drive(bench.SCRIPT, sign), done.set()), daemon=True).start()
        first = True
        t_start = time.time()
        while not done.is_set() or first:
            p.stdin.write(f"{bench.OUTPUT} {band[0]} {band[1]} {band[2]} {band[3]} {0 if first else 1} 300 1\n".encode())
            p.stdin.flush()
            head = p.stdout.readline().split()
            if len(head) != 3:
                continue
            w, h, t = map(int, head)
            rgb = np.frombuffer(p.stdout.read(w * h * 3), np.uint8).reshape(h, w, 3)
            m = (rgb[:, :, 0] > 200) & (rgb[:, :, 1] < 60) & (rgb[:, :, 2] > 200)
            ys, xs = np.nonzero(m)
            truth.append((xs.mean() + band[0], ys.mean() + band[1], m.sum()) if len(xs) else (-1, -1, 0))
            frames.append(rgb[:, :, 1].copy())
            times.append(t)
            first = False
        p.kill()
        print(name, "drive took", round(time.time() - t_start, 1), "s", flush=True)
        np.savez_compressed(f"/out/rec-{name}.npz", frames=np.array(frames), times=np.array(times),
                            truth=np.array(truth), box=np.array(box), band=np.array(band), win=np.array(win))
        print(name, len(frames), "frames", flush=True)
    bench.kill_apps()


def evaluate(path, verbose=False):
    d = np.load(path)
    frames, times, truth = d["frames"], d["times"], d["truth"]
    box, band, win = [tuple(map(float, d[k])) for k in ("box", "band", "win")]
    bx, by = int(band[0]), int(band[1])
    rect = follow.template_rect(box, win)
    x, y, w, h = rect
    a = follow.Anchor(frames[0][y - by:y + h - by, x - bx:x + w - bx], np.ones((h, w), bool), rect)
    a.fix(x, y, 0.0, times[0])
    t0 = truth[0]
    n0 = t0[2]
    ok = np.ones(frames[0].shape, bool)
    good = vis = 0
    errs, ghosts, gone, cost = [], 0, 0, []
    H, W = frames[0].shape
    for f, t, tr in zip(frames[1:], times[1:], truth[1:]):
        c = time.perf_counter()
        # the piece of the frame the live Follower would grab: around where it should be, or its whole column
        column = a.lost or a.misses > 0 or getattr(a, "recheck", 0) > 0
        if column:
            cx0, cy0, cx1, cy1 = a.x - follow.REACH // 2 - 10, by, a.x + w + follow.REACH // 2 + 10, by + H
        else:
            rx0, ry0, rx1, ry1 = a.reach(t)
            cx0, cy0, cx1, cy1 = rx0 - follow.REACH // 2 - 10, ry0 - 2, rx1 + w + follow.REACH // 2 + 10, ry1 + h + 2
        cx0, cy0 = int(max(bx, cx0)), int(max(by, cy0))
        cx1, cy1 = int(min(bx + W, cx1)), int(min(by + H, cy1))
        piece = f[cy0 - by:cy1 - by, cx0 - bx:cx1 - bx]
        frame = follow.blur(piece)
        follow.track(a, frame, np.ones(piece.shape, bool), cx0, cy0, t, column)
        cost.append((time.perf_counter() - c) * 1000)
        if tr[2] >= 0.9 * n0:
            vis += 1
            want = (tr[0] - t0[0], tr[1] - t0[1])
            if not a.lost:
                e = float(np.hypot(a.shift()[0] - want[0], a.shift()[1] - want[1]))
                errs.append(e)
                good += e <= 4
            if verbose and (a.lost or errs[-1] > 4):
                print(f"  t={t - times[0]:6d} truth dy={want[1]:7.1f} mark dy={a.shift()[1]:7.1f} lost={a.lost} err={a.err:.2f}")
        elif tr[2] <= 0.1 * n0:
            gone += 1
            ghosts += not a.lost
            if verbose and not a.lost:
                print(f"  t={t - times[0]:6d} truth gone, mark at dy={a.shift()[1]:7.1f} err={a.err:.2f}")
    return {"frames": len(frames), "follow": round(good / max(1, vis), 3),
            "err_p50": round(float(np.median(errs)), 1) if errs else None,
            "err_p95": round(float(np.percentile(errs, 95)), 1) if errs else None,
            "ghost": round(ghosts / gone, 3) if gone else None, "ms": round(float(np.mean(cost)), 2),
            "ms_p95": round(float(np.percentile(cost, 95)), 2), "points": len(a.py)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["record", "eval"])
    ap.add_argument("files", nargs="*")
    ap.add_argument("--scenes", default="web,pdf,code,term")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--min", type=float, help="exit 1 when the mean share tracked is below this")
    ap.add_argument("--max-ms", type=float, help="exit 1 when the mean ms per frame is above this")
    args = ap.parse_args()
    if args.mode == "record":
        return record(args)
    res = {}
    for f in args.files or sorted(glob.glob("/out/rec-*.npz")):
        res[os.path.basename(f)] = r = evaluate(f, args.v)
        print(os.path.basename(f), json.dumps(r), flush=True)
    ok = list(res.values())
    follow_mean, ms = float(np.mean([r["follow"] for r in ok])), float(np.mean([r["ms"] for r in ok]))
    print("mean follow", round(follow_mean, 3), "ms", round(ms, 2))
    if (args.min and follow_mean < args.min) or (args.max_ms and ms > args.max_ms):
        print(f"FAIL: wanted follow >= {args.min} and ms <= {args.max_ms}")
        sys.exit(1)


if __name__ == "__main__":
    main()
