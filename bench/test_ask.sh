#!/usr/bin/env bash
# `chalk ask` end to end in the private headless Sway, with a stand-in for claude (bench/fake-claude: no model,
# no key): the question goes in, the streamed circle and the note card land on the screen, ask.log records it.
# Run from a checkout on a machine with sway and grim:  bench/test_ask.sh
set -euo pipefail
cd "$(dirname "$0")/.."
eval "$(bench/headless-sway.sh start)"
export CHALK_STATE=/tmp/chalk-test-ask CHALK_NO_FORWARD=1 CHALK_CLAUDE="$PWD/bench/fake-claude" FAKE_LOG=/tmp/chalk-test-ask/fake.log
rm -rf "$CHALK_STATE"; mkdir -p "$CHALK_STATE"
fail() { echo "FAIL: $*"; exit 1; }
./chalk stop >/dev/null 2>&1 || true
./chalk clear >/dev/null 2>&1 || true

echo "1. a question: the marks call's circle and the note card are drawn"
out=$(./chalk ask --json "which button saves the file?")
jq -e '.answer.shapes[0].kind == "circle" and (.first_shape_ms != null)' <<<"$out" >/dev/null || fail "ask: $out"
st=$(./chalk status)
jq -e '.shapes >= 2' <<<"$st" >/dev/null || fail "expected the circle and the note on screen: $st"
grep -q "which button saves the file" "$CHALK_STATE/ask.log" || fail "ask.log doesn't have the question"
grep -q 'The user asks: \\"which button saves the file?\\"' "$FAKE_LOG" || fail "the prompt didn't carry the question: $(head -c 400 "$FAKE_LOG")"
echo "   ok (first stroke $(jq .first_shape_ms <<<"$out") ms)"

echo "2. a follow-up keeps the first answer and says so in the prompt"
./chalk ask --json "and the one next to it?" >/dev/null
grep -q "This is a follow-up question" "$FAKE_LOG" || fail "the follow-up had no conversation context"
echo "   ok"

echo "3. a board the user closed: the next follow-up is told it's gone (not that it's on screen) and puts it back"
op() { python3 -c 'import json, socket, sys; s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.sendall(sys.argv[2].encode() + b"\n"); print(s.makefile().readline())' "$CHALK_SOCK" "$1"; }
board='{"draw": true, "at": [1100, 200], "scene": {"title": "emf peak", "size": [420, 260], "objects": [{"id": "t", "text": "sin peaks at 1", "at": [40, 120]}], "steps": [{"show": ["t"], "say": "the peak"}]}}'
out=$(CHALK_SCENE_CHECK=0 FAKE_SKETCH="$board" ./chalk ask --json "why is that the max?")
tag=$(op '{"op": "tags"}' | jq -r '.tags | to_entries[] | select(.key | startswith("b")) | .key')
[[ -n $tag ]] || fail "no board drawn: $(op '{"op": "tags"}')"
op "{\"op\": \"panel_act\", \"tag\": \"$tag\", \"action\": \"close\"}" >/dev/null   # its red dot
sleep 1
op '{"op": "tags"}' | jq -e --arg t "$tag" '.tags[$t] == null' >/dev/null || fail "the board didn't close"
: > "$FAKE_LOG"
CHALK_SCENE_CHECK=0 FAKE_SKETCH="{\"draw\": false, \"restore\": [\"$tag\"]}" ./chalk ask --json "help" > "$CHALK_STATE/out.json"
grep -q "$tag: board \\\\\"emf peak\\\\\".*the user closed it" "$FAKE_LOG" || fail "the follow-up wasn't told the board was closed: $(head -c 1500 "$FAKE_LOG")"
grep -q "NOT in front of the user" "$FAKE_LOG" || fail "no off-screen section in the prompt"
jq -e --arg t "$tag" '.restored == [$t]' "$CHALK_STATE/out.json" >/dev/null || fail "not restored: $(cat "$CHALK_STATE/out.json")"
op '{"op": "panel_info"}' | jq -e --arg t "$tag" '.panels[] | select(.tag == $t) | .objects == 1' >/dev/null || fail "the board isn't back: $(op '{"op": "panel_info"}')"
echo "   ok ($tag closed, then put back)"

echo "4. route teach: Super+/ goes to the tutor (CHALK_TEACH) instead"
printf '#!/bin/sh\necho "$@" >> %s/teach.log\n' "$CHALK_STATE" > "$CHALK_STATE/teach"; chmod +x "$CHALK_STATE/teach"
export CHALK_ROUTE="$CHALK_STATE/route.json"
./chalk route teach demo "Demo lesson" | jq -e '.to == "teach" and .slug == "demo"' >/dev/null || fail "route teach"
CHALK_TEACH="$CHALK_STATE/teach" ./chalk ask "what does this do?"
grep -q "tell demo what does this do?" "$CHALK_STATE/teach.log" || fail "the tutor didn't get the question"
./chalk route off >/dev/null
[[ $(./chalk route) == null ]] || fail "route off"
echo "   ok"

./chalk clear
echo "all ok"
