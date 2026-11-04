#!/usr/bin/env bash
# Panels (panel.py) in the private headless Sway (never your real session), driven with a real pointer (Sway's
# seat cursor) and real keys (wtype):
#   (the pointer is bench/vpointer: Sway's own `seat cursor` moves no client's pointer without a device)
#   1. a question anchored to a code line sits beside it, not on it; a click on a choice answers it (keyboard
#      untouched), right/wrong + why show, and `chalk panel --wait` prints the answer
#   2. a typed answer: a click on the field takes the keyboard, Enter sends, and the keys go back to the editor
#   3. Super+; (chalk focus) + number keys; Esc hands the keyboard back
#   4. a card with code gets Copy buttons: a click copies block 1, `chalk copy` then block 2 (wl-paste checks)
#   5. a waiting question outlives the next plain draw and the tutor's clear; the yellow dot tucks it away, a click on the tab
#      brings it back, the red dot dismisses it
#   6. chalk watch doesn't count typing inside a panel as him working
# Run from a checkout on a machine with sway, foot, grim, jq, wtype and zenity:  bench/test_panel.sh
set -euo pipefail
cd "$(dirname "$0")/.."
eval "$(bench/headless-sway.sh start)"
export CHALK_STATE=/tmp/chalk-test-panel CHALK_NO_FORWARD=1 PATH="$HOME/.local/bin:$PATH"
mkdir -p "$CHALK_STATE"; rm -f /tmp/panel-*.json /tmp/panel-got
fail() { echo "FAIL: $*"; ./chalk status; exit 1; }
shot() { grim -s 0.5 "/tmp/panel-$1.png"; }
info() { python3 -c "import json,socket,sys; s=socket.socket(socket.AF_UNIX); s.connect('$CHALK_SOCK'); s.sendall(b'{\"op\":\"panel_info\"}\n'); print(s.makefile().readline())"; }
hit() {  # hit <tag> <action json> -> centre "x y" of that target on screen
  info | python3 -c "
import json,sys; d=json.load(sys.stdin); want=json.loads(sys.argv[2])
for p in d['panels']:
    if p['tag']==sys.argv[1]:
        for (x,y,w,h),a in p['hits']:
            if a==want: print(int(x+w/2), int(y+h/2)); sys.exit()
sys.exit('no target '+sys.argv[2])" "$1" "$2"
}
# a real pointer: the headless Sway has no input devices, so bench/vpointer makes one and keeps it alive
bench/vpointer/build.sh
rm -f /tmp/panel-ptr; mkfifo /tmp/panel-ptr
bench/vpointer/vpointer 1920 1280 < /tmp/panel-ptr > /dev/null & ptr=$!
exec 7> /tmp/panel-ptr
trap 'exec 7>&-; kill $ptr 2>/dev/null; rm -f /tmp/panel-ptr' EXIT
click() { echo "move $1 $2" >&7; sleep 0.2; echo "click" >&7; sleep 0.4; }
keys() { info | python3 -c "import json,sys; print(json.load(sys.stdin)['keys'])"; }
type_() { wtype " $1"; sleep 0.3; }   # wtype in a headless Sway drops the first key of each string
key_() { wtype -k Shift_L -k "$1"; sleep 0.4; }

./chalk stop >/dev/null 2>&1 || true; sleep 0.3
swaymsg -q '[title=".*"] kill' || true; sleep 0.3
swaymsg -q workspace 1
cat > /tmp/panel-code.py <<'EOF'
import math

def sigmoid(z):
    return 1 / (1 + math.exp(-z))

def forward(x, W1, b1, w2, b2):
    h = [sigmoid(sum(xi * wi for xi, wi in zip(x, w)) + b) for w, b in zip(W1, b1)]
    a = sigmoid(sum(hi * wi for hi, wi in zip(h, w2)) + b2)
    return h, a
