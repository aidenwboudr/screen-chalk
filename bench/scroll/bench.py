#!/usr/bin/python3
"""Scroll bench: do chalk's marks stay on what they mark while real apps scroll? Runs inside the bench
container (bench/scroll/run.sh), against the chalk checkout mounted at $CHALK_DIR.

Each scene opens real content in a real app (a long web page in Chrome, a PDF in Chrome's viewer, a CSS file
in VS Code, a long log in foot), with a small pure-magenta square next to one line: the ground truth that
moves exactly as the content does. chalk circles the text beside it in pure green, then a virtual touchpad
and mouse wheel (bench/vpointer) scroll the app (slow, fast flicks, the wheel, out of sight and back). `chalk-cap probe` sees
every frame the compositor shows and finds both colours; the score compares where the circle is with where
it should be, frame by frame:

  attached share of the frames where the target is on screen that show the circle within 4 px of it plus
           35 ms of its motion (two refreshes: it reads as moving with the page)    <- the headline number
  follow   the same within a flat 4 px (no allowance for motion)
  rest_ok  share of the frames where the target has been still for 100+ ms with the circle within 3 px
  missing  share of the frames where the target is on screen but the circle isn't
  err      median / 95th percentile distance (px) while both are on screen
  ghost    share of the frames where the target is out of sight that still show the circle
  lag      ms the circle's motion trails the target's (best time shift between the two)
  cpu      overlay + chalk-cap CPU (ms per second) while scrolling, and while still with marks up

  bench.py [--scenes web,pdf,code,term] [--out results.json] [--record DIR]
"""
import argparse, json, os, re, shutil, signal, subprocess, sys, time
import numpy as np

CHALK = os.environ.get("CHALK_DIR", "/chalk")
OUTPUT, SCALE = "HEADLESS-1", 1.5
W, H = 1920, 1280
WORK = "/tmp/scroll"
INK = "00ff00"
CPU_HZ = os.sysconf("SC_CLK_TCK")

# one script for every scene: (verb, args); `sign` flips it for apps that start at the bottom (the terminal)
SCRIPT = [
    ("sleep", 700),
    # reading: the finger moves slowly down a few times, then back up (100-400 px/s on screen)
    ("swipe", 40, 900), ("sleep", 250), ("swipe", 40, 900), ("sleep", 250), ("swipe", 40, 900), ("sleep", 600),
    ("swipe", -40, 900), ("sleep", 250), ("swipe", -40, 900), ("sleep", 250), ("swipe", -40, 900), ("sleep", 900),
    ("wheel", 2), ("sleep", 900), ("wheel", -2), ("sleep", 900),                        # mouse wheel
    ("swipe", 500, 250), ("sleep", 1500), ("swipe", -500, 250), ("sleep", 1500),        # a flick each way
    ("swipe", 1500, 900), ("sleep", 1200), ("swipe", -1500, 900), ("sleep", 1600),      # out of sight and back
]


def sh(*a, **k):
    return subprocess.run(a, check=True, **k)


def chalk(*args, check=True):
    return subprocess.run([os.path.join(CHALK, "chalk"), *args], capture_output=True, text=True, check=check)


def screen():
    raw = subprocess.run(["grim", "-t", "ppm", "-"], capture_output=True, check=True).stdout
    _, dims, _, body = raw.split(b"\n", 3)
    w, h = map(int, dims.split())
    return np.frombuffer(body, np.uint8, count=w * h * 3).reshape(h, w, 3)


def find_magenta(img=None):
    img = screen() if img is None else img
    m = (img[:, :, 0] > 220) & (img[:, :, 1] < 40) & (img[:, :, 2] > 220)
    if m.sum() < 9:
        return None
    ys, xs = np.nonzero(m)
    return xs.mean() / SCALE, ys.mean() / SCALE, int(m.sum())


def wait_magenta(timeout=40, settle=True):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        p = find_magenta()
        if p and settle and last and abs(p[1] - last[1]) < 0.5:
            return p
        last = p if p else None
        time.sleep(0.7)
    raise SystemExit("ground-truth marker never showed up")


