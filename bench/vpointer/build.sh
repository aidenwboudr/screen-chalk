#!/usr/bin/env bash
# Build the tests' virtual pointer (vpointer.c) into ./vpointer (gcc + wayland-scanner).
set -euo pipefail
cd "$(dirname "$0")"
out=${1:-.}  # (the scroll bench builds it elsewhere: its checkout is mounted read-only)
[[ $out/vpointer -nt vpointer.c ]] && exit 0
gen=$(mktemp -d); trap 'rm -rf "$gen"' EXIT
wayland-scanner client-header wlr-virtual-pointer-unstable-v1.xml "$gen/wlr-virtual-pointer-unstable-v1-client-protocol.h"
wayland-scanner private-code wlr-virtual-pointer-unstable-v1.xml "$gen/wlr-virtual-pointer-protocol.c"
gcc -O2 -I"$gen" -o "$out/vpointer" vpointer.c "$gen/wlr-virtual-pointer-protocol.c" -lwayland-client -lm
