#!/usr/bin/env bash
# Start a private headless Sway (never touches your real session) and print
# the env lines to talk to it:  eval "$(bench/headless-sway.sh start)" ; ... ; bench/headless-sway.sh stop
# Output is 1920x1280 logical at scale 1.5 (a common laptop panel). CHALK_HEADLESS_RENDERER=gles2 renders on a GPU
# (the scroll bench's container can use the host's). The env it prints sets CHALK_CONFIG=/dev/null, so chalk run
# against it uses pure defaults, never the machine's ~/.config/chalk/env (which may point at a real screen).
set -euo pipefail
RT="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
DIR="$RT/chalk-headless"
case "${1:-start}" in
start)
  mkdir -p "$DIR"
  if [[ -f $DIR/pid ]] && kill -0 "$(cat "$DIR/pid")" 2>/dev/null; then cat "$DIR/env"; exit 0; fi
  cat > "$DIR/config" <<'EOF'
output HEADLESS-1 resolution 2880x1920 scale 1.5 bg #f4f4f2 solid_color
default_border none
focus_follows_mouse no
EOF
  env -u WAYLAND_DISPLAY -u SWAYSOCK -u DISPLAY WLR_BACKENDS=headless WLR_RENDERER="${CHALK_HEADLESS_RENDERER:-pixman}" \
    WLR_LIBINPUT_NO_DEVICES=1 SWAYSOCK="$DIR/sway.sock" \
    setsid sway -c "$DIR/config" >"$DIR/log" 2>&1 < /dev/null &
  pid=$!
  echo $pid > "$DIR/pid"
  for _ in $(seq 50); do
    # the display is the wayland-N.lock this sway holds (a name from an earlier run can come back)
    new=$(ls -l /proc/$pid/fd 2>/dev/null | grep -oE 'wayland-[0-9]+\.lock' | head -1 | sed 's/\.lock$//' || true)
    [[ -n $new && -S $DIR/sway.sock ]] && break
    sleep 0.1
  done
  [[ -n ${new:-} ]] || { echo "headless sway failed to start:" >&2; tail "$DIR/log" >&2; exit 1; }
  printf 'export WAYLAND_DISPLAY=%s SWAYSOCK=%s CHALK_SOCK=%s CHALK_CONFIG=/dev/null\n' "$new" "$DIR/sway.sock" "$DIR/chalk.sock" > "$DIR/env"
  cat "$DIR/env"
  ;;
stop)
  [[ -f $DIR/pid ]] && kill "$(cat "$DIR/pid")" 2>/dev/null || true
  rm -f "$DIR/pid" "$DIR/env"
  ;;
esac
