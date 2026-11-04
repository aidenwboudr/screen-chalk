#!/usr/bin/env python3
"""Hill-climb harness for chalk's grounding: does the model mark the right thing in the right place?

  .venv/bin/python run.py prep                       render pages + OCR them (cached)
  .venv/bin/python run.py run VARIANT [--model opus] [--only code] [-j 6] [--repeat 1]
  .venv/bin/python run.py show RESULT.json           per-task table + failure gallery

A variant = how the screen is shown to the model (image size, overlays, OCR element list) +
which policy prompt (policies/*.md). Score = share of tasks whose primary mark lands on a
ground-truth element (see `hit`). Results go to out/results/.
"""
import argparse, base64, concurrent.futures as cf, io, json, os, re, subprocess, sys, time

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ground  # noqa: E402

OUT = os.path.join(HERE, "out")
RENDER = os.path.join(OUT, "render")
RESULTS = os.path.join(OUT, "results")
W, H, HIRES = 1920, 1280, 1.5

SCHEMA = {
    "type": "object",
    "properties": {
        "say": {"type": "string", "description": "one or two spoken sentences that go with the drawing"},
        "shapes": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["circle", "arrow", "underline", "highlight", "box", "point",
                                                "label", "card", "spotlight", "strike", "path"]},
            "id": {"type": "string"}, "span": {"type": "string"}, "text": {"type": "string"},
            "box": {"type": "array", "items": {"type": "number"}},
            "at": {"type": "array", "items": {"type": "number"}},
            "label": {"type": "string"},
        }, "required": ["kind"]}},
    },
    "required": ["shapes"],
}


# ---------- prep ----------

def prep(psm="11"):
    if not os.path.exists(os.path.join(RENDER, "gt.json")) or newer_pages():
        subprocess.run(["node", os.path.join(HERE, "render.js"), RENDER], check=True)
    for f in sorted(os.listdir(RENDER)):
        if not f.endswith(".hires.png"):
            continue
        base = f[:-len(".hires.png")]
        hires = os.path.join(RENDER, f)
        one = os.path.join(RENDER, base + ".png")
        if not os.path.exists(one) or os.path.getmtime(one) < os.path.getmtime(hires):
            Image.open(hires).convert("RGB").resize((W, H), Image.LANCZOS).save(one)
        look = os.path.join(RENDER, f"{base}.look-psm{psm}.json")
        if not os.path.exists(look) or os.path.getmtime(look) < os.path.getmtime(hires):
            t0 = time.monotonic()
            tsv = subprocess.run(["tesseract", hires, "stdout", "--psm", psm, "tsv"], capture_output=True, text=True).stdout
            els = ground.parse_tsv(tsv, scale=1 / HIRES)
            json.dump({"elements": els, "ocr_ms": round((time.monotonic() - t0) * 1000)}, open(look, "w"))
            print(f"ocr {base}: {len(els)} elements")

def newer_pages():
    gt = os.path.getmtime(os.path.join(RENDER, "gt.json"))
    for d in ("pages", "pages-hard"):
        d = os.path.join(HERE, d)
        if os.path.isdir(d) and any(os.path.getmtime(os.path.join(d, p)) > gt for p in os.listdir(d)):
            return True
    return False

def load_tasks(name):
    return json.load(open(os.path.join(HERE, name)))

def all_tasks():
    out = {}
    for name in ("tasks.json", "tasks-hard.json"):
        if os.path.exists(os.path.join(HERE, name)):
            out.update({t["id"]: t for t in load_tasks(name)})
    return out


# ---------- variants ----------