EOF
# the "editor": shows the code, then reads one line of keys (to prove they reach it after a panel lets go)
swaymsg -q exec "foot -T editor sh -c 'cat -n /tmp/panel-code.py; read x; echo \"\$x\" > /tmp/panel-got; sleep 600'"
sleep 1.5
./chalk look >/dev/null

echo "1. a question beside its line, answered with a click"
LINE='[30,150,620,22]'
./chalk panel "{\"tag\":\"q1\",\"sub\":\"Neural network · step 10 of 21\",\"box\":$LINE,\"questions\":[{\"kind\":\"choice\",\"prompt\":\"What does \`sigmoid(0)\` return?\",\"options\":[\"0\",\"0.5\",\"1\"],\"answer\":1,\"why\":\"\$e^0 = 1\$, so it's 1/(1+1).\"}]}" --wait > /tmp/panel-q1.json &
waiter=$!
sleep 1.2; shot 1a
info | python3 -c "
import json,sys; d=json.load(sys.stdin); p=[p for p in d['panels'] if p['tag']=='q1'][0]; x,y,w,h=p['rect']; lx,ly,lw,lh=$LINE
assert not (x < lx+lw and lx < x+w and y < ly+lh and ly < y+h), ('panel covers its line', p['rect'])
print('   placed at', p['rect'])"
read -r X Y < <(hit q1 '["choice", 0, 1]')
click "$X" "$Y"
wait $waiter
grep -q '"choice": 1' /tmp/panel-q1.json || fail "q1 result: $(cat /tmp/panel-q1.json)"
[[ $(keys) == False ]] || fail "a click on a choice took the keyboard"
sleep 0.4; shot 1b
echo "   ok: $(cat /tmp/panel-q1.json)"

echo "2. a typed answer: click the field, type, Enter"
./chalk panel '{"tag":"q2","title":"Quick check","questions":[{"kind":"text","prompt":"In NumPy, how do you write the dot product of `a` and `b`?"}]}' --wait > /tmp/panel-q2.json &
waiter=$!
sleep 1
read -r X Y < <(hit q2 '["input", 0]')
click "$X" "$Y"
[[ $(keys) == True ]] || fail "clicking the field didn't take the keyboard"
type_ "a @ b"; shot 2a
key_ Return
wait $waiter
grep -q '"text": "a @ b"' /tmp/panel-q2.json || fail "q2 result: $(cat /tmp/panel-q2.json)"
[[ $(keys) == False ]] || fail "the keyboard didn't come back after Enter"
type_ "back in the editor"; key_ Return
grep -q "back in the editor" /tmp/panel-got || fail "keys after the panel didn't reach the editor: $(cat /tmp/panel-got 2>/dev/null)"
echo "   ok: $(cat /tmp/panel-q2.json)"

echo "3. Super+; then number keys; Esc gives the keys back"
./chalk panel '{"tag":"q3","questions":[{"kind":"choice","prompt":"Which one is elementwise?","options":["a @ b","a * b","a.dot(b)"],"answer":1},{"kind":"task","prompt":"Now change line 7 to use it."}]}' --wait > /tmp/panel-q3.json &
waiter=$!
sleep 1
./chalk focus >/dev/null; sleep 0.4
[[ $(keys) == True ]] || fail "chalk focus didn't give the panel the keyboard"
type_ "2"; shot 3a
key_ Return   # Next
type_ "d"
wait $waiter
grep -q '"choice": 1' /tmp/panel-q3.json && grep -q '"task": "done"' /tmp/panel-q3.json || fail "q3 result: $(cat /tmp/panel-q3.json)"
[[ $(keys) == False ]] || fail "the keyboard stayed with a panel that's done"
./chalk panel '{"tag":"q3b","questions":[{"kind":"choice","prompt":"Esc test","options":["x","y"]}]}' >/dev/null; sleep 0.8
./chalk focus >/dev/null; sleep 0.4; key_ Escape
[[ $(keys) == False ]] || fail "Esc didn't hand the keyboard back"
./chalk remove q3b
echo "   ok: $(cat /tmp/panel-q3.json)"

