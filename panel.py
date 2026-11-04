"""Panels you can use: a chalk card that answers back.

A `panel` shape is a card in the tutor's one visual language (the Teach app's workspace
look: scene.py's dark Manim ground, the 3b1b colours, serif for what the tutor says) that, unlike every other mark, takes the mouse
and, when asked, the keyboard: multiple-choice and typed questions with instant right/wrong + why, Done/Stuck
steps, reveal-then-grade review cards, plain buttons, and code blocks with a Copy button. The overlay owns the
window, placement and the tracker; this module owns what's inside one panel: its state, its layout (which also
yields the click targets), and how it reacts to clicks and keys. Nothing here touches GTK windows.

Spec (`chalk draw` / `chalk panel`, JSON):
  {"kind": "panel", "tag": "q1", "title": "...", "sub": "header text", "text": "markdown-ish, ```code``` fences",
   "blocks": [{"text": "..."}, {"code": "...", "lang": "py"}],
   "questions": [{"kind": "choice|multi|text|number|task", "prompt": "...", "options": [...], "answer": 1,
                  "why": "...", "idk": true, "unit": "V", "placeholder": "...", "tolerance": 0.01}],
   "intro": "...", "reveal": "shown after Show answer", "buttons": [{"label": "Good", "value": "good", "key": "g"}],
   "box": [x, y, w, h] (what it's about: it sits beside it with a faint leader), "width": 380,
   "sent_label": "✓ Sent", "later_label": "Not now" (the line under an answered / put-aside set)}
The result (what `chalk panel --wait` prints) uses the Teach app's answer shapes:
  {"status": "answered", "answers": [{"choice": 1} | {"choices": [0, 2]} | {"idk": true} | {"text": "..."} |
   {"value": "0.5"} | {"task": "done"|"stuck"}], "button": "good"}   or {"status": "dismissed" | "gone"}
"""
import math, os, re, time

import cairo
import gi
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GLib, Pango, PangoCairo

THEME = None          # the overlay's THEME, set by setup()
# the hotkeys named in hints (install.sh --sway binds these; set them if you bound others)
KEY_FOCUS = os.environ.get("CHALK_KEY_FOCUS", "Super+;")
KEY_COPY = os.environ.get("CHALK_KEY_COPY", "Super+Ctrl+C")
HAND = "Source Serif 4, Noto Serif, DejaVu Serif, serif"    # what the tutor says (Teach's prose font)
TITLE = "Source Sans 3, Noto Sans, DejaVu Sans, sans-serif"   # titles and chrome
MONO = "monospace"    # fontconfig's alias: JetBrains Mono on the laptop
PAD = 16
HEADER = 28
DEFAULT_W = 380
XFADE = 0.22          # seconds a panel crossfades from one state to the next
COPIED = 1.4          # seconds a Copy button says "Copied"
LONG_TEXT = 280       # a card with more text than this gets a Copy button too
SCALE = 2             # offscreen render scale (the laptop runs at 1.5, GTK3 renders at 2 and scales down)

# the same surfaces and colour roles as Teach and scene.py's THEMES["dark"]: one tutor, one look
GROUND, CARD, CARD2 = (0.110, 0.110, 0.118), (0.161, 0.161, 0.176), (0.192, 0.192, 0.212)
EDGE, EDGE_STRONG, STRIP = (0.227, 0.227, 0.251), (0.30, 0.30, 0.33), (0.141, 0.141, 0.153)
INK, INK2 = (0.93, 0.93, 0.93), (0.65, 0.65, 0.67)
BLUE, GREEN, RED, YELLOW = (0.345, 0.769, 0.867), (0.514, 0.757, 0.404), (0.988, 0.384, 0.333), (0.941, 0.675, 0.373)
ON_BLUE = (0.055, 0.102, 0.118)   # text on a filled colour
PAPER_HOVER = CARD2
CODE_BG, CODE_EDGE = GROUND, EDGE


def setup(theme):
    global THEME
    THEME = theme


def tint(rgb, k=0.82):
    """A colour washed into the card's ground (k = how much ground): 0.85 is Teach's 15 % tint."""
    return tuple(c * (1 - k) + g * k for c, g in zip(rgb, GROUND))


def rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


# ---------- text: the tutor writes Markdown with a little LaTeX; Pango gets markup ----------

TEX = {r"\sigma": "σ", r"\partial": "∂", r"\eta": "η", r"\delta": "δ", r"\Delta": "Δ", r"\alpha": "α",
       r"\beta": "β", r"\theta": "θ", r"\lambda": "λ", r"\mu": "μ", r"\pi": "π", r"\sum": "Σ", r"\cdot": "·",
       r"\times": "×", r"\leftarrow": "←", r"\rightarrow": "→", r"\to": "→", r"\approx": "≈", r"\leq": "≤",
       r"\geq": "≥", r"\le": "≤", r"\ge": "≥", r"\neq": "≠", r"\infty": "∞", r"\nabla": "∇", r"\prime": "′",
       r"\quad": "  ", r"\,": " ", r"\;": " ", r"\!": "", r"\left": "", r"\right": "", r"\mathrm": "",
       r"\text": "", r"\hat": "", r"\in": "∈", r"\pm": "±", r"\sqrt": "√", r"\ln": "ln", r"\log": "log",
       r"\exp": "exp", r"\odot": "⊙", r"\top": "ᵀ", r"\Omega": "Ω", r"\omega": "ω", r"\phi": "φ",
       r"\epsilon": "ε", r"\varepsilon": "ε", r"\rho": "ρ", r"\tau": "τ", r"\ell": "ℓ", r"\dots": "…",
       r"\ldots": "…", r"\mathbf": "", r"\boldsymbol": "", r"\operatorname": ""}