VARIANTS = {
    # image width the model sees, whether it gets the OCR list, extra drawing on the image, policy prompt
    "raw-full":  {"img_w": 1920, "elements": False, "overlay": None, "policy": "raw"},
    "raw-1280":  {"img_w": 1280, "elements": False, "overlay": None, "policy": "raw"},
    "ids-full":  {"img_w": 1920, "elements": True, "overlay": None, "policy": "ids"},
    "ids-1280":  {"img_w": 1280, "elements": True, "overlay": None, "policy": "ids"},
    "grid-full": {"img_w": 1920, "elements": True, "overlay": "grid", "policy": "ids"},
    "marks-full": {"img_w": 1920, "elements": True, "overlay": "marks", "policy": "ids"},
    "raw-zoom":  {"img_w": 1920, "elements": False, "overlay": None, "policy": "raw", "zoom": True},
    "grid-raw":  {"img_w": 1920, "elements": False, "overlay": "grid", "policy": "raw"},
    "grid-zoom": {"img_w": 1920, "elements": True, "overlay": "grid", "policy": "ids", "zoom": True},
}

def load_variant(name):
    v = dict(VARIANTS[name.split("+")[0]])
    for extra in name.split("+")[1:]:  # e.g. ids-full+policy=ids2
        k, _, val = extra.partition("=")
        v[k] = int(val) if val.isdigit() else (val == "1" if val in ("0", "1") else val)
    v["name"] = name
    return v

def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)

def make_image(page, v, elements):
    img = Image.open(os.path.join(RENDER, page.replace(".html", ".png"))).convert("RGB")
    if v["overlay"] == "grid":
        import layout
        img = layout.with_grid(img)
    elif v["overlay"] == "marks":
        d = ImageDraw.Draw(img, "RGBA")
        f = font(11)
        for e in elements:
            x, y, w, h = e["box"]
            d.rectangle([x, y, x + w, y + h], outline=(255, 0, 160, 150))
            tag = e["id"][1:]
            tw = d.textlength(tag, font=f)
            d.rectangle([x - 1, y - 12, x + tw + 2, y - 1], fill=(255, 0, 160, 200))
            d.text((x + 1, y - 12), tag, fill=(255, 255, 255, 255), font=f)
    k = v["img_w"] / W
    if k != 1:
        img = img.resize((round(W * k), round(H * k)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), k


def scaled_listing(elements, k):
    if k == 1:
        return ground.element_listing(elements)
    out = []
    for e in elements:
        x, y, w, h = (int(round(c * k)) for c in e["box"])
        out.append(f'{e["id"]} [{x},{y},{w},{h}] "{e["text"]}"')
    return "\n".join(out)

def build_prompt(task, v, elements, k):
    policy = open(os.path.join(HERE, "policies", v["policy"] + ".md")).read()
    iw, ih = round(W * k), round(H * k)
    parts = [policy.strip(), "",
             f"The screenshot is {iw}x{ih} pixels, origin top-left; give every coordinate in this image's pixels."]
    if v["elements"]:
        parts += ["", "Text on screen found by OCR (id [x,y,w,h] \"text\"):", scaled_listing(elements, k)]
    parts += ["", f"The user asks: \"{task['q']}\"",
              "Reply with the drawing as JSON matching the schema: the main mark first."]
    return "\n".join(parts)


# ---------- model call ----------

def ask(model, img_png, prompt, effort=None, schema=None):
    msg = {"type": "user", "message": {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(img_png).decode()}},
        {"type": "text", "text": prompt}]}}
    cmd = ["claude", "-p", "--bare", "--no-session-persistence", "--model", model, "--tools", "",
           "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--json-schema", json.dumps(schema or SCHEMA)]
    if effort:
        cmd += ["--effort", effort]
    t0 = time.monotonic()
    r = subprocess.run(cmd, input=json.dumps(msg) + "\n", capture_output=True, text=True, timeout=300)
    secs = time.monotonic() - t0
    for line in r.stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "result":
            out = e.get("structured_output")
            if out is None:
                m = re.search(r"\{.*\}", e.get("result") or "", re.S)
                out = json.loads(m.group(0)) if m else None
            return out, secs, e.get("usage", {})
    return None, secs, {"error": r.stderr[-400:]}


