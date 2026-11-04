#!/usr/bin/env bash
# Install chalk for this user (no sudo; README.md lists the packages it needs first).
#
#   ./install.sh           `chalk` and `chalk-mcp` on PATH (~/.local/bin), the MCP server registered with Claude Code,
#                          MathJax for scene formulas (tex/, if npm is there)
#   ./install.sh --sway    also, for the machine whose screen chalk draws on: chalk-cap built (cap/: marks follow
#                          what they mark as it scrolls), the hotkeys in ~/.config/sway/config.d/chalk.conf, Sway
#                          reloaded and the overlay (re)started:
#       Super+/        ask about the screen (`chalk ask`: needs ANTHROPIC_API_KEY, see README.md)
#       Super+Shift+/  wipe the drawing (and stop any voice)
#       Super+.        cards/diagrams that moved out of the way: back, or all tucked away
#       Super+;        answer the newest panel with the keyboard (again, or Esc: back to your window)
#       Super+Ctrl+C   copy the newest panel's code (press again for the next block)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
sway=0
for a in "$@"; do
  case $a in
    --sway) sway=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option $a" >&2; exit 2 ;;
  esac
done

mkdir -p ~/.local/bin
ln -sfn "$HERE/chalk" ~/.local/bin/chalk
ln -sfn "$HERE/chalk-mcp" ~/.local/bin/chalk-mcp
echo "chalk -> $HERE/chalk"
export PATH="$HOME/.local/bin:$PATH"  # claude installs there too, and ssh sessions don't have it on PATH

# scene boards typeset their formulas with MathJax (tex/texd.js); without it they fall back to plain text
if command -v npm >/dev/null; then
  if [[ ! -d "$HERE/tex/node_modules" ]] || [[ "$HERE/tex/package-lock.json" -nt "$HERE/tex/node_modules" ]]; then
    (cd "$HERE/tex" && npm ci --silent --no-audit --no-fund) && touch "$HERE/tex/node_modules" && echo "tex: MathJax installed" \
      || echo "tex: npm ci failed: scene formulas fall back to plain text" >&2
  fi
else
  echo "tex: no npm: scene formulas fall back to plain text" >&2
fi

if command -v claude >/dev/null; then
  claude mcp remove -s user chalk >/dev/null 2>&1 || true
  claude mcp add -s user chalk -- "$HOME/.local/bin/chalk-mcp" >/dev/null
  echo "registered MCP server 'chalk' with Claude Code for $(whoami)"
else
  echo "no claude CLI: register the MCP server later with: claude mcp add -s user chalk -- ~/.local/bin/chalk-mcp" >&2
fi

((sway)) || exit 0

# chalk-cap: the overlay's screen grabber, which keeps marks on what they mark as it scrolls (follow.py)
if "$HERE/cap/build.sh" 2>/dev/null; then
  echo "chalk-cap built"
else
  echo "chalk-cap didn't build (needs gcc, wayland-scanner, libwayland-dev): marks won't follow scrolling" >&2
fi

mkdir -p ~/.config/sway/config.d
conf=~/.config/sway/config.d/chalk.conf
# Mod4, not $mod: config.d files load alphabetically, so chalk.conf can come before the file that sets $mod
cat > "$conf" <<EOF
# chalk ($HERE): Claude draws on the screen to explain it
#   Super+/        ask about what's on screen
#   Super+Shift+/  wipe the drawing and stop the voice
#   Super+.        bring back cards/diagrams that got out of your way (or tuck them all away)
#   Super+;        answer the newest panel with the keyboard (again, or Esc: back to your window)
#   Super+Ctrl+C   copy the newest panel's code (press again for the next block)
bindsym Mod4+slash exec ~/.local/bin/chalk ask
bindsym Mod4+Shift+slash exec ~/.local/bin/chalk clear --hush
bindsym Mod4+period exec ~/.local/bin/chalk peek
bindsym Mod4+semicolon exec ~/.local/bin/chalk focus
bindsym Mod4+Ctrl+c exec ~/.local/bin/chalk copy
exec ~/.local/bin/chalk start
EOF
main=${SWAY_CONFIG:-~/.config/sway/config}
if [[ ! -f $main ]]; then
  echo "sway: no $main yet (Sway is using /etc/sway/config): copy that there and add  include ~/.config/sway/config.d/*" >&2
elif ! grep -qE '^\s*include\s+.*(~|\$HOME|/home/[^/]+)/\.config/sway/config\.d' "$main"; then
  echo "sway: add this line to $main so it loads chalk's hotkeys:  include ~/.config/sway/config.d/*" >&2
fi
# check before reloading: a bad line would put an error bar on the live session. Only errors in chalk.conf count
# (other configs can have lines a plain `sway -C` doesn't know, e.g. SwayFX's).
if command -v sway >/dev/null && [[ -f $main ]]; then
  errs=$(WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman \
         sway -C -c "$main" 2>&1 | grep "Error on line" | grep "chalk.conf" || true)
  if [[ -n $errs ]]; then
    rm -f "$conf"
    echo "sway: chalk.conf didn't validate, left it out:" >&2
    echo "$errs" >&2
    exit 1
  fi
fi
export SWAYSOCK="${SWAYSOCK:-$(ls -t "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"/sway-ipc.*.sock 2>/dev/null | head -1)}"
if [[ -n $SWAYSOCK ]] && swaymsg reload >/dev/null 2>&1; then
  echo "sway: Super+/ asks, Super+Shift+/ clears, Super+. peeks, Super+; answers, Super+Ctrl+C copies"
  ~/.local/bin/chalk stop >/dev/null 2>&1 || true   # a running overlay keeps its old code until restarted
  sleep 0.4
  ~/.local/bin/chalk start || true
else
  echo "sway: the hotkeys apply at the next Sway start or reload"
fi