echo "4. code gets Copy buttons"
./chalk draw '[{"kind":"card","tag":"code","title":"Add this to xor.py","text":"First the backward pass:\n```python\ndef backward(x, y, h, a, w2):\n    d_out = 2 * (a - y) * a * (1 - a)\n    return d_out\n```\nthen the check:\n```python\nprint(backward([0, 1], 1, *forward([0, 1], W1, b1, w2, b2)[::-1], w2))\n```"}]' >/dev/null
sleep 1.2; shot 4a
read -r X Y < <(hit code '["copy", 1]')
click "$X" "$Y"; sleep 0.3; shot 4b
wl-paste -n | grep -q "def backward" || fail "Copy didn't put block 1 on the clipboard: $(wl-paste -n | head -2)"
./chalk copy >/dev/null; sleep 0.3
wl-paste -n | grep -q "^print(backward" || fail "chalk copy didn't move on to block 2: $(wl-paste -n | head -2)"
echo "   ok"

echo "5. a waiting question survives a redraw; tuck away, bring back, dismiss"
./chalk panel '{"tag":"q5","questions":[{"kind":"choice","prompt":"Still here after a redraw?","options":["yes","no"]}]}' --wait > /tmp/panel-q5.json &
waiter=$!
sleep 1
./chalk draw '[{"kind":"circle","box":[30,150,120,22]}]' >/dev/null; sleep 1
info | grep -q '"q5"' || fail "a plain redraw took the waiting question away"
./chalk clear --marks; sleep 0.8
info | grep -q '"q5"' || fail "the tutor's clear (--marks) took the waiting question away"
read -r X Y < <(hit q5 '"dock"'); click "$X" "$Y"; sleep 0.6
info | python3 -c "import json,sys; d=json.load(sys.stdin); assert [p for p in d['panels'] if p['tag']=='q5'][0]['docked']" || fail "the yellow dot didn't tuck it away"
shot 5a
read -r X Y < <(info | python3 -c "
import json,sys; d=json.load(sys.stdin); x,y,w,h=[p for p in d['panels'] if p['tag']=='q5'][0]['rect']; print(int(x+w/2), int(y+h/2))")
click "$X" "$Y"; sleep 0.8
info | python3 -c "import json,sys; d=json.load(sys.stdin); assert not [p for p in d['panels'] if p['tag']=='q5'][0]['docked']" || fail "a click on the tab didn't bring it back"
read -r X Y < <(hit q5 '"close"'); click "$X" "$Y"
wait $waiter
grep -q '"dismissed"' /tmp/panel-q5.json || fail "q5 result: $(cat /tmp/panel-q5.json)"
echo "   ok"

echo "6. typing in a panel isn't a screen change for watch"
./chalk clear; sleep 0.6
./chalk panel '{"tag":"q6","questions":[{"kind":"text","prompt":"Type a long answer here"}]}' >/dev/null; sleep 1
# park the pointer on the panel: where a compositor draws the cursor into the frames (Sway 1.9's headless output
# does), its shape changing when the keys move is a change on screen too, and it belongs to answering, like the typing
read -r X Y < <(info | python3 -c "import json,sys; r=[p['rect'] for p in json.load(sys.stdin)['panels'] if p['tag']=='q6'][0]; print(int(r[0]+r[2]/2), int(r[1]+r[3]/2))")
echo "move $X $Y" >&7; sleep 0.5
./chalk watch --timeout 6 --settle 1 --json > /tmp/panel-watch.json &
w=$!
sleep 1.5
./chalk focus >/dev/null; sleep 0.3
type_ "this is me typing an answer, not working in the editor"
wait $w
grep -q '"reason": "timeout"' /tmp/panel-watch.json || fail "watch saw panel typing: $(head -c 200 /tmp/panel-watch.json)"
key_ Escape
echo "   ok"
./chalk clear
swaymsg -q '[title=".*"] kill' || true
echo PASS
