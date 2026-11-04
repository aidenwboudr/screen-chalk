"""Offline check of panel.tex: the LaTeX the tutor writes between $…$ comes out as readable unicode.
Run with the system python (it has cairo/GI):  /usr/bin/python3 bench/test_tex.py"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import panel  # noqa: E402

CASES = [
    (r"\dfrac{\partial L}{\partial W1_{00}}", "∂L/∂W1₀₀"),        # a brace inside an argument (the quiz bug)
    (r"\frac{\partial L}{\partial w}", "∂L/∂w"),
    (r"\frac{\partial^2 L}{\partial w^2}", "∂²L/∂w²"),
    (r"\frac{1}{1+e^{-z}}", "1/(1+e⁻ᶻ)" if "z" in panel.SUP else "1/(1+e^(-z))"),
    (r"\frac{a+b}{2}", "(a+b)/2"),
    (r"\tfrac{1}{2}", "1/2"),
    (r"\frac{\frac{a}{b}}{c}", "(a/b)/c"),
    (r"\frac12", "1/2"),
    (r"\sigma'(z) \cdot w_1", "σ'(z) · w₁"),
    (r"\nabla_W L", "∇_W L"),
]

bad = [(src, got, want) for src, want in CASES if (got := panel.tex(src)) != want]
for src, got, want in bad:
    print(f"FAIL {src!r}: got {got!r}, want {want!r}")
print(f"{len(CASES) - len(bad)}/{len(CASES)} ok")
sys.exit(1 if bad else 0)