def kill_apps():
    subprocess.run(["swaymsg", "-q", "[title=.*] kill"], capture_output=True)
    for p in ("chrome", "code", "foot"):
        subprocess.run(["pkill", "-f", p], capture_output=True)
    for _ in range(50):  # until the windows are really gone (a slow VS Code exit once ate a scene's scrolling)
        tree = subprocess.run(["swaymsg", "-t", "get_tree"], capture_output=True, text=True).stdout
        if '"pid"' not in tree:
            break
        if _ == 25:
            for p in ("chrome", "code", "foot"):
                subprocess.run(["pkill", "-9", "-f", p], capture_output=True)
        time.sleep(0.2)
    time.sleep(0.5)


# ---------- content ----------

def prose():
    txt = open("/usr/share/common-licenses/GPL-3").read()
    paras = [" ".join(p.split()) for p in txt.split("\n\n") if p.strip()]
    return [p for p in paras if len(p) > 3]


def page(path, sticky=True, target=70):
    paras = prose()
    body = []
    for i, p in enumerate(paras):
        p = p.replace("&", "&amp;").replace("<", "&lt;")
        if len(p) < 70 and p[:1].isupper():
            body.append(f"<h2>{p}</h2>")
        elif i == target:
            body.append(f'<p id="t"><span class="truth"></span>{p}</p>')
        else:
            body.append(f"<p>{p}</p>")
    nav = '<nav>GNU General Public License &middot; Version 3</nav>' if sticky else ""
    css = """body{font:17px/1.6 "DejaVu Serif",serif;margin:0;color:#222;background:#fff}
nav{position:sticky;top:0;height:56px;background:#24292f;color:#fff;display:flex;align-items:center;
    padding:0 24px;font:600 16px "DejaVu Sans",sans-serif;z-index:2}
main{max-width:780px;margin:0 auto;padding:24px 40px 600px 140px;position:relative}
p{margin:0 0 14px} h2{font:600 22px "DejaVu Sans",sans-serif;margin:30px 0 10px}
#t{scroll-margin-top:520px}
.truth{position:absolute;left:40px;width:7px;height:7px;background:#ff00ff;margin-top:10px;
        -webkit-print-color-adjust:exact;print-color-adjust:exact}"""
    open(path, "w").write(f"<!doctype html><meta charset=utf-8><title>GPL</title><style>{css}</style>"
                          f"{nav}<main>{''.join(body)}</main>")


def css_file(path, target=420):
    rnd = np.random.default_rng(7)
    words = re.findall(r"[a-z]{4,10}", open("/usr/share/common-licenses/GPL-3").read().lower())
    props = ["display: flex", "align-items: center", "padding: 0 12px", "margin: 4px 0 0",
             "font: 500 14px/1.4 var(--font-ui)", "color: var(--fg-muted)", "border-radius: 6px",
             "background: var(--surface-2)", "transition: opacity 120ms ease-out", "gap: 8px",
             "grid-template-columns: repeat(auto-fill, minmax(220px, 1fr))", "overflow: hidden",
             "text-overflow: ellipsis", "white-space: nowrap", "z-index: 3", "position: relative",
             "box-shadow: 0 1px 2px rgb(0 0 0 / 12%)", "width: calc(100% - 2 * var(--gutter))"]
    out, n = [":root {", "  --gutter: 16px;", "}", ""], 0
    while len(out) < 900:
        a, b = rnd.choice(words, 2)
        out.append(f".{a}-{b}{':hover' if rnd.random() < .2 else ''} {{")
        for p in rnd.choice(props, int(rnd.integers(2, 7)), replace=False):
            out.append(f"  {p};")
        if len(out) >= target and n == 0:
            out.append("  box-shadow: inset 0 0 0 1px rgb(0 0 0 / 6%), 0 0 0 3px #ff00ff;")
            n = len(out)
        out.append("}")
        out.append("")
    open(path, "w").write("\n".join(out))
    return n


