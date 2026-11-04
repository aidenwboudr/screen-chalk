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

echo "3. route teach: Super+/ goes to the tutor (CHALK_TEACH) instead"
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
