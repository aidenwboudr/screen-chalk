#!/usr/bin/env python3
"""Answer the panel that's up in the headless Sway the way a person would: a real pointer (bench/vpointer) clicks
a choice, Next, Done; a typed answer goes in with wtype. For end-to-end tests (Teach's screen-mode e2e tests).

  drive_panel.py wait PREFIX [TIMEOUT]     wait for a panel whose tag starts with PREFIX, print its tag
  drive_panel.py answer TAG [--pick N] [--text WORDS]   answer every question on it, page by page
Needs the headless Sway's env (bench/headless-sway.sh) and CHALK_SOCK."""
import json, os, shutil, socket, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
VP = os.path.join(HERE, "vpointer", "vpointer")
WTYPE = shutil.which("wtype") or os.path.expanduser("~/.local/bin/wtype")


def info():
    s = socket.socket(socket.AF_UNIX)
    s.connect(os.environ["CHALK_SOCK"])
    s.sendall(b'{"op":"panel_info"}\n')
    return json.loads(s.makefile().readline())


def panel(tag):
    return next((p for p in info().get("panels", []) if p["tag"] == tag), None)


def click(x, y):
    subprocess.run([VP, "1920", "1280"], input=f"move {x} {y}\nwait 250\nclick\nwait 250\n", text=True,
                   stdout=subprocess.DEVNULL, check=True)
    time.sleep(0.5)


def centre(rect):
    x, y, w, h = rect
    return int(x + w / 2), int(y + h / 2)


def wait(prefix, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for p in info().get("panels", []):
            if str(p["tag"]).startswith(prefix):
                print(p["tag"])
                return 0
        time.sleep(1)
    print("timeout", file=sys.stderr)
    return 1


def answer(tag, pick=1, text="I think it squashes it to between 0 and 1"):
    for _ in range(20):
        p = panel(tag)
        if p is None or p["status"]:
            print(json.dumps({"tag": tag, "status": p and p["status"], "answers": p and p["answers"]}))
            return 0
        hits = [(r, a if isinstance(a, str) else tuple(a)) for r, a in p["hits"]]
        acts = {a: r for r, a in hits}
        nxt = next((r for r, a in hits if a in ("next", "finish")), None)
        choices = [(r, a) for r, a in hits if isinstance(a, tuple) and a[0] == "choice"]
        if nxt:
            click(*centre(nxt))
        elif choices:
            r, _ = choices[min(pick, len(choices)) - 1]
            click(*centre(r))
            done = next((r for r, a in hits if isinstance(a, tuple) and a[0] == "multi"), None)
            if done:  # several to pick: one is enough here, then Done
                click(*centre(done))
        elif any(isinstance(a, tuple) and a[0] == "input" for a in acts):
            r = next(r for r, a in hits if isinstance(a, tuple) and a[0] == "input")
            click(*centre(r))
            subprocess.run([WTYPE, " " + text]); time.sleep(0.3)
            subprocess.run([WTYPE, "-k", "Shift_L", "-k", "Return"]); time.sleep(0.6)
        elif any(isinstance(a, tuple) and a[0] == "task" for a in acts):
            r = next(r for r, a in hits if isinstance(a, tuple) and a[0] == "task" and a[2] == "done")
            click(*centre(r))
        elif any(isinstance(a, tuple) and a[0] == "button" for a in acts):
            r = [r for r, a in hits if isinstance(a, tuple) and a[0] == "button"][-1]
            click(*centre(r))
        elif "reveal" in acts:
            click(*centre(acts["reveal"]))
        else:
            print(f"nothing to press on {tag}: {p['hits']}", file=sys.stderr)
            return 1
    return 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[0] == "wait":
        sys.exit(wait(a[1], float(a[2]) if len(a) > 2 else 300))
    if a[0] == "answer":
        pick = int(a[a.index("--pick") + 1]) if "--pick" in a else 1
        text = a[a.index("--text") + 1] if "--text" in a else "I think it squashes it to between 0 and 1"
        sys.exit(answer(a[1], pick, text))