SUP = dict(zip("0123456789+-=()nTi", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿᵀⁱ"))
SUB = dict(zip("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))


def _script(s, table, mark):
    return "".join(table[c] for c in s) if s and all(c in table for c in s) else f"{mark}({s})" if len(s) > 1 else mark + s


def _group(s, i):
    """The {…} group (braces balanced) or single character at s[i] -> (what's inside, index after it)."""
    while i < len(s) and s[i] == " ":
        i += 1
    if s[i:i + 1] != "{":
        return s[i:i + 1], i + 1
    depth = 0
    for j in range(i, len(s)):
        depth += {"{": 1, "}": -1}.get(s[j], 0)
        if depth == 0:
            return s[i + 1:j], j + 1
    return s[i + 1:], len(s)


def _over(a):
    return f"({a})" if len(a) > 1 and re.search(r"[\s+\-−=/·×±]", a) else a


def tex(s):
    """LaTeX (what the tutor writes between $…$) -> readable unicode: σ'(z), ∂L/∂w, w₁²."""
    while m := re.search(r"\\[dt]?frac(?![a-zA-Z])", s):   # \frac{∂L}{∂W1_{00}}: arguments may hold braces
        num, i = _group(s, m.end())
        den, j = _group(s, i)
        s = s[:m.start()] + f"{_over(tex(num))}/{_over(tex(den))}" + s[j:]
    for k in sorted(TEX, key=len, reverse=True):
        s = re.sub(re.escape(k) + r"(?![a-zA-Z])", lambda _m, v=TEX[k]: v, s)
    s = re.sub(r"\^\{([^{}]*)\}", lambda m: _script(m[1], SUP, "^"), s)
    s = re.sub(r"\^(\w)", lambda m: _script(m[1], SUP, "^"), s)
    s = re.sub(r"_\{([^{}]*)\}", lambda m: _script(m[1], SUB, "_"), s)
    s = re.sub(r"_(\w)", lambda m: _script(m[1], SUB, "_"), s)
    s = re.sub(r"([∂∇][⁰¹²³⁴⁵⁶⁷⁸⁹]*) +(?=\w)", r"\1", s)   # \partial L is ∂L, as LaTeX sets it
    return s.replace("{", "").replace("}", "").replace("\\", "")


def plain(s):
    """Markdown-ish -> plain text (what Copy puts on the clipboard for prose)."""
    s = re.sub(r"\$\$?([^$]+)\$\$?", lambda m: tex(m[1]), str(s or ""))
    return re.sub(r"\*\*([^*]+)\*\*|`([^`]+)`", lambda m: m[1] or m[2], s)


def markup(s):
    """Markdown-ish (bold, `code`, $math$) -> Pango markup."""
    out = []
    for part in re.split(r"(\$\$?[^$]+\$\$?|`[^`]+`|\*\*[^*]+\*\*)", str(s or "")):
        if not part:
            continue
        if part.startswith("$"):
            out.append(f"<i>{GLib.markup_escape_text(tex(part.strip('$')))}</i>")
        elif part.startswith("`"):
            out.append(f'<span face="{MONO}" size="smaller">{GLib.markup_escape_text(part[1:-1])}</span>')
        elif part.startswith("**"):
            out.append(f"<b>{GLib.markup_escape_text(part[2:-2])}</b>")
        else:
            out.append(GLib.markup_escape_text(part))
    return "".join(out)


def parse_blocks(text):
    """A card's text -> [{"text"}, {"code", "lang"}] split on ``` fences."""
    blocks = []
    for i, part in enumerate(re.split(r"```", str(text or ""))):
        if i % 2:
            lang, _, body = part.partition("\n")
            if not body and lang:               # ```one line```
                body, lang = lang, ""
            body = body.rstrip("\n")
            if body.strip():
                blocks.append({"code": body, "lang": lang.strip()})
        elif part.strip():
            blocks.append({"text": part.strip()})
    return blocks


def wants_panel(spec):
    """Does a plain `card` deserve to be a panel (code to copy, or a lot of text)?"""
    text = str(spec.get("text") or spec.get("body") or "")
    return not spec.get("image") and ("```" in text or len(text) > LONG_TEXT)


_M = None


def measure_ctx():
    global _M
    if _M is None:
        _M = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8))
    return _M


def make_layout(cr, text, font, size, width=None, weight=None, is_markup=True, wrap=True):
    lay = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription.from_string(font)
    fd.set_absolute_size(size * Pango.SCALE)
    if weight:
        fd.set_weight(weight)
    lay.set_font_description(fd)
    if width:
        lay.set_width(int(width * Pango.SCALE))
        lay.set_wrap(Pango.WrapMode.WORD_CHAR if wrap else Pango.WrapMode.CHAR)
    if is_markup:
        try:
            lay.set_markup(text, -1)
        except GLib.Error:
            lay.set_text(text, -1)
    else:
        lay.set_text(text, -1)
    return lay


# ---------- the panel ----------

class Panel:
    def __init__(self, spec):
        self.spec = spec
        self.title = spec.get("title")
        self.sub = spec.get("sub")
        blocks = list(spec.get("blocks") or [])
        if spec.get("text") or spec.get("body"):
            blocks = parse_blocks(spec.get("text") or spec.get("body")) + blocks
        self.blocks = [b for b in blocks if isinstance(b, dict) and (b.get("text") or b.get("code"))]
        self.questions = [q for q in (spec.get("questions") or []) if isinstance(q, dict) and q.get("prompt")]
        self.intro = spec.get("intro")
        self.reveal = spec.get("reveal")
        self.buttons = [b for b in (spec.get("buttons") or []) if isinstance(b, dict) and b.get("label")]
        n = len(self.questions)
        self.page = 0
        self.answers = [None] * n
        self.inputs = [""] * n
        self.picks = [set() for _ in range(n)]   # multi, before Done
        self.feedback = [False] * n
        self.revealed = not self.reveal
        self.button = None
        self.holding = False      # all answered, "hold": waiting for their Next
        self.status = None        # "answered" / "dismissed" once it has reported
        self.result = None
        self.focused = False      # has the keyboard
        self.hover = None
        self.copied = {}          # copy target -> when
        self.sel = None           # copy target picked with Tab
        self.touched = 0.0        # last click/key: the tracker leaves a panel alone while it's in use
        self.version = 0
        self.prev = None          # (surface, w, h, t) of the state it is crossfading from
        self.cache = None
        texts = sum(len(b.get("text", "")) for b in self.blocks)
        self.targets = [i for i, b in enumerate(self.blocks) if b.get("code")]
        if not self.targets and (texts > LONG_TEXT or spec.get("copy")) and self.blocks:
            self.targets = ["all"]
        self.w = float(spec.get("width") or spec.get("card_width") or self.natural_width())

    # --- what it is ---

    @property
    def pending(self):
        """Waiting on them: a pending panel survives a plain redraw (the tutor's next marks)."""
        return self.status is None and bool(self.questions or self.buttons)

    @property
    def interactive(self):
        return True

    def natural_width(self):
        w = DEFAULT_W
        cr = measure_ctx()
        for b in self.blocks:
            if b.get("code"):
                for line in b["code"].split("\n")[:80]:
                    lw = make_layout(cr, line, MONO, 14, is_markup=False).get_pixel_extents()[1].width
                    w = max(w, lw + 2 * PAD + 26)
        if any(len(o) > 40 for q in self.questions for o in q.get("options") or []):
            w = max(w, 440)
        return min(w, 680)

    def copy_text(self, t):
        if t == "all":
            return "\n\n".join(plain(b["text"]) if b.get("text") else b["code"] for b in self.blocks)
        return self.blocks[t]["code"]

    def touch(self, major=False):
        self.touched = time.monotonic()
        if major and self.cache:
            self.prev = (self.cache[0], self.cache[1], self.cache[2], time.monotonic())
        self.version += 1

    def animating(self, now):
        return (self.prev is not None and now - self.prev[3] < XFADE) or any(now - t < COPIED + 0.05 for t in self.copied.values())

    def header_text(self):
        bits = [self.sub] if self.sub else []
        if self.focused:
            bits.append(("Ctrl+C copy · Tab next · " if self.targets and not self.questions else "") + "Esc: back to your work")
        elif self.pending:
            bits.append(f"{KEY_FOCUS} keys")
        elif self.targets:
            bits.append(f"{KEY_COPY} copies")
        return "  ·  ".join(bits)

    # --- drawing (the same walk lays it out and yields the click targets) ---

    def snapshot(self):
        """(surface, w, h, hits): the panel rendered offscreen, cached until it changes. The surface has a
        MARGIN of room for the shadow on every side."""
        key = (self.version, self.focused, self.hover, self.sel)
        now = time.monotonic()
        live_copy = tuple(t for t, at in self.copied.items() if now - at < COPIED)
        if self.cache and self.cache[4] == key + (live_copy,):
            return self.cache[:4]
        h, hits = self.flow(measure_ctx(), False)
        M = MARGIN
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(math.ceil((self.w + 2 * M) * SCALE)), int(math.ceil((h + 2 * M) * SCALE)))
        surf.set_device_scale(SCALE, SCALE)
        cr = cairo.Context(surf)
        cr.translate(M, M)
        self.paint_frame(cr, h)
        self.flow(cr, True)
        self.cache = (surf, self.w, h, hits, key + (live_copy,))
        return self.cache[:4]

    def paint_frame(self, cr, h):
        T, w = THEME, self.w
        rounded(cr, 2, 5, w, h, 10)
        cr.set_source_rgba(0, 0, 0, 0.45); cr.fill()   # a dark card needs a stronger drop over a dark editor
        rounded(cr, 0, 0, w, h, 10)
        cr.set_source_rgba(*GROUND, 1.0); cr.fill_preserve()
        cr.set_source_rgba(*(BLUE if self.focused else EDGE_STRONG), 1.0); cr.set_line_width(1.0)
        cr.stroke()
        cr.save()
        rounded(cr, 0, 0, w, h, 10)
        cr.clip()
        cr.set_source_rgba(*STRIP, 1)
        cr.rectangle(0, 0, w, HEADER - 2); cr.fill()
        cr.set_source_rgba(*EDGE, 1); cr.set_line_width(1)
        cr.move_to(0, HEADER - 2); cr.line_to(w, HEADER - 2); cr.stroke()
        cr.restore()

    def flow(self, cr, paint):
        """Walk the panel top to bottom; paint when asked. Returns (height, [(rect, action)])."""
        T, w = THEME, self.w
        hits = []
        ink, body = INK, INK2
        inner = w - 2 * PAD

        def text(yy, s, size=18, color=body, x=PAD, width=None, weight=None, font=HAND, alpha=1.0, is_markup=True):
            lay = make_layout(cr, s, font, size, width or (w - x - PAD), weight, is_markup)
            ext = lay.get_pixel_extents()[1]
            if paint:
                cr.move_to(x, yy)
                cr.set_source_rgba(*color, alpha)
                PangoCairo.show_layout(cr, lay)
            return ext.height, ext.width

        def pill(x, y0, label, action, primary=False, color=BLUE, size=16, minw=0):
            lay = make_layout(cr, label, TITLE, round(size * 0.82), weight=Pango.Weight.MEDIUM)
            ext = lay.get_pixel_extents()[1]
            pw, ph = max(minw, ext.width + 26), ext.height + 12
            if paint:
                hov = self.hover == action
                rounded(cr, x, y0, pw, ph, ph / 2)
                if primary:
                    cr.set_source_rgba(*(c * (0.85 if hov else 1.0) for c in color), 1); cr.fill()
                    cr.set_source_rgba(*ON_BLUE, 1)
                else:
                    cr.set_source_rgba(*(PAPER_HOVER if hov else tint(color, 0.9)), 1); cr.fill_preserve()
                    cr.set_source_rgba(*color, 0.75); cr.set_line_width(1.3); cr.stroke()
                    cr.set_source_rgba(*ink, 1)
                cr.move_to(x + (pw - ext.width) / 2 - ext.x, y0 + 6 - ext.y)
                PangoCairo.show_layout(cr, lay)
            hits.append(((x, y0, pw, ph), action))
            return pw, ph

        # header: red dot closes, yellow dot tucks it away; the right side says what it is and the keys
        for i, (act, c) in enumerate((("close", (0.878, 0.424, 0.384)), ("dock", (0.886, 0.714, 0.290)), (None, (0.475, 0.722, 0.494)))):
            cx = 16 + i * 14
            if paint:
                cr.new_sub_path(); cr.arc(cx, 12, 4.4 if self.hover == act and act else 4, 0, 2 * math.pi)
                cr.set_source_rgba(*c, 0.9); cr.fill()
                if act and self.hover == act:
                    cr.set_source_rgba(0.25, 0.15, 0.1, 0.8); cr.set_line_width(1.2)
                    if act == "close":
                        cr.move_to(cx - 1.8, 10.2); cr.line_to(cx + 1.8, 13.8); cr.move_to(cx + 1.8, 10.2); cr.line_to(cx - 1.8, 13.8)
                    else:
                        cr.move_to(cx - 2, 12); cr.line_to(cx + 2, 12)
                    cr.stroke()
            if act:
                hits.append(((cx - 7, 5, 14, 14), act))
        ht = self.header_text()
        if ht:
            lay = make_layout(cr, GLib.markup_escape_text(ht), MONO, 11.5)
            ext = lay.get_pixel_extents()[1]
            if paint:
                cr.move_to(max(64, w - PAD - ext.width), (HEADER - 2 - ext.height) / 2 - ext.y)
                cr.set_source_rgba(*body, 0.62)
                PangoCairo.show_layout(cr, lay)

        y = HEADER + 12
        if self.title:
            th, _ = text(y, markup(self.title), 15.5, ink, weight=Pango.Weight.SEMIBOLD, font=TITLE)
            y += th + 6

        for bi, b in enumerate(self.blocks):
            if b.get("code"):
                # a slim strip on top (language, Copy), then the code; lines too long for the panel wrap
                lay = make_layout(cr, b["code"], MONO, 14, inner - 20, is_markup=False)
                ext = lay.get_pixel_extents()[1]
                strip = 26
                bh = strip + ext.height + 16
                if paint:
                    rounded(cr, PAD, y, inner, bh, 8)
                    cr.set_source_rgba(*CODE_BG, 1); cr.fill_preserve()
                    sel = self.sel == bi
                    cr.set_source_rgba(*(BLUE if sel else CODE_EDGE), 1); cr.set_line_width(1.8 if sel else 1.1); cr.stroke()
                    cr.set_source_rgba(*CODE_EDGE, 0.8); cr.set_line_width(1)
                    cr.move_to(PAD + 1, y + strip); cr.line_to(PAD + inner - 1, y + strip); cr.stroke()
                    n = self.targets.index(bi) + 1 if len(self.targets) > 1 else None
                    tag = make_layout(cr, " · ".join(x for x in (b.get("lang") or "code", f"{n} of {len(self.targets)}" if n else "") if x), MONO, 12, is_markup=False)
                    te = tag.get_pixel_extents()[1]
                    cr.move_to(PAD + 10, y + (strip - te.height) / 2)
                    cr.set_source_rgba(*body, 0.6)
                    PangoCairo.show_layout(cr, tag)
                    cr.move_to(PAD + 10, y + strip + 8)
                    cr.set_source_rgba(*ink, 1)
                    PangoCairo.show_layout(cr, lay)
                self.copy_button(cr, paint, hits, bi, PAD + inner - 4, y + 2, pill)
                y += bh + 10
            else:
                th, _ = text(y, markup(b["text"]), 15)
                y += th + 8
        if self.targets == ["all"]:
            self.copy_button(cr, paint, hits, "all", w - PAD, y - 2, pill)
            y += 30

        if self.reveal:
            if self.revealed:
                if paint:
                    cr.set_source_rgba(*EDGE_STRONG, 1); cr.set_line_width(1)
                    cr.move_to(PAD, y + 2); cr.line_to(w - PAD, y + 2); cr.stroke()
                y += 10
                th, _ = text(y, markup(self.reveal), 15, ink)
                y += th + 10
            else:
                _, ph = pill(PAD, y, "Show answer   Space", "reveal")
                y += ph + 10

        if self.questions:
            y = self.flow_question(cr, paint, hits, y, text, pill)

        if self.buttons and self.revealed:
            x = PAD
            for b in self.buttons:
                lab = b["label"] + (f"   {b['key'].upper()}" if b.get("key") else "")
                picked = self.button == b.get("value", b["label"])
                if self.status and not picked:
                    continue
                pw, ph = pill(x, y, ("✓ " if picked else "") + lab, ("button", b.get("value", b["label"])),
                              primary=picked, color={"red": RED, "green": GREEN}.get(b.get("color"), BLUE))
                x += pw + 8
            y += 34
        return y + PAD - 4, hits

    def copy_button(self, cr, paint, hits, target, right, top, pill):
        done = time.monotonic() - self.copied.get(target, -9) < COPIED
        label = "Copied ✓" if done else "Copy"
        lay = make_layout(cr, label, TITLE, 12, weight=Pango.Weight.MEDIUM)
        pw = lay.get_pixel_extents()[1].width + 20
        x = right - pw
        if paint:
            hov = self.hover == ("copy", target)
            ph = 22
            rounded(cr, x, top, pw, ph, 11)
            cr.set_source_rgba(*(tint(GREEN, 0.85) if done else PAPER_HOVER if hov else GROUND), 1); cr.fill_preserve()
            cr.set_source_rgba(*(GREEN if done else CODE_EDGE), 1); cr.set_line_width(1.1); cr.stroke()
            ext = lay.get_pixel_extents()[1]
            cr.move_to(x + 10 - ext.x, top + (ph - ext.height) / 2 - ext.y)
            cr.set_source_rgba(*(GREEN if done else INK2), 1)
            PangoCairo.show_layout(cr, lay)
        hits.append(((x, top, pw, 22), ("copy", target)))

    def after_label(self):
        """The line under an answered (or put-aside) set: the spec's own words, Teach's for Teach's panels."""
        teach = str(self.spec.get("tag", "")).startswith("teach-")
        if self.status == "answered":
            return self.spec.get("sent_label") or ("✓ Sent to your tutor" if teach else "✓ Sent")
        return self.spec.get("later_label") or ("Not now: it's still in Teach" if teach else "Not now")

    def flow_question(self, cr, paint, hits, y, text, pill):
        T, w = THEME, self.w
        ink, body = INK, INK2
        i = self.page
        q = self.questions[i]
        n = len(self.questions)
        kind = q.get("kind", "choice")
        a = self.answers[i]
        fb = self.feedback[i]
        if self.intro and i == 0:
            th, _ = text(y, markup(self.intro), 16, body, alpha=0.8)
            y += th + 6
        if n > 1:
            th, _ = text(y, f"Question {i + 1} of {n}", 14, body, alpha=0.6, is_markup=False)
            y += th + 2
        th, _ = text(y, markup(q["prompt"]), 16.5, ink)
        y += th + 10
        inner = w - 2 * PAD
        if kind in ("choice", "multi"):
            opts = list(q.get("options") or [])
            rows = [(j, o) for j, o in enumerate(opts)]
            if q.get("idk", True):
                rows.append(("idk", "I don't know"))
            for j, o in rows:
                key = "0" if j == "idk" else str(j + 1)
                lay = make_layout(cr, markup(o), HAND, 14.5, inner - 52)
                ext = lay.get_pixel_extents()[1]
                rh = max(30, ext.height + 12)
                act = ("idk", i) if j == "idk" else ("choice", i, j)
                picked = (a or {}).get("idk") if j == "idk" else (j == (a or {}).get("choice") or j in (a or {}).get("choices", []) or (kind == "multi" and not a and j in self.picks[i]))
                state = "picked" if picked else ""
                if fb and "answer" in q:
                    keyed = j != "idk" and (j == q["answer"] if kind == "choice" else j in (q["answer"] or []))
                    state = "right" if picked and keyed else "wrong" if picked else "missed" if keyed else "dim"
                if paint:
                    hov = self.hover == act and not a
                    fill = {"picked": tint(BLUE, 0.85), "right": tint(GREEN, 0.8), "wrong": tint(RED, 0.82)}.get(state, PAPER_HOVER if hov else CARD)
                    edge = {"picked": BLUE, "right": GREEN, "wrong": RED, "missed": GREEN}.get(state, EDGE_STRONG)
                    rounded(cr, PAD, y, inner, rh, 6)
                    cr.set_source_rgba(*fill, 0.55 if state in ("", "dim") and not hov else 0.95); cr.fill_preserve()
                    if state == "missed":
                        cr.set_dash([5, 4])
                    cr.set_source_rgba(*edge, 0.9 if state else 0.8); cr.set_line_width(1.6 if state and state != "dim" else 1.1); cr.stroke()
                    cr.set_dash([])
                    # the key chip: a circle for one pick, a square for several
                    cx, cy = PAD + 17, y + rh / 2
                    if kind == "multi" and j != "idk":
                        rounded(cr, cx - 9, cy - 9, 18, 18, 4)
                    else:
                        cr.new_sub_path(); cr.arc(cx, cy, 9.5, 0, 2 * math.pi)
                    cr.set_source_rgba(*(edge if state and state != "dim" else EDGE_STRONG), 0.95 if state else 0.7); cr.fill()
                    kl = make_layout(cr, key, MONO, 11, is_markup=False)
                    ke = kl.get_pixel_extents()[1]
                    cr.move_to(cx - ke.width / 2 - ke.x, cy - ke.height / 2 - ke.y)
                    cr.set_source_rgba(*(ON_BLUE if state and state != "dim" else ink), 1)
                    PangoCairo.show_layout(cr, kl)
                    cr.move_to(PAD + 36, y + (rh - ext.height) / 2)
                    cr.set_source_rgba(*ink, 0.55 if state == "dim" or j == "idk" and not picked else 1)
                    PangoCairo.show_layout(cr, lay)
                    mark = {"right": "✓", "wrong": "✗", "missed": "✓"}.get(state)
                    if mark:
                        ml = make_layout(cr, mark, HAND, 18, is_markup=False)
                        me = ml.get_pixel_extents()[1]
                        cr.move_to(PAD + inner - 12 - me.width, y + (rh - me.height) / 2 - me.y)
                        cr.set_source_rgba(*(GREEN if mark == "✓" else RED), 1)
                        PangoCairo.show_layout(cr, ml)
                if not a and not self.status:
                    hits.append(((PAD, y, inner, rh), act))
                y += rh + 6
            if kind == "multi" and not a:
                _, ph = pill(PAD, y + 2, "Done   Enter", ("multi", i), primary=True)
                y += ph + 8
        elif kind in ("text", "number"):
            val = self.inputs[i] if not a else (a.get("text") or a.get("value") or ("I don't know" if a.get("idk") else ""))
            mono = make_layout(cr, GLib.markup_escape_text(val) if val else "", MONO, 15, inner - 24 - (40 if q.get("unit") else 0))
            ext = mono.get_pixel_extents()[1]
            ih = max(36, ext.height + 16)
            editing = self.focused and not a
            if paint:
                rounded(cr, PAD, y, inner, ih, 8)
                cr.set_source_rgba(*CARD, 1 if not a else 0.5); cr.fill_preserve()
                cr.set_source_rgba(*(BLUE if editing else EDGE_STRONG), 1); cr.set_line_width(1.8 if editing else 1.2); cr.stroke()
                if val:
                    cr.move_to(PAD + 12, y + 8)
                    cr.set_source_rgba(*ink, 1)
                    PangoCairo.show_layout(cr, mono)
                else:
                    ph_text = q.get("placeholder") or ("a number" if kind == "number" else "your answer")
                    hint = f"{ph_text}  ·  Enter sends" if editing else f"{ph_text}  ·  click here to type"
                    pl = make_layout(cr, GLib.markup_escape_text(hint), HAND, 16)
                    pe = pl.get_pixel_extents()[1]
                    cr.move_to(PAD + 12, y + (ih - pe.height) / 2 - pe.y)
                    cr.set_source_rgba(*body, 0.45)
                    PangoCairo.show_layout(cr, pl)
                if editing:  # a still caret (a blinking one would repaint the screen twice a second)
                    lines = mono.get_line_count()
                    last = mono.get_line_readonly(max(0, lines - 1))
                    lx = (last.get_pixel_extents()[1].width if val else 0) + PAD + 13
                    ly = y + 8 + (ext.height - 20 if val else (ih - 36) / 2 + 0)
                    cr.set_source_rgba(*BLUE, 0.9); cr.set_line_width(1.6)
                    cr.move_to(lx, ly + 2); cr.line_to(lx, ly + 20); cr.stroke()
                if q.get("unit"):
                    ul = make_layout(cr, GLib.markup_escape_text(q["unit"]), MONO, 14)
                    ue = ul.get_pixel_extents()[1]
                    cr.move_to(PAD + inner - 12 - ue.width, y + (ih - ue.height) / 2 - ue.y)
                    cr.set_source_rgba(*body, 0.7)
                    PangoCairo.show_layout(cr, ul)
            if not a and not self.status:
                hits.append(((PAD, y, inner, ih), ("input", i)))
            y += ih + 8
            if not a and not self.status:
                x = PAD
                if self.inputs[i].strip():
                    pw, ph = pill(x, y, "Send   Enter", ("send", i), primary=True)
                    x += pw + 8
                if q.get("idk", kind == "number"):
                    pill(x, y, "I don't know", ("idk", i))
                y += 36
        elif kind == "task":
            if not a:
                pw, ph = pill(PAD, y, "Done   D", ("task", i, "done"), primary=True, color=GREEN)
                pill(PAD + pw + 8, y, "Stuck: a hint   S", ("task", i, "stuck"))
                y += ph + 8
            else:
                th, _ = text(y, "✓ Done" if a.get("task") == "done" else "Asked for a hint", 17, GREEN if a.get("task") == "done" else YELLOW, is_markup=False)
                y += th + 8

        if fb and "answer" in q and kind in ("choice", "multi", "number"):
            ok = self.correct(i)
            lead = "✓ Right." if ok else ("✗ Not quite." if kind != "number" else f"✗ Not quite: it's {q['answer']}{(' ' + q['unit']) if q.get('unit') else ''}.")
            th, _ = text(y, GLib.markup_escape_text(lead), 18, GREEN if ok else RED)
            y += th + 2
            if q.get("why"):
                th, _ = text(y, markup(q["why"]), 17, body)
                y += th + 6
        # what's next
        if self.status:
            th, _ = text(y, self.after_label(), 15, GREEN if self.status == "answered" else body, alpha=0.85, is_markup=False)
            y += th + 4
        elif self.holding:
            _, ph = pill(PAD, y + 2, self.spec.get("next_label") or "Next ›   Enter", "finish", primary=True)
            y += ph + 8
        elif a is not None and i < n - 1:
            _, ph = pill(PAD, y + 2, "Next ›   Enter", "next", primary=True)
            y += ph + 8
        elif not a:
            if kind in ("choice", "multi"):
                k = len(q.get("options") or [])
                hint = f"1–{k} pick" + ("  ·  0 don't know" if q.get("idk", True) else "")
            elif kind == "task":
                hint = "D done  ·  S stuck"
            else:
                hint = "Enter sends" + ("  ·  Shift+Enter new line" if kind == "text" else "")
            th, _ = text(y, GLib.markup_escape_text(hint + ("" if self.focused else f"  ·  keys: {KEY_FOCUS}")), 14, body, alpha=0.55)
            y += th + 2
        return y

    # --- answers ---

    def correct(self, i):
        q, a = self.questions[i], self.answers[i] or {}
        if a.get("idk") or "answer" not in q:
            return False if a.get("idk") else None
        kind = q.get("kind", "choice")
        if kind == "choice":
            return a.get("choice") == q["answer"]
        if kind == "multi":
            return sorted(a.get("choices", [])) == sorted(q["answer"] or [])
        if kind == "number":
            try:
                v = float(str(a.get("value", "")).replace(",", ""))
                tol = float(q.get("tolerance") or abs(float(q["answer"])) * 0.01 or 1e-9)
                return abs(v - float(q["answer"])) <= tol
            except ValueError:
                return False
        return None

    def answer(self, i, a):
        """Record an answer; graded ones show right/wrong + why, the last one sends the set."""
        if self.answers[i] is not None or self.status:
            return []
        self.answers[i] = a
        q = self.questions[i]
        graded = "answer" in q and q.get("kind", "choice") in ("choice", "multi", "number")
        self.feedback[i] = graded
        out = []
        if i == len(self.questions) - 1 or all(x is not None for x in self.answers):
            if self.spec.get("hold"):   # the user reads the why, then moves on by themselves
                self.holding = True
            else:
                out += self.finish({"status": "answered", "answers": self.answers})
        elif not graded:
            self.page = i + 1          # nothing to read here: straight on to the next question
        self.touch(major=True)
        return out + ["redraw"]

    def finish(self, result):
        if self.status:
            return []
        self.status = result["status"]
        self.result = result
        out = [("result", result)]
        if self.focused:
            out.append("unfocus")
        return out

    # --- input ---

    def act(self, action):
        """A click (or its key) on one of the panel's targets -> effects for the overlay."""
        self.touched = time.monotonic()
        if action == "close":
            return self.finish({"status": "dismissed"}) + ["close"]
        if action == "dock":
            return ["dock"]
        if action == "reveal":
            self.revealed = True
            self.touch(major=True)
            return ["redraw"]
        if action == "finish" and self.holding:
            self.holding = False
            return self.finish({"status": "answered", "answers": self.answers}) + ["redraw"]
        if action == "next":
            if self.page < len(self.questions) - 1 and self.answers[self.page] is not None:
                self.page += 1
                self.touch(major=True)
            return ["redraw"]
        if isinstance(action, tuple):
            k = action[0]
            if k == "copy":
                self.copied[action[1]] = time.monotonic()
                self.touch()
                return [("copy", self.copy_text(action[1]), action[1])]
            if k == "choice":
                _, i, j = action
                if self.questions[i].get("kind") == "multi":
                    self.picks[i] ^= {j}
                    self.touch()
                    return ["redraw"]
                return self.answer(i, {"choice": j})
            if k == "idk":
                return self.answer(action[1], {"idk": True})
            if k == "multi":
                return self.answer(action[1], {"choices": sorted(self.picks[action[1]])})
            if k == "task":
                return self.answer(action[1], {"task": action[2]})
            if k == "send":
                i = action[1]
                v = self.inputs[i].strip()
                if not v:
                    return []
                return self.answer(i, {"value": v} if self.questions[i].get("kind") == "number" else {"text": v})
            if k == "input":
                return ["focus"]
            if k == "button":
                self.button = action[1]
                self.touch(major=True)
                return self.finish({"status": "answered", "button": action[1]}) + ["redraw"]
        return []

    def key(self, name, char, ctrl=False, shift=False):
        """A key while the panel has the keyboard. name = Gdk key name, char = its text."""
        self.touched = time.monotonic()
        if name == "Escape":
            return ["unfocus"]
        if ctrl and name.lower() == "c":
            t = self.sel if self.sel is not None else (self.targets[0] if self.targets else None)
            return self.act(("copy", t)) if t is not None else []
        if name in ("Tab", "ISO_Left_Tab") and self.targets:
            order = self.targets
            cur = order.index(self.sel) if self.sel in order else -1
            self.sel = order[(cur + (-1 if shift else 1)) % len(order)]
            self.touch()
            return ["redraw"]
        if ctrl and name.lower() == "w":
            return self.act("close")
        q = self.questions[self.page] if self.questions else None
        i = self.page
        kind = q.get("kind", "choice") if q else None
        typing = q is not None and kind in ("text", "number") and self.answers[i] is None and not self.status
        if typing:
            if name in ("Return", "KP_Enter"):
                if shift and kind == "text":
                    self.inputs[i] += "\n"
                    self.touch()
                    return ["redraw"]
                return self.act(("send", i))
            if name == "BackSpace":
                s = self.inputs[i]
                self.inputs[i] = re.sub(r"\S*\s*$", "", s) if ctrl else s[:-1]
                self.touch()
                return ["redraw"]
            if ctrl and name.lower() == "v":
                return ["paste"]
            if char and char.isprintable() and not ctrl:
                self.inputs[i] += char
                self.touch()
                return ["redraw"]
            return []
        if name in ("Return", "KP_Enter", "Right"):
            if q and kind == "multi" and self.answers[i] is None:
                return self.act(("multi", i))
            if self.holding:
                return self.act("finish")
            if q and self.answers[i] is not None and self.page < len(self.questions) - 1:
                return self.act("next")
            if self.status:
                return ["unfocus"]
            return []
        if name == "space" and not self.revealed:
            return self.act("reveal")
        if q and self.answers[i] is None and not self.status:
            if kind in ("choice", "multi") and char and char.isdigit():
                n = int(char)
                if n == 0 and q.get("idk", True):
                    return self.act(("idk", i))
                if 1 <= n <= len(q.get("options") or []):
                    return self.act(("choice", i, n - 1))
            if kind == "task" and char and char.lower() in "ds":
                return self.act(("task", i, "done" if char.lower() == "d" else "stuck"))
        if self.revealed and char:
            for b in self.buttons:
                if b.get("key") and char.lower() == b["key"].lower() and not self.status:
                    return self.act(("button", b.get("value", b["label"])))
        return []

    def paste(self, text):
        q = self.questions[self.page] if self.questions else None
        if q and self.answers[self.page] is None:
            self.inputs[self.page] += text if q.get("kind") == "text" else text.strip()
            self.touch()

    def hit(self, x, y):
        _, _, _, hits = self.snapshot()
        for (hx, hy, hw, hh), action in reversed(hits):
            if hx <= x <= hx + hw and hy <= y <= hy + hh:
                return action
        return None


MARGIN = 8   # room around the panel in its snapshot for the shadow
