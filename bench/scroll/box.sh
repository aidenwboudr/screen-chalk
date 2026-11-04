#!/usr/bin/env bash
# Inside the bench container: start the private headless Sway (1920x1280 @1.5, bench/headless-sway.sh), build
# chalk-cap, then run the given command with the Sway env set. Used by bench/scroll/run.sh.
set -euo pipefail
export XDG_RUNTIME_DIR=/tmp/rt
mkdir -p -m 700 "$XDG_RUNTIME_DIR"
mkdir -p /tmp/cap && /tools/cap/build.sh /tmp/cap >/dev/null && /tools/bench/vpointer/build.sh /tmp/cap >/dev/null
export PATH=/tmp/cap:$PATH CHALK_CAP_DIR=/tmp/cap
eval "$(/tools/bench/headless-sway.sh start)"
grep -q "renderer\|GLES\|EGL" "$XDG_RUNTIME_DIR/chalk-headless/log" 2>/dev/null || true
export CHALK_SOCK
swaymsg -q 'seat * hide_cursor 1' || true
eval "$(dbus-launch --sh-syntax)"
exec "$@"
