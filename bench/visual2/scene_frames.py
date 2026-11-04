#!/usr/bin/python3
"""Render a scene board's frames offscreen on a virtual clock (no overlay, no compositor).

  scene_frames.py SPEC.json OUTDIR [--times 0.4,1.2,6] [--play]
Writes OUTDIR/<name>.png for: each time after it appears, each step after Next (settled), and each slider at
its min and max on the last step (`--play` adds the interactions). A storyboard strip of them goes to
OUTDIR/strip.png. The bench uses it on the scene the model wrote; it's also handy while writing scenes.
"""
import argparse, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))
_GI = os.path.expanduser(os.environ.get("CHALK_GI_SITE", ""))   # optional: a gi with cairo support, put first on sys.path
if _GI and os.path.isdir(_GI):
    sys.path.insert(0, _GI)
import cairo  # noqa: E402
import scene  # noqa: E402  (not overlay: it needs numpy)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec"); ap.add_argument("out")
    ap.add_argument("--times", default="0.5,1.2,6")
    ap.add_argument("--play", action="store_true")
    ap.add_argument("--from-answer", action="store_true", help="SPEC is a chalk ask answer.json (its sketch.scene)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    spec = json.load(open(a.spec))
    if a.from_answer:
        spec = {"scene": spec["sketch"]["scene"]}
    for p in scene.storyboard(spec, a.out, [float(t) for t in a.times.split(",")], a.play):
        print(p)


if __name__ == "__main__":
    main()