def log_lines(path, target_from_end=26):
    src = subprocess.run(["bash", "-c", "ls -la --time-style=long-iso /usr/lib/x86_64-linux-gnu /usr/share/doc /usr/bin"],
                         capture_output=True, text=True).stdout.splitlines()
    lines = [l for l in src if l and not l.endswith(":")][:420]
    k = len(lines) - target_from_end
    lines[k] = "\x1b[38;2;255;0;255m\u2588\x1b[0m  " + lines[k]
    open(path, "w").write("\n".join(lines) + "\n")


# ---------- scenes: open the app, return (target box, script sign) ----------

def chrome(url):
    sh("swaymsg", "-q", "exec", f"google-chrome --ozone-platform=wayland --no-sandbox --no-first-run "
       f"--no-default-browser-check --user-data-dir={WORK}/chrome-profile --new-window '{url}'")


def scene_web():
    page(f"{WORK}/page.html")
    chrome(f"file://{WORK}/page.html#t")
    mx, my, _ = wait_magenta()
    return [mx + 80, my - 12, 320, 24], 1


def scene_pdf():
    page(f"{WORK}/pdfpage.html", sticky=False, target=4)
    sh("google-chrome", "--headless", "--no-sandbox", "--disable-gpu", f"--print-to-pdf={WORK}/doc.pdf",
       "--no-pdf-header-footer", f"file://{WORK}/pdfpage.html", capture_output=True)
    chrome(f"file://{WORK}/doc.pdf")
    mx, my, _ = wait_magenta()
    return [mx + 80, my - 12, 320, 24], 1


def scene_code():
    n = css_file(f"{WORK}/style.css")
    os.makedirs(f"{WORK}/vscode/User", exist_ok=True)
    json.dump({"workbench.startupEditor": "none", "security.workspace.trust.enabled": False,
               "window.restoreWindows": "none", "update.mode": "none", "telemetry.telemetryLevel": "off",
               "workbench.tips.enabled": False, "extensions.ignoreRecommendations": True,
               "window.titleBarStyle": "custom", "editor.cursorBlinking": "solid",
               "chat.disableAIFeatures": True, "workbench.secondarySideBar.defaultVisibility": "hidden"},
              open(f"{WORK}/vscode/User/settings.json", "w"))
    sh("swaymsg", "-q", "exec", f"code --ozone-platform=wayland --no-sandbox --user-data-dir={WORK}/vscode "
       f"--extensions-dir={WORK}/vscode-ext --disable-workspace-trust --skip-welcome --skip-release-notes "
       f"--goto {WORK}/style.css:{n - 12}")
    mx, my, _ = wait_magenta(60)
    return [mx - 400, my - 11, 320, 22], 1


def scene_term():
    log_lines(f"{WORK}/log.txt")
    sh("swaymsg", "-q", "exec", f"foot -T log -o cursor.blink=no sh -c 'printf \"\\033[?25l\"; cat {WORK}/log.txt; sleep 600'")
    mx, my, _ = wait_magenta()
    return [mx + 60, my - 11, 320, 22], -1


SCENES = {"web": scene_web, "pdf": scene_pdf, "code": scene_code, "term": scene_term}


# ---------- driving and watching ----------

def drive(script, sign=1):
    lines = []
    for verb, *a in script:
        if verb in ("swipe", "wheel"):
            a = [a[0] * sign] + a[1:]
        lines.append(" ".join(["wait" if verb == "sleep" else verb, *map(str, a)]))
    subprocess.run(["vpointer", str(W), str(H)], input="\n".join(lines) + "\n", text=True,
                   capture_output=True, check=True)


def cpu_ms(pids):
    t = 0
    for pid in pids:
        try:
            f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            t += int(f[11]) + int(f[12]) + int(f[13]) + int(f[14])  # (+ waited-for children: grim, chalk-cap)
        except (OSError, IndexError):
            pass
    return t * 1000 / CPU_HZ


