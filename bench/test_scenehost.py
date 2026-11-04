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
slider_y = 505   # the rc-lowpass card's frequency slider
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
check(not last("error"), f"no errors: {last('error')}")
lat.sort()
med = lat[len(lat) // 2] * 1e3 if lat else 1e9
print(f"input -> frame: median {med:.0f} ms, p90 {lat[int(len(lat) * 0.9)] * 1e3 if lat else 0:.0f} ms over {len(lat)} frames")
check(med < 120, f"a frame follows his pointer within 120 ms (median {med:.0f})")
p.kill()
print("FAIL: " + "; ".join(fails) if fails else "PASS")
sys.exit(1 if fails else 0)
