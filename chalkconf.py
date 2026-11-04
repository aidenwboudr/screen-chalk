"""chalk's settings, shared by every entry point (chalk, chalk-mcp, ask.py, overlay.py).

Settings are environment variables. `~/.config/chalk/env` (or the file in $CHALK_CONFIG) holds KEY=VALUE lines
that fill in any variable the environment doesn't already set, so a hotkey, a Claude Code MCP server and a
shell all see the same values. README.md lists the variables.
"""
import importlib.util, os, shlex

_loaded = False


def load():
    """Fill os.environ from the config file (never overriding what's already set). Safe to call repeatedly."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = os.path.expanduser(os.environ.get("CHALK_CONFIG") or "~/.config/chalk/env")
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = os.path.expandvars(os.path.expanduser(val))


def runtime_dir():
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def state_dir():
    """Where screenshots, the last look, the hotkey's conversation and ask.log live: private to this user."""
    d = os.environ.get("CHALK_STATE") or os.path.join(runtime_dir(), "chalk")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def words(name, default=""):
    """A command setting (CHALK_VOICE_CMD, CHALK_PLAYER, ...) as an argv list, with ~ expanded."""
    v = os.environ.get(name, default)
    return [os.path.expanduser(w) for w in shlex.split(v)] if v.strip() else []


_hooks = False


def hooks():
    """The module named by CHALK_HOOKS (a .py file), or None. It may define any of:
      intercept(question) -> callable(convo, env, send) | None   take a hotkey question elsewhere instead of answering
      context(question) -> str                                    background added to the hotkey's prompt
      route(route, args, env, send) -> bool                       answer a question while a route other than teach is on
      route_cmd(args) -> dict | None                              `chalk route <name> ...` for names the core doesn't know
    """
    global _hooks
    if _hooks is False:
        _hooks = None
        path = os.environ.get("CHALK_HOOKS")
        if path:
            spec = importlib.util.spec_from_file_location("chalk_hooks", os.path.expanduser(path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _hooks = mod
    return _hooks


def hook(name):
    h = hooks()
    return getattr(h, name, None) if h else None
