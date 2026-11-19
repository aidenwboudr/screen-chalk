#!/usr/bin/env bash
# Nothing chalk shows is bigger than the screen. In the private headless Sway (1920x1280, never your real session):
#   1. an 83-line code card (bench/code-wall.json: a whole main.c, the card that once ran off both ends of a laptop
#      screen) sits inside the screen, top to bottom, with its header and a scrollbar showing
#   2. the mouse wheel over it scrolls the body; the header stays put and the Copy button still copies all of it
#   3. with the keyboard (chalk focus): End goes to the bottom, Home back to the top
#   4. a card asked to be wider than the screen is cut to the screen's width
#   5. a short card doesn't scroll at all
# Run from a checkout on a machine with sway, grim, jq, wtype and wl-clipboard:  bench/test_fit.sh
set -euo pipefail
cd "$(dirname "$0")/.."
eval "$(bench/headless-sway.sh start)"
export CHALK_STATE=/tmp/chalk-test-fit CHALK_NO_FORWARD=1 PATH="$HOME/.local/bin:$PATH"
mkdir -p "$CHALK_STATE"
fail() { echo "FAIL: $*"; ./chalk status; exit 1; }
shot() { grim -s 0.5 "/tmp/fit-$1.png"; }
info() { python3 -c "import json,socket,sys; s=socket.socket(socket.AF_UNIX); s.connect('$CHALK_SOCK'); s.sendall(b'{\"op\":\"panel_info\"}\n'); print(s.makefile().readline())"; }
field() { info | python3 -c "import json,sys; p=json.load(sys.stdin)['panels'][0]; print(p[sys.argv[1]])" "$1"; }
inside() {  # inside: the newest panel's rect lies on the 1920x1280 screen
  info | python3 -c "
import json,sys; p=json.load(sys.stdin)['panels'][0]; x,y,w,h=p['rect']
assert x >= 0 and y >= 0 and x + w <= 1920 and y + h <= 1280, ('off screen', p['rect'])
print('   on screen at', p['rect'], 'scroll', p['scroll'], 'of', p['scrollable'])"
}
bench/vpointer/build.sh
rm -f /tmp/fit-ptr; mkfifo /tmp/fit-ptr
bench/vpointer/vpointer 1920 1280 < /tmp/fit-ptr > /dev/null & ptr=$!
exec 7> /tmp/fit-ptr
trap 'exec 7>&-; kill $ptr 2>/dev/null; rm -f /tmp/fit-ptr' EXIT

./chalk stop >/dev/null 2>&1 || true; sleep 0.3
swaymsg -q '[title=".*"] kill' || true; sleep 0.3

echo "1. a whole file in one card fits the screen"
./chalk draw "$(cat bench/code-wall.json)" >/dev/null
sleep 1.5; shot 1
inside || fail "the code card runs off the screen"
[[ $(field scrollable) -gt 200 ]] || fail "an 83-line card should scroll (scrollable $(field scrollable))"
read -r X Y < <(info | python3 -c "import json,sys; x,y,w,h=json.load(sys.stdin)['panels'][0]['rect']; print(int(x+w/2), int(y+h/2))")

echo "2. the wheel scrolls it, the header stays, Copy still copies the whole file"
echo "move $X $Y" >&7; sleep 0.3
echo "wheel 6" >&7; sleep 0.8; shot 2
s1=$(field scroll)
[[ $s1 -gt 100 ]] || fail "the wheel didn't scroll it (scroll $s1)"
inside
info | python3 -c "
import json,sys; p=json.load(sys.stdin)['panels'][0]; x,y,w,h=p['rect']
acts=[a for _,a in p['hits']]
assert 'close' in acts and 'dock' in acts, ('header dots gone while scrolled', acts)
for (hx,hy,hw,hh),a in p['hits']:
    assert hy >= y and hy + hh <= y + h + 1, ('a target outside the visible window', a, hy, hh, p['rect'])"
echo "wheel -20" >&7; sleep 1.2
[[ $(field scroll) -eq 0 ]] || fail "scrolling back up didn't reach the top ($(field scroll))"
read -r CX CY < <(info | python3 -c "
import json,sys; p=json.load(sys.stdin)['panels'][0]
b=[b for b,a in p['hits'] if a==['copy',0]][0]; print(int(b[0]+b[2]/2), int(b[1]+b[3]/2))")
if command -v wl-paste >/dev/null; then
  echo "move $CX $CY" >&7; sleep 0.2; echo "click" >&7; sleep 0.8
  wl-paste -n | grep -q "dmic_trigger(dmic, DMIC_TRIGGER_STOP)" || fail "Copy didn't copy the whole file: $(wl-paste -n | tail -2)"
  echo "   ok: scrolled $s1 px, copied $(wl-paste -n | wc -l) lines"
else
  echo "   ok: scrolled $s1 px (no wl-clipboard here: Copy not checked)"
fi

echo "3. keys: End to the bottom, Home to the top"
./chalk focus >/dev/null; sleep 1   # (the compositor hands the panel the keyboard a moment after the overlay asks)
for _ in 1 2 3; do   # (a headless Sway drops the first key; a slower one can drop the first press too)
  wtype -k Shift_L -k End; sleep 0.5
  [[ $(field scroll) -eq $(field scrollable) ]] && break
done
shot 3
[[ $(field scroll) -eq $(field scrollable) ]] || fail "End didn't reach the bottom ($(field scroll) of $(field scrollable))"
wtype -k Shift_L -k Home; sleep 0.5
[[ $(field scroll) -eq 0 ]] || fail "Home didn't go back to the top"
wtype -k Shift_L -k Escape; sleep 0.3
echo "   ok"

echo "4. a card asked wider than the screen gets the screen's width"
./chalk draw '[{"kind":"card","title":"wide","width":4000,"card_width":4000,"text":"```\n'"$(printf 'x%.0s' $(seq 300))"'\n```"}]' >/dev/null
sleep 1.2; shot 4
inside || fail "the wide card runs off the screen"
echo "   ok"

echo "5. a short card doesn't scroll"
./chalk draw '[{"kind":"card","title":"short","text":"```c\nint x = 1;\n```\nOne line."}]' >/dev/null
sleep 1.2
inside
[[ $(field scrollable) -eq 0 ]] || fail "a short card scrolls"
echo "   ok"
./chalk clear
echo "all ok (shots: /tmp/fit-*.png)"