def chalk_pids():
    out = subprocess.run(["pgrep", "-f", "overlay.py|chalk-cap serve"], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


def run_scene(name, record=None):
    kill_apps()
    chalk("clear", check=False)
    box, sign = SCENES[name]()
    drive([("move", W // 2, H // 2 + 40), ("sleep", 300)])
    chalk("look")
    chalk("draw", json.dumps([{"kind": "circle", "box": box, "color": "#" + INK}]))
    time.sleep(1.5)
    pids = chalk_pids()
    c0 = cpu_ms(pids); time.sleep(4.0); idle = (cpu_ms(pids) - c0) / 4.0  # marks up, nothing moving
    total = sum(a[0] for v, *a in SCRIPT if v == "sleep") / 1000 + sum(a[1] for v, *a in SCRIPT if v == "swipe") / 1000 + 4
    # the probe watches the column the target and its marker move in (a full-screen grab can't keep up)
    x0 = int(max(0, box[0] - 160)); x1 = int(min(W, box[0] + box[2] + 160))
    cmd = ["chalk-cap", "probe", OUTPUT, str(total), INK, str(x0), "0", str(x1 - x0), str(H)]
    if record:
        cmd.append(os.path.join(record, f"{name}.raw"))
    probe = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    time.sleep(0.3)
    c0, t0 = cpu_ms(pids), time.time()
    drive(SCRIPT, sign)
    busy = (cpu_ms(pids) - c0) / (time.time() - t0)
    rows = [list(map(float, l.split())) for l in probe.communicate()[0].splitlines() if l.strip()]
    if os.environ.get("CHALK_FOLLOW_DUMP"):
        np.save(os.path.join(os.environ["CHALK_FOLLOW_DUMP"], f"probe-{name}.npy"), np.array(rows))
    res = score(np.array(rows))
    res.update(cpu_scroll=round(busy, 1), cpu_idle=round(idle, 1), box=box)
    return res


def score(a):
    """a: rows (t_ms, tx, ty, tn, ix, iy, in), positions in layout px."""
    t = a[:, 0]
    tp, ip = a[:, 1:3], a[:, 4:6]   # (the probe reports layout px)
    tn, inn = a[:, 3], a[:, 6]
    tn0, in0 = tn[0], inn[0]
    off = ip[0] - tp[0]   # where the circle sits relative to the marker when it's right
    tvis, tgone = tn >= 0.9 * tn0, tn <= 0.1 * tn0
    ivis = inn >= 0.6 * in0
    want = tp + off
    err = np.hypot(*(ip - want).T)
    both = tvis & ivis
    # how fast the target moves (px/s), from the frames either side
    speed = np.zeros(len(t))
    for i in range(1, len(t) - 1):
        if tvis[i - 1] and tvis[i + 1] and t[i + 1] > t[i - 1]:
            speed[i] = np.hypot(*(tp[i + 1] - tp[i - 1])) / (t[i + 1] - t[i - 1]) * 1000
    # at rest: the target hasn't moved for 150 ms
    rest = np.zeros(len(t), bool)
    for i in range(len(t)):
        j = np.nonzero((t >= t[i] - 150) & (t <= t[i]) & tvis)[0]
        rest[i] = tvis[i] and len(j) > 0 and t[i] - t[j[0]] >= 100 and np.ptp(tp[j, 1]) < 0.5 and np.ptp(tp[j, 0]) < 0.5
    good = both & (err <= 4)
    # attached: within 4 px plus 35 ms of the target's motion (two refreshes: as close as any mark that reacts
    # to what's on screen can be; it reads as moving with the page)
    attached = both & (err <= 4 + speed * 0.035)
    # ignore the first 250 ms after the target leaves (the circle fading out with it is fine)
    settled = tgone.copy()
    for i in np.nonzero(tgone)[0]:
        prev = np.nonzero(~tgone[:i])[0]
        if len(prev) and t[i] - t[prev[-1]] < 250:
            settled[i] = False
    # lag: shift the target's path in time until it best matches the circle's (frames where both show)
    lag = None
    if both.sum() > 20:
        best = None
        for d in range(0, 301, 4):
            e = np.abs(ip[both, 1] - off[1] - np.interp(t[both] - d, t[tvis], tp[tvis, 1])).mean()
            if best is None or e < best[0]:
                best = (e, d)
        moving = np.abs(np.gradient(tp[tvis, 1])).sum() > 50
        lag = best[1] if moving else None
    n = max(1, tvis.sum())
    fast = both & (speed >= 500)
    slow = both & ~rest & (speed < 500)
    return {"frames": int(len(a)), "attached": round(float(attached.sum() / n), 3), "follow": round(float(good.sum() / n), 3),
            "rest_ok": round(float((rest & ivis & (err <= 3)).sum() / rest.sum()), 3) if rest.any() else None,
            "err_p50": round(float(np.median(err[both])), 1) if both.any() else None,
            "err_p95": round(float(np.percentile(err[both], 95)), 1) if both.any() else None,
            "err_slow": round(float(np.median(err[slow])), 1) if slow.any() else None,
            "err_fast": round(float(np.median(err[fast])), 1) if fast.any() else None,
            "missing": round(float((tvis & ~ivis).sum() / n), 3),
            "ghost": round(float((settled & ivis).sum() / max(1, settled.sum())), 3) if settled.any() else None,
            "lag_ms": lag}


def to_video(raw, mp4, fps=30):
    """The probe's frames (each at its own time) -> a constant-rate video."""
    data = open(raw, "rb").read()
    head, data = data.split(b"\n", 1)
    fw, fh = map(int, head.split())
    n = 8 + fw * fh * 3
    frames = [(int.from_bytes(data[i:i + 8], "little"), data[i + 8:i + n]) for i in range(0, len(data) - n + 1, n)]
    if not frames:
        return
    p = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{fw}x{fh}",
                          "-r", str(fps), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", mp4],
                         stdin=subprocess.PIPE)
    t, k = frames[0][0], 0
    while t <= frames[-1][0]:
        while k + 1 < len(frames) and frames[k + 1][0] <= t:
            k += 1
        p.stdin.write(frames[k][1])
        t += 1000 / fps
    p.stdin.close(); p.wait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", default="web,pdf,code,term")
    ap.add_argument("--out")
    ap.add_argument("--record")
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    os.makedirs(WORK, exist_ok=True)
    os.environ.setdefault("CHALK_STATE", f"{WORK}/state")
    os.environ["CHALK_NO_FORWARD"] = "1"
    if args.record:
        os.makedirs(args.record, exist_ok=True)
    chalk("stop", check=False)
    chalk("start")
    results = {}
    for name in args.scenes.split(","):
        try:
            results[name] = run_scene(name, args.record)
        except (SystemExit, subprocess.CalledProcessError) as e:
            results[name] = {"error": str(e)}
        print(name, json.dumps(results[name]), flush=True)
        if args.record and os.path.exists(os.path.join(args.record, f"{name}.raw")):
            to_video(os.path.join(args.record, f"{name}.raw"), os.path.join(args.record, f"{name}{args.label}.mp4"))
            os.unlink(os.path.join(args.record, f"{name}.raw"))
    kill_apps()
    chalk("stop", check=False)
    log = os.path.join(os.environ["CHALK_STATE"], "overlay.log")
    if args.out and os.path.exists(log):
        shutil.copy(log, os.path.splitext(args.out)[0] + ".log")
    ok = [r for r in results.values() if "follow" in r]
    summary = {"attached": round(float(np.mean([r["attached"] for r in ok])), 3) if ok else 0,
               "follow": round(float(np.mean([r["follow"] for r in ok])), 3) if ok else 0,
               "rest_ok": round(float(np.mean([r["rest_ok"] for r in ok if r["rest_ok"] is not None] or [0])), 3),
               "missing": round(float(np.mean([r["missing"] for r in ok])), 3) if ok else 0,
               "cpu_scroll": round(float(np.mean([r["cpu_scroll"] for r in ok])), 1) if ok else None,
               "cpu_idle": round(float(np.mean([r["cpu_idle"] for r in ok])), 1) if ok else None}
    print("summary", json.dumps(summary))
    if args.out:
        json.dump({"scenes": results, "summary": summary}, open(args.out, "w"), indent=1)


if __name__ == "__main__":
    main()
