#!/usr/bin/env bash
# The checks that need no screen (CI runs these; so does fork-sync before any PR):
#   syntax of every Python/shell/JS file, then the offline tests in bench/.
#   ./test.sh            all of it (test_scene/test_scenehost need gi with cairo + PangoCairo, and tex/ installed)
#   ./test.sh --quick    syntax + the tests that need only python3
set -euo pipefail
cd "$(dirname "$0")"
export CHALK_CONFIG=/dev/null   # pure defaults: never this machine's ~/.config/chalk/env
py=${CHALK_PYTHON:-$( [[ -x /usr/bin/python3 ]] && echo /usr/bin/python3 || command -v python3 )}

echo "syntax"
mapfile -t pys < <(git ls-files '*.py' chalk chalk-mcp)
for f in "${pys[@]}"; do [[ -f $f ]] && "$py" -c 'import ast, sys; ast.parse(open(sys.argv[1]).read(), sys.argv[1])' "$f"; done
for f in $(git ls-files '*.sh'); do [[ -f $f ]] && bash -n "$f"; done
if command -v node >/dev/null; then for f in $(git ls-files '*.js'); do [[ -f $f ]] && node --check "$f"; done; fi

echo "offline tests"
"$py" bench/test_config.py
"$py" bench/test_convo.py
[[ ${1:-} == --quick ]] && exit 0
"$py" bench/test_tex.py
"$py" bench/test_scene.py
"$py" bench/test_scenehost.py
echo "all ok"
