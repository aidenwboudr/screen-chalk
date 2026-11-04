#!/usr/bin/env bash
# `chalk watch` against the private headless Sway (never your real session): it must
#   1. ignore a blinking text caret (a GTK entry sitting idle) and time out,
#   2. return "screen" soon after a window's content changes and holds still, with `where` around it,
#   3. not wait out the change while chalk's own strokes are still animating.
# Run from a checkout on a machine with sway, foot, grim, jq, wtype and zenity:  bench/test_watch.sh
set -euo pipefail
cd "$(dirname "$0")/.."
eval "$(bench/headless-sway.sh start)"
export CHALK_STATE=/tmp/chalk-test-watch CHALK_NO_FORWARD=1
mkdir -p "$CHALK_STATE"
fail() { echo "FAIL: $*"; exit 1; }
swaymsg -q '[title=".*"] kill' || true
sleep 0.5

echo "1. idle caret: zenity --entry with a blinking cursor, 6 s"
swaymsg -q exec "zenity --entry --title=caret --text='type here'"
# wait for the dialog itself, then for its first paints to settle (a slow machine shows it seconds later)
for _ in $(seq 50); do swaymsg -t get_tree | grep -q '"name": "caret"' && break; sleep 0.2; done
sleep 2
out=$(./chalk watch --timeout 6 --json)
reason=$(jq -r .reason <<<"$out")
[[ $reason == timeout ]] || fail "idle caret counted as a change: $(jq -c 'del(.elements)' <<<"$out")"
echo "   ok (timeout after $(jq .waited <<<"$out") s)"
swaymsg -q '[title=".*"] kill'; sleep 0.5

echo "2. a terminal prints a big block of text 3 s in"
swaymsg -q exec "foot -T target sh -c 'sleep 3; for i in \$(seq 12); do echo \"line \$i: the quick brown fox jumps over the lazy dog\"; done; sleep 60'"
sleep 1.5
t0=$(date +%s.%N)
out=$(./chalk watch --timeout 20 --json)
t1=$(date +%s.%N)
reason=$(jq -r .reason <<<"$out")
[[ $reason == screen ]] || fail "missed the change: $(jq -c 'del(.elements)' <<<"$out")"
echo "   ok: $(jq -c '{reason, waited, where, cells}' <<<"$out"), returned $(python3 -c "print(round($t1-$t0,2))") s after start"
swaymsg -q '[title=".*"] kill'; sleep 0.5

echo "3. chalk draws (animated) right before the watch, nothing else changes, 5 s"
swaymsg -q exec "foot -T calm sh -c 'echo steady; sleep 60'"
sleep 1.5
./chalk look >/dev/null
./chalk draw '[{"kind":"circle","box":[300,300,200,80],"label":"click here"},{"kind":"card","box":[300,300,200,80],"title":"Step 1","text":"Open the menu."}]' >/dev/null
out=$(./chalk watch --timeout 5 --json)
[[ $(jq -r .reason <<<"$out") == timeout ]] || fail "its own drawing counted as a change: $(jq -c 'del(.elements)' <<<"$out")"
echo "   ok"
./chalk clear
swaymsg -q '[title=".*"] kill' || true
echo "PASS"
