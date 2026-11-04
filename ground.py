"""Grounding: turn what an agent says ("circle t12", "underline 'cutoff frequency'")
into layout-pixel boxes. Pure Python, no GUI deps, so the bench can import it too.

A *look* is {"size": [W, H], "elements": [{"id", "text", "box", "words": [[text, x, y, w, h], ...]}]}
built from tesseract's TSV of the screen.
"""
import difflib, re

# ---------- OCR -> elements ----------

def parse_tsv(tsv, scale=1.0, ox=0.0, oy=0.0, min_conf=30):
    """tesseract TSV -> words grouped into lines, lines split at wide gaps (separate buttons on one row)."""
    lines = {}
    for row in tsv.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or c[0] != "5":
            continue
        text = c[11].strip()
        if not text or float(c[10]) < min_conf:
            continue
        x, y, w, h = (int(v) * scale for v in c[6:10])
        key = (int(c[1]), int(c[2]), int(c[3]), int(c[4]))
        lines.setdefault(key, []).append([text, round(ox + x, 1), round(oy + y, 1), round(w, 1), round(h, 1)])
    elements = []
    for key in sorted(lines):
        words = sorted(lines[key], key=lambda w: w[1])
        group = [words[0]]
        for w in words[1:]:
            prev = group[-1]
            gap = w[1] - (prev[1] + prev[3])
            hgt = max(prev[4], w[4], 8)
            if gap > 1.6 * hgt:
                elements.append(_element(group)); group = [w]
            else:
                group.append(w)
        elements.append(_element(group))
    elements = [e for e in elements if _is_real(e)]
    elements.sort(key=lambda e: (round(e["box"][1] / 8), e["box"][0]))
    for i, e in enumerate(elements, 1):
        e["id"] = f"t{i}"
    return elements

def _is_real(e):
    """Drop tesseract noise: single punctuation crumbs and boxes that are mostly non-word characters."""
    t = e["text"]
    alnum = sum(ch.isalnum() for ch in t)
    return alnum >= 1 and (alnum >= 2 or len(t) == 1)

def _element(words):
    x0 = min(w[1] for w in words); y0 = min(w[2] for w in words)
    x1 = max(w[1] + w[3] for w in words); y1 = max(w[2] + w[4] for w in words)
    return {"text": " ".join(w[0] for w in words), "box": [round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)],
            "words": words}

def element_listing(elements, limit=400):
    """Compact text listing for a model: `t12 [x,y,w,h] "text"`."""
    out = []
    for e in elements[:limit]:
        x, y, w, h = (int(round(v)) for v in e["box"])
        out.append(f'{e["id"]} [{x},{y},{w},{h}] "{e["text"]}"')
    return "\n".join(out)


# ---------- matching ----------

def norm(s):
    return re.sub(r"[^\w%&+#.-]+", " ", s.lower()).strip()

def union(boxes):
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes); y1 = max(b[1] + b[3] for b in boxes)
    return [x0, y0, x1 - x0, y1 - y0]

def span_in(words, phrase):
    """Best run of consecutive words matching phrase -> (score, box)."""
    want = norm(phrase)
    if not want:
        return 0.0, None
    n_want = len(want.split())
    best = (0.0, None)
    for n in range(max(1, n_want - 1), n_want + 2):
        for i in range(0, len(words) - n + 1):
            run = words[i:i + n]
            got = norm(" ".join(w[0] for w in run))
            if not got:
                continue
            r = difflib.SequenceMatcher(None, want, got).ratio()
            if r > best[0]:
                best = (r, union([w[1:] for w in run]))
    return best

def center(b):
    return (b[0] + b[2] / 2, b[1] + b[3] / 2)

