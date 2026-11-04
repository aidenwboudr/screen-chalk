"""Offline checks of chalk's settings (chalkconf.py) and the CLI without a screen: the config file fills only what
the environment doesn't set, the state dir is private, CHALK_HOOKS loads, and with no Sway session and no
CHALK_HOST the CLI says so instead of reaching for another machine."""
import os, stat, subprocess, sys, tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)


def fresh_conf():
    """chalkconf with its once-per-process state reset."""
    import importlib
    import chalkconf
    return importlib.reload(chalkconf)


def main():
    tmp = tempfile.mkdtemp()
    conf = os.path.join(tmp, "env")
    open(conf, "w").write("# a comment\nCHALK_TEST_A=from-file\nexport CHALK_TEST_B='quoted value'\n"
                          "CHALK_TEST_C=~/x\nCHALK_MODEL=sonnet\nnot a setting\n")
    os.environ.update(CHALK_CONFIG=conf, CHALK_MODEL="opus")
    c = fresh_conf()
    c.load()
    assert os.environ["CHALK_TEST_A"] == "from-file"
    assert os.environ["CHALK_TEST_B"] == "quoted value"
    assert os.environ["CHALK_TEST_C"] == os.path.expanduser("~/x")
    assert os.environ["CHALK_MODEL"] == "opus", "the environment wins over the file"

    os.environ["CHALK_TEST_CMD"] = "~/bin/py '/a b/voice.py' --fast"
    assert c.words("CHALK_TEST_CMD") == [os.path.expanduser("~/bin/py"), "/a b/voice.py", "--fast"]
    assert c.words("CHALK_TEST_UNSET") == []

    os.environ.pop("CHALK_STATE", None)
    os.environ["XDG_RUNTIME_DIR"] = tmp
    d = c.state_dir()
    assert d == os.path.join(tmp, "chalk") and stat.S_IMODE(os.stat(d).st_mode) == 0o700, oct(os.stat(d).st_mode)

    hook = os.path.join(tmp, "hooks.py")
    open(hook, "w").write("def context(q):\n    return 'ctx:' + q\n")
    os.environ["CHALK_HOOKS"] = hook
    c = fresh_conf()
    assert c.hook("context")("hi") == "ctx:hi" and c.hook("route") is None
    os.environ.pop("CHALK_HOOKS")
    assert fresh_conf().hooks() is None

    # no Sway, no CHALK_HOST: a clear error, no ssh
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWAYSOCK", "WAYLAND", "CHALK_"))}
    env.update(CHALK_CONFIG=os.devnull, XDG_RUNTIME_DIR=tmp)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "chalk"), "look"], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "no Sway session found" in r.stderr and "CHALK_HOST" in r.stderr, (r.returncode, r.stderr)
    print("config ok")


if __name__ == "__main__":
    main()
