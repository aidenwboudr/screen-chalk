#!/usr/bin/env bash
# Scene boards (scene.py) in the real overlay, in the private headless Sway (never your real session), driven with a
# real pointer (bench/vpointer):
#   1. a scene board appears, its frames keep changing while it animates in (and while time-driven parts run)
#   2. dragging the slider's knob to the right end sets the parameter to its max; the board repaints
#   3. Next walks to the next step; the board stays click-through outside itself
#   4. a corrected scene (the check pass) swaps in where the board stands: same step, his slider value kept
#   5. a streamed board: it opens with no objects and each scene_add draws more in
# Run from a checkout on a machine with sway, foot, grim, jq, wtype and zenity:  bench/test_scene.sh
set -euo pipefail
cd "$(dirname "$0")/.."
eval "$(bench/headless-sway.sh start)"
export CHALK_STATE=/tmp/chalk-test-scene CHALK_NO_FORWARD=1 PATH="$HOME/.local/bin:$PATH"
mkdir -p "$CHALK_STATE"; rm -f /tmp/scene-*.png
fail() { echo "FAIL: $*"; ./chalk status; exit 1; }
info() { python3 -c "import json,socket,sys; s=socket.socket(socket.AF_UNIX); s.connect('$CHALK_SOCK'); s.sendall(b'{\"op\":\"panel_info\"}\n'); print(s.makefile().readline())"; }
q() { info | python3 -c "import json,sys; d=json.load(sys.stdin); p=d['panels'][0]; print(eval(sys.argv[1]))" "$1"; }
bench/vpointer/build.sh
rm -f /tmp/scene-ptr; mkfifo /tmp/scene-ptr
bench/vpointer/vpointer 1920 1280 < /tmp/scene-ptr > /dev/null & ptr=$!
exec 7> /tmp/scene-ptr
trap 'exec 7>&-; kill $ptr 2>/dev/null; rm -f /tmp/scene-ptr' EXIT

./chalk stop >/dev/null 2>&1 || true; sleep 0.3
swaymsg -q '[title=".*"] kill' || true; sleep 0.3
swaymsg -q exec "foot -T editor sh -c 'seq 1 60; sleep 600'"; sleep 1.2
./chalk look >/dev/null

echo "1. a scene board animates in"
python3 -c "import json; d=json.load(open('bench/visual2/examples/rc-lowpass.json')); d.update(kind='panel', tag='s1', at=[1100, 120]); print(json.dumps([d]))" > /tmp/scene-spec.json
./chalk draw "$(cat /tmp/scene-spec.json)" >/dev/null
sleep 0.5; grim /tmp/scene-a.png; sleep 0.6; grim /tmp/scene-b.png
cmp -s /tmp/scene-a.png /tmp/scene-b.png && fail "the board didn't change while animating in"
echo "   ok: frames differ while it builds"

echo "2. drag the slider knob to the right end"
read -r X0 X1 Y < <(q "' '.join(str(v) for v in next(([b[0]+b[2]*0+12, b[0]+b[2]-12, b[1]+b[3]//2] for b,a in p['hits'] if a[0]=='slider')))")
f0=$(q "p['values']['f']")
KX=$(python3 -c "import math; lo,hi,v=50,20000,$f0; print(int($X0 + (math.log(v)-math.log(lo))/(math.log(hi)-math.log(lo))*($X1-$X0)))")
echo "move $KX $Y" >&7; sleep 0.2; echo "down" >&7; sleep 0.1
for x in $(seq "$KX" 40 $((X1 + 60))); do echo "move $x $Y" >&7; sleep 0.02; done
echo "up" >&7; sleep 0.4
f1=$(q "p['values']['f']")
[[ $f1 == 20000.0 ]] || fail "after the drag f = $f1 (was $f0)"
grim /tmp/scene-c.png
echo "   ok: f $f0 -> $f1"

echo "3. Next steps through; outside the board stays click-through"
read -r NX NY < <(q "' '.join(str(int(b[0]+b[2]/2))+' '+str(int(b[1]+b[3]/2)) for b,a in p['hits'] if a=='next')")
echo "move $NX $NY" >&7; sleep 0.2; echo "click" >&7; sleep 0.5
[[ $(q "p['step']") == 1 ]] || fail "Next didn't advance (step $(q "p['step']"))"
echo "move 200 300" >&7; sleep 0.2; echo "click" >&7; sleep 0.3
[[ $(swaymsg -t get_tree | python3 -c "import json,sys
def walk(n):
    if n.get('focused'): print(n.get('name')); return
    for c in n.get('nodes', []) + n.get('floating_nodes', []): walk(c)
walk(json.load(sys.stdin))") == editor ]] || fail "a click outside the board didn't reach the editor"
grim /tmp/scene-d.png
echo "   ok: step 2, editor still gets clicks"
echo "4. a corrected scene swaps in place"
r0=$(q "p['rect']")
python3 -c "import json; d=json.load(open('bench/visual2/examples/rc-lowpass.json')); d['scene']['title']='Corrected title'; print(json.dumps([{'kind':'scene_replace','tag':'s1','scene':d['scene']}]))" > /tmp/scene-fix.json
./chalk draw "$(cat /tmp/scene-fix.json)" --keep >/dev/null
sleep 0.5
[[ $(q "p['step']") == 1 ]] || fail "the swap reset the step ($(q "p['step']"))"
[[ $(q "p['values']['f']") == 20000.0 ]] || fail "the swap lost his slider value ($(q "p['values']['f']"))"
[[ $(q "len(d['panels'])") == 1 ]] || fail "the swap left two boards"
r1=$(q "p['rect']"); [[ ${r0%,*} == "${r1%,*}" ]] || fail "the board moved: $r0 -> $r1"
grim /tmp/scene-e.png
echo "   ok: step 2, f 20000 kept, same spot"
echo "5. a streamed board fills in as objects arrive"
./chalk clear >/dev/null; sleep 0.3
./chalk draw '[{"kind":"panel","tag":"s2","at":[1100,120],"scene":{"title":"Streaming","size":[400,200],"streaming":true,"steps":[{"show":["a"]},{"show":["b"]}],"objects":[]}}]' >/dev/null
sleep 0.3; [[ $(q "p['objects']") == 0 ]] || fail "a streamed board didn't open empty"
./chalk draw '[{"kind":"scene_add","tag":"s2","objects":[{"id":"a","circle":[100,100,40]}]}]' --keep >/dev/null
./chalk draw '[{"kind":"scene_add","tag":"s2","objects":[{"id":"b","rect":[200,60,120,80]}]}]' --keep >/dev/null
sleep 0.4; [[ $(q "p['objects']") == 2 ]] || fail "scene_add didn't land ($(q "p['objects']") objects)"
[[ $(q "len(d['panels'])") == 1 ]] || fail "scene_add made a second board"
echo "   ok: 0 -> 2 objects on one board"
echo "all ok"
