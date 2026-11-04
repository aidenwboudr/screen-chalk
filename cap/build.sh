#!/usr/bin/env bash
# Build chalk-cap and libchalkmatch.so (follow.py's inner loop) next to this script (or into $1).
# Needs gcc, wayland-scanner, libwayland-dev.
set -euo pipefail
cd "$(dirname "$0")"
out=${1:-.}
gen=$(mktemp -d); trap 'rm -rf "$gen"' EXIT
for p in wlr-screencopy-unstable-v1; do
  wayland-scanner client-header "$p.xml" "$gen/$p-client-protocol.h"
  wayland-scanner private-code "$p.xml" "$gen/$p-protocol.c"
done
flags="-O2 -Wall -Wno-unused-parameter -I$gen"
gcc $flags -o "$out/chalk-cap" cap.c "$gen/wlr-screencopy-unstable-v1-protocol.c" -lwayland-client
gcc -O3 -Wall -shared -fPIC -o "$out/libchalkmatch.so" match.c -lm
