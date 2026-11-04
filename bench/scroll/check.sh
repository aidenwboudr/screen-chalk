#!/usr/bin/env bash
# Regression check for marks following what they mark (run before merging changes to follow.py / chalk-cap):
#   bench/scroll/check.sh          replays the recorded scrolls (web page, PDF, VS Code, terminal) through
#                                  follow.py: fails when under 90% of frames are tracked within 4 px or a
#                                  frame takes over 8 ms on average. Records them first if there are none.
#   bench/scroll/check.sh --live   then the live bench too (real apps, the real overlay; slower and noisier)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
out=${CHALK_SCROLL_OUT:-$HOME/.local/share/chalk/scroll-bench}
if ! ls "$out"/rec-*.npz >/dev/null 2>&1; then
  "$here/run.sh" --replay record
fi
"$here/run.sh" --replay eval --min 0.9 --max-ms 8
if [[ ${1:-} == --live ]]; then
  "$here/run.sh" --out /out/check.json
  python3 - "$out/check.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))["summary"]
print("live:", s)
sys.exit(0 if s["follow"] >= 0.6 else 1)
PY
fi