def find_text(elements, phrase, near=None):
    """Where is this text on screen? Returns (box, score, element_id) or (None, 0, None)."""
    cands = []
    for e in elements:
        s, box = span_in(e["words"], phrase)
        if box:
            cands.append((s, box, e["id"]))
    if not cands:
        return None, 0.0, None
    top = max(c[0] for c in cands)
    good = [c for c in cands if c[0] >= top - 0.02]
    if near and len(good) > 1:
        good.sort(key=lambda c: (center(c[1])[0] - near[0]) ** 2 + (center(c[1])[1] - near[1]) ** 2)
    s, box, eid = good[0]
    return (box, s, eid) if s >= 0.6 else (None, s, None)


# ---------- shape resolution ----------

class GroundError(ValueError):
    pass

def resolve_target(t, elements, size=None):
    """A target reference -> [x, y, w, h] in layout px.

    Forms: {"id": "t12"}, {"id": "t12", "span": "words"}, {"text": "Export", "near": [x, y]},
    {"box": [x, y, w, h]}, {"at": [x, y]}, {"norm_box": [...]} / {"norm_at": [...]} (0-1000 coords).
    """
    by_id = {e["id"]: e for e in elements}
    if "id" in t:
        ids = t["id"] if isinstance(t["id"], list) else [t["id"]]
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise GroundError(f"no element {missing[0]} in the last look")
        if t.get("span"):
            boxes = []
            for i in ids:
                s, box = span_in(by_id[i]["words"], t["span"])
                if box and s >= 0.6:
                    boxes.append(box)
            if boxes:
                return union(boxes)
        return union([by_id[i]["box"] for i in ids])
    if "text" in t:
        given = [float(v) for v in t["box"]] if t.get("box") else None
        near = t.get("near") or (center(given) if given else None)
        box, s, _ = find_text(elements, t["text"], near)
        if given and (box is None or ((center(box)[0] - near[0]) ** 2 + (center(box)[1] - near[1]) ** 2) ** 0.5
                      > max(120, given[2], given[3])):
            return given  # snap to the words only when they're where the box says
        if box is None:
            raise GroundError(f"text {t['text']!r} not found on screen (best match {s:.2f})")
        return box
    if "box" in t:
        return [float(v) for v in t["box"]]
    if "at" in t:
        x, y = t["at"]
        return [float(x) - 1, float(y) - 1, 2.0, 2.0]
    if size and ("norm_box" in t or "norm_at" in t):
        W, H = size
        if "norm_box" in t:
            x, y, w, h = t["norm_box"]
            return [x * W / 1000, y * H / 1000, w * W / 1000, h * H / 1000]
        x, y = t["norm_at"]
        return [x * W / 1000 - 1, y * H / 1000 - 1, 2.0, 2.0]
    raise GroundError(f"can't tell where {t!r} is: give id, text, box or at")

TARGET_KEYS = ("id", "text", "box", "at", "span", "near", "norm_box", "norm_at")

def resolve_shapes(shapes, elements, size=None):
    """Agent shapes -> overlay shapes with plain boxes. Shapes carry their target either
    inline ({"kind": "circle", "id": "t3"}) or under "target"."""
    out = []
    for s in shapes:
        s = dict(s)
        kind = s.get("kind", "circle")
        tgt = s.pop("target", None)
        # keys that belong to the shape itself rather than saying where its target is: what a label or
        # card writes, and where a sketch's board goes
        own = {"label": ("text",), "card": ("text",), "sketch": ("text", "at"), "panel": ("text", "at"),
               "toast": ("text", "at")}.get(kind, ())
        if tgt is None:
            tgt = {k: s[k] for k in TARGET_KEYS if k in s and k not in own}
        for k in TARGET_KEYS:
            if k not in own:
                s.pop(k, None)
        label = s.pop("label", None)
        if label:
            s["text"] = label
        if tgt:
            s["box"] = [round(v, 1) for v in resolve_target(tgt, elements, size)]
        if "from" in s and isinstance(s["from"], dict):
            b = resolve_target(s["from"], elements, size)
            s["from"] = list(center(b))
        if kind == "path" and s.get("points"):
            s["points"] = [list(p) for p in s["points"]]
        out.append(s)
    return out