# ---------- scoring ----------

def iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    u = a[2] * a[3] + b[2] * b[3] - inter
    return inter / u if u > 0 else 0.0

def inside(pt, b, pad=4):
    return b[0] - pad <= pt[0] <= b[0] + b[2] + pad and b[1] - pad <= pt[1] <= b[1] + b[3] + pad

def overlap_frac(a, b):
    """Share of a's area that lies inside b."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return max(0, x1 - x0) * max(0, y1 - y0) / max(1.0, a[2] * a[3])

def hit(pred, gt):
    """A mark counts when it's on the target: snugly around it, or on a meaningful part of it
    (the checkbox of the MIDI row, the plates of the capacitor). Circling the whole toolbar
    doesn't count as circling one button."""
    if pred[2] <= 4 and pred[3] <= 4:  # a point
        return inside(ground.center(pred), gt, 6)
    # a 9 px ref designator gets circled with a 24+ px loop; judge tiny targets at that size
    gw, gh = max(gt[2], 24), max(gt[3], 24)
    g2 = [gt[0] + gt[2] / 2 - gw / 2, gt[1] + gt[3] / 2 - gh / 2, gw, gh]
    cx, cy = ground.center(pred)
    if g2[0] < cx < g2[0] + gw and g2[1] < cy < g2[1] + gh and pred[2] * pred[3] <= 6 * gw * gh:
        return True  # centred on it and about its size (a whole table cell around a 16 px value)
    if iou(pred, gt) >= 0.4:
        return True
    pad = [gt[0] - 4, gt[1] - 4, gt[2] + 8, gt[3] + 8]
    if overlap_frac(pred, pad) >= 0.8:  # a part of the target
        return True
    area_ratio = (pred[2] * pred[3]) / max(1.0, gt[2] * gt[3])
    return overlap_frac(gt, pred) >= 0.8 and area_ratio <= 4.0  # around it, not around its whole panel

def to_screen(shapes, k):
    """Undo the image scale on any raw coordinates the model gave."""
    if k == 1:
        return shapes
    out = []
    for s in shapes:
        s = dict(s)
        for key in ("box", "at", "from"):
            if isinstance(s.get(key), list):
                s[key] = [c / k for c in s[key]]
        if s.get("points"):
            s["points"] = [[c / k for c in p] for p in s["points"]]
        out.append(s)
    return out

def score(task, answer, elements, gt_page, k):
    gts = []
    for g in task["gt"]:
        if g in gt_page:  # a wrapped sentence: its whole box or any one of its lines
            gts.append(gt_page[g]["box"])
            gts += [r for r in gt_page[g].get("rects", []) if len(gt_page[g]["rects"]) > 1]
    res = {"ok": False, "any": False, "pred": None, "iou": 0.0, "err": None, "kind": None}
    if not answer or not answer.get("shapes"):
        res["err"] = "no shapes"
        return res
    marks = [s for s in answer["shapes"] if s.get("kind") not in ("label", "card")]
    try:
        resolved = ground.resolve_shapes(to_screen(marks, k), elements, [W, H])
    except ground.GroundError as e:
        res["err"] = str(e)
        return res
    boxes = [(s.get("kind"), s["box"]) for s in resolved if s.get("box")]
    if not boxes:
        res["err"] = "no box"
        return res
    kind, pred = boxes[0]
    res.update(pred=[round(c, 1) for c in pred], kind=kind,
               ok=any(hit(pred, g) for g in gts), any=any(hit(b, g) for _, b in boxes for g in gts),
               iou=round(max(iou(pred, g) for g in gts), 3))
    return res


# ---------- zoom: a second, magnified look at the main mark ----------

ZOOM_SCHEMA = {"type": "object", "properties": {"box": {"type": "array", "items": {"type": "number"}}},
               "required": ["box"]}

