#!/usr/bin/env bash
# Run the scroll bench in a container with its own headless Sway (never your real session):
#   bench/scroll/run.sh [--chalk DIR] [bench.py args...]      e.g. --scenes web,term --out /out/r.json
# --chalk: the chalk checkout to test (default: this one). Results and recordings go to $CHALK_SCROLL_OUT
# (default ~/.local/share/chalk/scroll-bench, mounted at /out). `run.sh --replay ...` runs replay.py instead.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
chalk=$(cd "$here/../.." && pwd)
if [[ ${1:-} == --chalk ]]; then chalk=$(cd "$2" && pwd); shift 2; fi
out=${CHALK_SCROLL_OUT:-$HOME/.local/share/chalk/scroll-bench}
mkdir -p "$out"; chmod 777 "$out"  # (the container's user writes there)
script=bench.py
if [[ ${1:-} == --replay ]]; then script=replay.py; shift; fi
docker image inspect chalk-scroll-bench >/dev/null 2>&1 || docker build -t chalk-scroll-bench "$here"
# the bench and chalk-cap always come from this checkout (/tools), the chalk under test from --chalk (/chalk)
gpu=()  # the host's GPU when it has one: Sway composites and the apps render on it
if [[ -e /dev/dri/renderD128 ]]; then
  gpu=(--device /dev/dri -e CHALK_HEADLESS_RENDERER=gles2 --group-add "$(stat -c %g /dev/dri/renderD128)"
       --group-add "$(stat -c %g /dev/dri/card0)")
fi
exec docker run --rm --memory=6g --cpus=12 --shm-size=1g "${gpu[@]}" -v "$chalk":/chalk:ro -v "$here/../..":/tools:ro -v "$out":/out \
  -e CHALK_DIR=/chalk -e CHALK_CONFIG=/dev/null -e CHALK_TRACK_DEBUG -e CHALK_FOLLOW_DUMP -e PYTHONPATH=/tools/bench/scroll \
  chalk-scroll-bench /tools/bench/scroll/box.sh python3 "/tools/bench/scroll/$script" "$@"
