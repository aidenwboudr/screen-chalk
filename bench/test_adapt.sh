#!/usr/bin/env bash
# The drawing keeps up with the screen (overlay Tracker), in the private headless Sway (never your real session):
#   1. switching workspace hides the marks; switching back brings them back
#   2. text scrolling under a circled line drops the circle
#   3. the card explained that line, so it docks as a tab (its text stays a peek away)
#   4. chalk peek brings a docked card back; peek again tucks it away
#   5. text appearing right next to a card (not on what it explains) moves the card to empty screen
# Run from a checkout on a machine with sway, foot, grim, jq, wtype and zenity:  bench/test_adapt.sh
set -euo pipefail
cd "$(dirname "$0")/.."
eval "$(bench/headless-sway.sh start)"
export CHALK_STATE=/tmp/chalk-test-adapt CHALK_NO_FORWARD=1
mkdir -p "$CHALK_STATE"; rm -f /tmp/adapt-go
fail() { echo "FAIL: $*"; ./chalk status; exit 1; }
stat() { ./chalk status | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['shown'], d['away'], d['docked'])"; }
shot() { grim -s 0.5 "/tmp/adapt-$1.png"; }
wait_for() {  # wait_for "<shown away docked>" seconds
  for _ in $(seq $(( $2 * 5 ))); do [[ $(stat) == "$1" ]] && return 0; sleep 0.2; done
  fail "wanted shown/away/docked = $1, have $(stat)"
}
./chalk stop >/dev/null 2>&1 || true; sleep 0.3
swaymsg -q '[title=".*"] kill' || true; sleep 0.3
swaymsg -q workspace 1
swaymsg -q exec "foot -T text sh -c 'seq 1 70 | sed \"s/^/line /\"; while [ ! -e /tmp/adapt-go ]; do sleep 0.2; done; seq 5000 5070 | sed \"s/^/other /\"; sleep 600'"
sleep 1.5
./chalk look >/dev/null
# a circle on a line near the bottom, a card explaining it
./chalk draw '[{"kind":"circle","box":[10,1180,80,18]},{"kind":"card","box":[10,1180,80,18],"title":"This line","text":"It is the last line printed so far."}]' >/dev/null
sleep 2.5
wait_for "2 0 0" 3
shot 0
sleep 2

echo "1. another workspace hides them, coming back shows them"
swaymsg -q workspace 2; wait_for "0 2 0" 4; shot 1
swaymsg -q workspace 1; wait_for "2 0 0" 4
echo "   ok"

echo "2+3. the text scrolls: the circle goes, the card gets out of the way"
sleep 4   # long enough for the tracker to have a picture of the screen as it is now
touch /tmp/adapt-go
for _ in $(seq 40); do s=$(stat); [[ $s == "0 0 1" || $s == "1 0 0" ]] && break; sleep 0.2; done
shot 2
s=$(stat)
[[ $s == "0 0 1" || $s == "1 0 0" ]] || fail "after the scroll: shown/away/docked = $s"
echo "   ok ($s: $([[ $s == "0 0 1" ]] && echo docked || echo moved))"

echo "4. peek brings it back, peek again tucks it away"
./chalk peek >/dev/null; wait_for "1 0 0" 3; sleep 0.8; shot 3
./chalk peek >/dev/null; wait_for "0 0 1" 3; sleep 0.6; shot 4
echo "   ok"
./chalk clear; rm -f /tmp/adapt-go
swaymsg -q '[title=".*"] kill' || true; sleep 0.5

echo "5. text runs up next to a card: it moves to empty screen"
# (a fixed font size: the 125-column lines have to reach the card whatever font foot is configured with)
swaymsg -q exec "foot -o font=monospace:size=12 -T text2 sh -c 'while [ ! -e /tmp/adapt-go ]; do sleep 0.2; done; for i in \$(seq 35); do printf \"%0125d\\n\" \$i; done; sleep 600'"
sleep 1.5
./chalk look >/dev/null
./chalk draw '[{"kind":"card","box":[1820,600,40,20],"title":"Over here","text":"A card sitting next to where the text is about to go."}]' >/dev/null
sleep 4
before=$(./chalk status | python3 -c "import json,sys; print(json.load(sys.stdin)['panels'])")
touch /tmp/adapt-go
for _ in $(seq 40); do
  after=$(./chalk status | python3 -c "import json,sys; print(json.load(sys.stdin)['panels'])")
  [[ $after != "$before" ]] && break; sleep 0.2
done
sleep 1; shot 5
[[ $(stat) == "1 0 0" && $after != "$before" ]] || fail "card should have moved and stayed open: $before -> $after, $(stat)"
echo "   ok: $before -> $after"
./chalk clear; rm -f /tmp/adapt-go
swaymsg -q '[title=".*"] kill' || true
echo PASS
