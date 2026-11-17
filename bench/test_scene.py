#!/usr/bin/python3
"""Scene boards (scene.py): expressions stay safe, live values interpolate, TeX comes back as outlines,
sliders and steps change what's drawn. Run: /usr/bin/python3 bench/test_scene.py (system python: cairo, Pango)."""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
_GI = os.path.expanduser(os.environ.get("CHALK_GI_SITE", ""))   # optional: a gi with cairo support, put first on sys.path
if _GI and os.path.isdir(_GI):
    sys.path.insert(0, _GI)
import scene  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    fails += not ok


fn, names = scene.compile_expr("1/sqrt(1+(f/fc)^2)")
check("expression evaluates", abs(fn({"f": 1.0, "fc": 1.0}) - 0.7071) < 1e-3)
check("expression names", names == {"f", "fc"}, names)
for bad in ("__import__('os')", "f.__class__", "[x for x in y]", "open('x')", "lambda: 1"):
    try:
        scene.compile_expr(bad)
        check(f"refuses {bad}", False)
    except (ValueError, SyntaxError):
        check(f"refuses {bad}", True)
check("bad math is nan, not a crash", scene.compile_expr("1/0")[0]({}) != scene.compile_expr("1/0")[0]({}))
check("interpolate formats", scene.interpolate("gain {g:.2f}", {"g": 0.70710}) == "gain 0.71")
check("interpolate leaves TeX braces", scene.interpolate("V_{out} = {g:.1f}", {"g": 2.0}, tex=True) == "V_{out} = {2.0}")
check("a live \\frac argument stays a group", scene.interpolate(r"\frac{N}{4}", {"N": 256.0}, tex=True) == r"\frac{256}{4}")
check("unknown names left alone", scene.interpolate("x_{in}", {}) == "x_{in}")
check("nice numbers", [scene.nice(v) for v in (995.2, 1000, 20000, 4.7e-8)] == ["995", "1k", "20k", "47n"],
      [scene.nice(v) for v in (995.2, 1000, 20000, 4.7e-8)])

check("readouts in engineering form", [scene.with_unit(*a) for a in ((1500, "kHz"), (0.002, "s"), (82, "%"))] == ["1.5 MHz", "2 ms", "82 %"])

t = scene.TEX.get(r"\frac{1}{2\pi RC}")
check("TeX -> outlines", bool(t and t["glyphs"] and t["w"] > 0), "is tex/node_modules installed? (cd tex && npm ci)")
check("TeX error -> None (falls back to text)", scene.TEX.get(r"\notacommand{x}") is None)

clock = [100.0]
scene.clock = lambda: clock[0]
spec = json.load(open(os.path.join(HERE, "visual2", "examples", "rc-lowpass.json")))
sc = scene.Scene(spec)
check("steps", len(sc.steps) == 3 and sc.step == 0)
surf, w, h, hits = sc.snapshot()
acts = [a for _, a in hits]
check("controls are click targets", "next" in acts and any(isinstance(a, tuple) and a[0] == "slider" for a in acts), acts)
sc.act("next"); sc.act("next")
check("Next walks the steps", sc.step == 2)
slider = next(a for a in acts if isinstance(a, tuple) and a[0] == "slider")
sc.drag_to(slider, slider[3] + 50, 0)
check("dragging past the end pins the max", sc.values["f"] == 20000)
sc.drag_to(slider, slider[2] - 50, 0)
check("dragging past the start pins the min", sc.values["f"] == 50)
e = sc.env(clock[0])
check("let values follow the slider", e["g"] > 0.99, e["g"])
clock[0] += 3
check("time-driven scene keeps animating", sc.animating(clock[0]))
clock[0] += scene.TIME_RUN + 5
check("...and stops after TIME_RUN untouched", not sc.animating(clock[0]))
sc.act("next")
check("Replay from the last step starts over", sc.step == 0)
e = sc.env(clock[0] + 0.5)
check("step and st in expressions", e["step"] == 1 and 0.4 < e["st"] < 0.6 and scene.compile_expr("lerp(0, 10, ease(st))")[0](e) > 0)

# symbols MathJax's text font lacks (θ in a plain label goes through \text{}) still get outlines, even far along
for src in (r"\text{L sin θ}", r"\text{%s θ}" % ("x" * 30)):
    with_sym, ascii_ = scene.TEX.get(src), scene.TEX.get(src.replace("θ", "q"))
    check(f"TeX keeps the θ in {src[:16]}", bool(with_sym and ascii_ and len(with_sym["glyphs"]) == len(ascii_["glyphs"])
                                             and with_sym["glyphs"][-1][2] > 0), with_sym and len(with_sym["glyphs"]))


def board(params=(), objects=(), size=(520, 320)):
    b = scene.Scene({"size": list(size), "params": list(params), "objects": list(objects),
                     "steps": [{"show": [o["id"] for o in objects]}]})
    clock[0] += 60   # everything has come in
    return b, b.snapshot()


lb, _ = board(objects=[{"id": "l", "line": [480, 150, 480, 250], "dash": True, "label": "L sin θ"}])
r = lb.placed[0] if lb.placed else None
check("a line's label near the right edge steps to the line's left", bool(r and r[0] + r[2] <= 480), r)
lb, _ = board(objects=[{"id": "l", "line": [100, 150, 100, 250], "label": "L sin θ"}])
r = lb.placed[0] if lb.placed else None
check("...and stays right of it when there's room", bool(r and r[0] >= 100), r)

long_label = "angle θ between the wire and the magnetic field lines, measured from the vertical"
for label, stacked in (("angle θ between wire and field", False), (long_label, True)):
    sb, (_, _, h_, hits_) = board(params=[{"name": "a", "label": label, "min": 0, "max": 1, "value": 0.5}])
    lay, lw, above = sb.slider_label(sb.params[0], scene.panel.measure_ctx())
    sl = next(a for _, a in hits_ if isinstance(a, tuple) and a[0] == "slider")
    check(f"slider label shown whole ({'own line' if stacked else 'beside the track'})",
          not lay.is_ellipsized() and bool(above) == stacked and abs(sl[2] - (scene.PAD + lw)) < 1e-6
          and (not stacked or lay.get_pixel_extents()[1].width <= sb.w - 2 * scene.PAD), (lw, above, sl))
_, (_, _, h_short, _) = board(params=[{"name": "a", "label": "a", "min": 0, "max": 1}])
check("a label on its own line makes the card taller by that line", h_ > h_short + 10, (h_, h_short))

bad = scene.Scene({"scene": {"objects": [{"plot": "1/(x", "on": "nope"}, {"tex": "\\frac{", "at": [0, 0]},
                                         {"circle": ["a", "b", "c"]}, {"bogus": 1}], "params": [{"name": "p", "min": 5, "max": 1}]}})
bad.snapshot()
check("broken objects don't sink the board", True)
print("all ok" if not fails else f"{fails} failed")
sys.exit(1 if fails else 0)