def zoom_view(page, box, min_w=360, min_h=240, max_out=1100):
    """Crop around box from the native-res render, magnified. Returns (png, region, mag)."""
    x, y, w, h = box
    cw, ch = max(min_w, 3 * w), max(min_h, 3 * h)
    cx, cy = x + w / 2, y + h / 2
    x0 = min(max(0, cx - cw / 2), W - cw); y0 = min(max(0, cy - ch / 2), H - ch)
    x0, y0 = max(0, x0), max(0, y0)
    cw, ch = min(cw, W - x0), min(ch, H - y0)
    mag = min(4.0, max_out / cw, max_out / ch)
    hires = Image.open(os.path.join(RENDER, page.replace(".html", ".hires.png"))).convert("RGB")
    crop = hires.crop((round(x0 * HIRES), round(y0 * HIRES), round((x0 + cw) * HIRES), round((y0 + ch) * HIRES)))
    crop = crop.resize((round(cw * mag), round(ch * mag)), Image.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, "PNG")
    return buf.getvalue(), (x0, y0, cw, ch), mag

def zoom_refine(task, page, answer, els, k, args):
    marks = [s for s in answer["shapes"] if s.get("kind") not in ("label", "card")]
    if not marks:
        return answer, 0.0
    try:
        first = ground.resolve_shapes(to_screen(marks[:1], k), els, [W, H])[0]
    except ground.GroundError:
        return answer, 0.0
    if not first.get("box"):
        return answer, 0.0
    png, (x0, y0, cw, ch), mag = zoom_view(page, first["box"])
    bx, by, bw, bh = first["box"]
    zb = [round((bx - x0) * mag), round((by - y0) * mag), round(bw * mag), round(bh * mag)]
    zw, zh = round(cw * mag), round(ch * mag)
    prompt = (f"You are marking the user's screen to answer: \"{task['q']}\".\n"
              f"You chose this {first.get('kind', 'circle')} for the main mark. The image is that part of the screen "
              f"magnified {mag:.1f}x ({zw}x{zh} px); your mark's box is at {zb} in this image.\n"
              f"Look closely and give the box [x, y, width, height] in THIS image's pixels that fits the exact "
              f"thing the mark should be on. Keep it if it's already right; move or resize it if not.")
    out, secs, _ = ask(args.model, png, prompt, args.effort, schema=ZOOM_SCHEMA)
    if not out or len(out.get("box") or []) != 4:
        return answer, secs
    zx, zy, zw2, zh2 = out["box"]
    refined = dict(marks[0])
    for key in ground.TARGET_KEYS:
        refined.pop(key, None)
    refined["box"] = [x0 + zx / mag, y0 + zy / mag, zw2 / mag, zh2 / mag]
    rest = [s for s in answer["shapes"] if s is not marks[0]]
    rest = [dict(s, **({"box": [c / k for c in s["box"]]} if isinstance(s.get("box"), list) and k != 1 else {}))
            for s in rest]
    return {**answer, "shapes": [refined] + rest, "first_pass": answer}, secs


# ---------- run ----------

