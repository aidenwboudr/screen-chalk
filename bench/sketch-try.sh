#!/usr/bin/env bash
# Try `chalk ask` on bench pages inside the private headless Sway (from a checkout on a machine with sway):
#   bench/sketch-try.sh PAGE "question" OUT.png
# Opens PAGE in Chrome there, asks, waits for the drawing to finish, screenshots it.
set -euo pipefail
cd "$(dirname "$0")/.."
eval "$(bench/headless-sway.sh start)"
export CHALK_STATE=/tmp/chalk-test
page=$1 q=$2 out=$3
pkill -f "[u]ser-data-dir=/tmp/chalk-chrome" || true
sleep 0.5
dir=pages; [[ -f bench/pages/$page ]] || dir=pages-hard
(setsid google-chrome --ozone-platform=wayland --user-data-dir=/tmp/chalk-chrome --no-first-run \
  --app="file://$PWD/bench/$dir/$page" >/dev/null 2>&1 &)
sleep 4
./chalk clear
./chalk ask "$q" --json | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("first", d["first_shape_ms"], "marks", d["marks_done_ms"], "sketch", d["sketch_ms"], d.get("sketch_error") or "")
print([s["kind"] for s in d["answer"]["shapes"]], "diagram:", (d.get("sketch") or {}).get("draw"))'
sleep 5
grim -s 1 "$out"
