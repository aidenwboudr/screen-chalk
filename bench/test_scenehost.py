#!/usr/bin/python3
"""scenehost.py, the way the Teach app drives it: open a scene, drag its slider, Next into a gated step (held
until released), and how fast a frame follows the user's pointer. Run anywhere with the system python's gi:
  bench/test_scenehost.py            (exit 1 on a failure; prints the latency)"""
import json, os, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = json.load(open(os.path.join(HERE, "visual2", "examples", "rc-lowpass.json")))
p = subprocess.Popen(["/usr/bin/python3", os.path.join(HERE, "..", "scenehost.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
msgs, sent, lat = [], {}, []


def read():
    for line in p.stdout:
        m = json.loads(line)
        m["at"] = time.monotonic()
        msgs.append(m)
        if m["t"] == "frame" and m["ack"] in sent:
            lat.append(m["at"] - sent.pop(m["ack"]))


threading.Thread(target=read, daemon=True).start()
send = lambda m: (p.stdin.write(json.dumps(m) + "\n"), p.stdin.flush())
last = lambda t: next((m for m in reversed(msgs) if m["t"] == t), None)
fails = []
check = lambda ok, what: ok or fails.append(what)

send({"op": "open", "id": "s1", "spec": SPEC, "gates": [1]})
time.sleep(2.5)
f = last("frame")
check(f and f["w"] > 300 and f["png"], "a frame came in")
v0 = last("state")["values"]["f"]
sz = max(f.get("zones") or [[0, 505, 1, 0]], key=lambda z: z[2])   # the slider (the widest zone): the drag below lands on it
slider_y = sz[1] + sz[3] / 2
M = 8   # the frame's margin (the card's shadow): pointer x/y are on the frame, zones too
send({"op": "pointer", "id": "s1", "t": "down", "x": 300, "y": slider_y, "seq": 1})
for i in range(60):
    sent[i + 2] = time.monotonic()
    send({"op": "pointer", "id": "s1", "t": "move", "x": 150 + i * 6, "y": slider_y, "seq": i + 2})
    time.sleep(0.016)
send({"op": "pointer", "id": "s1", "t": "up", "x": 510, "y": slider_y, "seq": 99})
time.sleep(0.5)
check(last("state")["values"]["f"] != v0, "the slider followed the drag")
send({"op": "act", "id": "s1", "action": "next"})
time.sleep(0.4)
check(last("gate") and last("gate")["step"] == 1 and last("state")["step"] == 0, "Next into a gated step is held")
send({"op": "release", "id": "s1", "step": 1})
time.sleep(0.6)
check(last("state")["step"] == 1, "released, it goes on to the step")
# touch zones: every frame names what takes the pointer, in the pointer's own coordinates
f = last("frame")
zones = f.get("zones") or []
inside = lambda x, y: any(zx <= x <= zx + zw and zy <= y <= zy + zh for zx, zy, zw, zh in zones)
check(len(zones) >= 3 and max(z[2] for z in zones) > f["w"] / 2, f"the frame names its zones, the slider's track among them ({zones})")
import base64, struct
pw = struct.unpack(">I", base64.b64decode(f["png"])[16:20])[0]   # the PNG's width, from its header
check(abs(pw / f["scale"] - f["w"]) <= 1, f"frame w is the PNG's own layout width ({pw}/{f['scale']} vs {f['w']})")
v1 = last("state")["values"]["f"]
send({"op": "pointer", "id": "s1", "t": "down", "x": f["w"] / 2, "y": 200, "seq": 200})   # on the plot: no zone there
send({"op": "pointer", "id": "s1", "t": "up", "x": f["w"] / 2, "y": 200, "seq": 201})
time.sleep(0.4)
check(not inside(f["w"] / 2, 200) and last("state")["values"]["f"] == v1 and last("state")["step"] == 1, "a press outside the zones changes nothing")
nx = max(zones, key=lambda z: (z[1], z[0]))   # Next: the lowest, rightmost zone
send({"op": "pointer", "id": "s1", "t": "down", "x": nx[0] + nx[2] / 2, "y": nx[1] + nx[3] / 2, "seq": 202})
send({"op": "pointer", "id": "s1", "t": "up", "x": nx[0] + nx[2] / 2, "y": nx[1] + nx[3] / 2, "seq": 203})
time.sleep(0.5)
check(last("state")["step"] != 1, f"a press on the Next zone steps ({last('state')['step']})")
# fit: a phone's width
w_own = last("frame")["w"]
send({"op": "fit", "id": "s1", "w": 380, "seq": 300})
time.sleep(0.5)
f = last("frame")
check(f["w"] <= 380 and f["w"] > 300 and all(z[0] + z[2] <= f["w"] + 1 for z in f["zones"]), f"fit 380: the frame fits ({f['w']}, zones {f['zones']})")
send({"op": "fit", "id": "s1", "w": 0, "seq": 301})
time.sleep(0.5)
check(last("frame")["w"] == w_own, f"fit 0: chalk's own width again ({last('frame')['w']} vs {w_own})")
send({"op": "open", "id": "s2", "spec": SPEC, "gates": [], "fit": 380})
time.sleep(1.5)
f2 = next((m for m in reversed(msgs) if m["t"] == "frame" and m["id"] == "s2"), None)
check(f2 and f2["w"] <= 380, "open with fit: the first frame already fits")
check(not last("error"), f"no errors: {last('error')}")
lat.sort()
med = lat[len(lat) // 2] * 1e3 if lat else 1e9
print(f"input -> frame: median {med:.0f} ms, p90 {lat[int(len(lat) * 0.9)] * 1e3 if lat else 0:.0f} ms over {len(lat)} frames")
check(med < 120, f"a frame follows his pointer within 120 ms (median {med:.0f})")
p.kill()
print("FAIL: " + "; ".join(fails) if fails else "PASS")
sys.exit(1 if fails else 0)