def run(args):
    v = load_variant(args.variant)
    tasks = load_tasks(args.tasks)
    if args.only:
        tasks = [t for t in tasks if any(o in t["id"] or o in t["page"] for o in args.only.split(","))]
    gt = json.load(open(os.path.join(RENDER, "gt.json")))
    psm = str(v.get("psm", 11))
    looks = {}

    def one(task, rep):
        page = task["page"]
        if page not in looks:
            looks[page] = json.load(open(os.path.join(RENDER, page.replace(".html", f".look-psm{psm}.json"))))["elements"]
        els = looks[page]
        img, k = make_image(page, v, els)
        prompt = build_prompt(task, v, els, k)
        answer, secs, usage = ask(args.model, img, prompt, args.effort)
        if v.get("zoom") and answer and answer.get("shapes"):
            answer, more = zoom_refine(task, page, answer, els, k, args)
            secs += more
            k = 1  # the refined box is already in screen px
        sc = score(task, answer, els, gt[page], k)
        return {"task": task["id"], "rep": rep, "q": task["q"], "gt": [gt[page][g]["box"] for g in task["gt"] if g in gt[page]],
                "answer": answer, "secs": round(secs, 1), **sc}

    jobs = [(t, r) for r in range(args.repeat) for t in tasks]
    rows = []
    with cf.ThreadPoolExecutor(args.j) as ex:
        futs = {ex.submit(one, t, r): t for t, r in jobs}
        for f in cf.as_completed(futs):
            try:
                row = f.result()
            except Exception as e:  # keep going; a crash is a miss
                t = futs[f]
                row = {"task": t["id"], "ok": False, "any": False, "err": f"crash: {e}", "secs": 0}
            rows.append(row)
            mark = "✓" if row["ok"] else "✗"
            print(f"{mark} {row['task']:<28} {row.get('kind') or '-':<9} iou={row.get('iou', 0):.2f} {row['secs']:>5}s {row.get('err') or ''}", flush=True)
    rows.sort(key=lambda r: (r["task"], r.get("rep", 0)))
    summary = {**summarize(v["name"], args.model, args.effort, rows), "tasks": args.tasks,
               "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    os.makedirs(RESULTS, exist_ok=True)
    tag = "" if args.tasks == "tasks.json" else "-" + args.tasks.replace(".json", "")
    path = os.path.join(RESULTS, f"{time.strftime('%m%d-%H%M%S')}-{v['name'].replace('/', '_')}-{args.model}{tag}.json")
    json.dump({"summary": summary, "rows": rows}, open(path, "w"), indent=1)
    with open(os.path.join(RESULTS, "log.jsonl"), "a") as f:
        f.write(json.dumps({**summary, "file": os.path.basename(path)}) + "\n")
    print(json.dumps(summary))
    print(path)


def summarize(name, model, effort, rows):
    n = len(rows)
    return {"variant": name, "model": model, "effort": effort, "n": n,
            "hit": round(sum(r["ok"] for r in rows) / n, 3),
            "any_hit": round(sum(r["any"] for r in rows) / n, 3),
            "mean_iou": round(sum(r.get("iou", 0) for r in rows) / n, 3),
            "median_secs": sorted(r["secs"] for r in rows)[n // 2]}

def rescore(args):
    """Re-grade saved answers with the current scorer and ground truth (no model calls)."""
    gt = json.load(open(os.path.join(RENDER, "gt.json")))
    tasks = all_tasks()
    for path in args.results:
        data = json.load(open(path))
        s = data["summary"]
        v = load_variant(s["variant"])
        psm = str(v.get("psm", 11))
        k = v["img_w"] / W
        for r in data["rows"]:
            t = tasks.get(r["task"])
            if not t:
                continue
            els = json.load(open(os.path.join(RENDER, t["page"].replace(".html", f".look-psm{psm}.json"))))["elements"]
            r.update(score(t, r.get("answer"), els, gt[t["page"]], k))
            r["gt"] = [gt[t["page"]][g]["box"] for g in t["gt"] if g in gt[t["page"]]]
        data["summary"] = {**summarize(s["variant"], s["model"], s.get("effort"), data["rows"]),
                           "tasks": s.get("tasks", "tasks.json"), "time": s.get("time")}
        json.dump(data, open(path, "w"), indent=1)
        print(json.dumps(data["summary"]))


def labels(args):
    """Label legibility, replayed offline from saved answers: how much busy screen each label
    covers, and how often it lands on a mark, with and without the occupancy map."""
    import layout
    tasks = all_tasks()
    gt = json.load(open(os.path.join(RENDER, "gt.json")))
    stats = {"plain": [], "aware": []}
    for path in args.results:
        data = json.load(open(path))
        v = load_variant(data["summary"]["variant"])
        k = v["img_w"] / W
        for r in data["rows"]:
            t = tasks.get(r["task"])
            if not t or not r.get("answer"):
                continue
            els = json.load(open(os.path.join(RENDER, t["page"].replace(".html", ".look-psm11.json"))))["elements"]
            occ = layout.occupancy(os.path.join(RENDER, t["page"].replace(".html", ".png")))
            busy = layout.Busy(occ)
            try:
                shapes = ground.resolve_shapes(to_screen(r["answer"]["shapes"], k), els, [W, H])
            except ground.GroundError:
                continue
            marks = [tuple(s["box"]) for s in shapes if s.get("box") and s.get("kind") not in ("label", "card")]
            for mode in stats:
                taken = []
                for s in shapes:
                    text = s.get("text") if s.get("kind") != "card" else None
                    if not text or not s.get("box") or s.get("kind") == "label":
                        continue
                    w, h = min(420, 0.43 * 30 * len(text)), 38 * (1 + int(0.43 * 30 * len(text) // 420))
                    clear = [(m[0] - 10, m[1] - 10, m[2] + 20, m[3] + 20) for m in marks] if mode == "aware" else ()
                    x, y = layout.place(s["box"], w, h, (0, 0, W, H), taken,
                                        busy if mode == "aware" else None, None, clear)
                    box = (x, y, w, h)
                    taken.append(box)
                    on_mark = any(layout.overlaps(box, m) for m in marks)
                    stats[mode].append((busy.cover(*box), on_mark))
    for mode, rows in stats.items():
        if rows:
            n = len(rows)
            print(f"{mode:6} labels={n} mean_busy_under_label={sum(c for c, _ in rows) / n:.3f} "
                  f"on_a_mark={sum(m for _, m in rows) / n:.2%}")


def show(args):
    data = json.load(open(args.result))
    gallery = os.path.join(OUT, "gallery", os.path.basename(args.result).replace(".json", ""))
    os.makedirs(gallery, exist_ok=True)
    tasks = all_tasks()
    for r in data["rows"]:
        print(f"{'✓' if r['ok'] else '✗'} {r['task']:<28} {r.get('kind') or '-':<9} pred={r.get('pred')} gt={r.get('gt')} {r.get('err') or ''}")
        if r["ok"] and not args.all:
            continue
        t = tasks.get(r["task"])
        if not t:
            continue
        img = Image.open(os.path.join(RENDER, t["page"].replace(".html", ".png"))).convert("RGB")
        d = ImageDraw.Draw(img)
        for g in r.get("gt") or []:
            d.rectangle([g[0], g[1], g[0] + g[2], g[1] + g[3]], outline=(0, 200, 0), width=3)
        if r.get("pred"):
            p = r["pred"]
            d.rectangle([p[0], p[1], p[0] + p[2], p[1] + p[3]], outline=(255, 0, 0), width=3)
        d.text((10, H - 30), r["q"][:150], fill=(255, 0, 0), font=font(18))
        img.save(os.path.join(gallery, f"{r['task']}-{r.get('rep', 0)}.png"))
    print(gallery)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("prep"); p.add_argument("--psm", default="11")
    p = sp.add_parser("run"); p.add_argument("variant"); p.add_argument("--model", default="opus")
    p.add_argument("--effort"); p.add_argument("--only"); p.add_argument("-j", type=int, default=6)
    p.add_argument("--repeat", type=int, default=1); p.add_argument("--tasks", default="tasks.json")
    p = sp.add_parser("show"); p.add_argument("result"); p.add_argument("--all", action="store_true")
    p = sp.add_parser("rescore"); p.add_argument("results", nargs="+")
    p = sp.add_parser("labels"); p.add_argument("results", nargs="+")
    a = ap.parse_args()
    if a.cmd == "prep":
        prep(a.psm)
    elif a.cmd == "run":
        run(a)
    elif a.cmd == "rescore":
        rescore(a)
    elif a.cmd == "labels":
        labels(a)
    else:
        show(a)


if __name__ == "__main__":
    main()
