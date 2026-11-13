"""Run: python tests/test_install.py  (what a new user hits: the hook launcher, first-run setup, finding projects)"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
LAUNCHER = SCRIPTS / "run-hook.sh"
FIXTURE = ROOT / "tests" / "fixtures" / "notes_app.jsonl"


def clean_env(home: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "DECISION_GRAPH", "CCR_")) and k not in ("CI",)}
    env.update(HOME=str(home), USERPROFILE=str(home), **extra)
    return env


def test_launcher(tmp: Path):
    home = tmp / "h1"
    home.mkdir()
    probe = tmp / "probe.py"
    probe.write_text("import sys; print('args', sys.argv[1:], sys.stdin.read().strip()); sys.exit(2)\n")
    r = subprocess.run(["sh", str(LAUNCHER), str(probe), "tool"], input='{"x": 1}', capture_output=True,
                       text=True, env=clean_env(home))
    assert r.returncode == 2 and "args ['tool'] {\"x\": 1}" in r.stdout, r     # exit code and stdin pass through
    assert (home / ".decision-graph" / "python").exists()                         # the choice is cached

    # no Python anywhere on PATH: one notice at session start, silence for every other hook
    nopy = tmp / "nopy"
    nopy.mkdir()
    for c in ("sh", "cat", "date", "mkdir", "printf", "uname"):
        src = shutil.which(c)
        if src:
            (nopy / c).symlink_to(src)
    home2 = tmp / "h2"
    home2.mkdir()
    env = clean_env(home2, PATH=str(nopy))
    r = subprocess.run([str(nopy / "sh"), str(LAUNCHER), "/x/inject_decisions.py"], input="{}",
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "needs Python 3.9" in json.loads(r.stdout)["systemMessage"], r
    r = subprocess.run([str(nopy / "sh"), str(LAUNCHER), "/x/inject_decisions.py"], input="{}",
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout == "", "the notice is shown once a day, not every session"
    r = subprocess.run([str(nopy / "sh"), str(LAUNCHER), "/x/guard.py", "tool"], input="{}",
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == "", "other hooks stay silent without Python"

    # hooks.json uses the launcher for every hook
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    for event, entries in hooks.items():
        for e in entries:
            for h in e["hooks"]:
                assert h["command"].startswith('sh "${CLAUDE_PLUGIN_ROOT}/scripts/run-hook.sh"'), (event, h)
                assert "args" not in h
    print("ok  hook launcher: finds Python, passes stdin and exit codes, explains a missing Python once")


def test_first_run(tmp: Path):
    home = tmp / "h3"
    home.mkdir()
    proj = tmp / "proj"
    proj.mkdir()
    fake_claude = tmp / "bin" / "claude"
    fake_claude.parent.mkdir()
    fake_claude.write_text("#!/bin/sh\necho 1.0\n")
    fake_claude.chmod(0o755)
    env = clean_env(home, DECISION_GRAPH_NO_SERVICE="1", CLAUDE_CODE_EXECPATH=str(fake_claude),
                    PATH="/usr/bin:/bin")
    hook = lambda e: subprocess.run([sys.executable, str(SCRIPTS / "inject_decisions.py")],
                                    input=json.dumps({"cwd": str(proj), "source": "startup"}),
                                    capture_output=True, text=True, env=e, cwd=proj)
    r = hook(env)
    out = json.loads(r.stdout)
    assert "set up on this computer" in out["systemMessage"], out
    assert "`dg` command" in out["systemMessage"]
    assert (home / ".local" / "bin" / "dg").exists(), "the dg command is installed"
    assert (home / ".decision-graph" / "setup.json").exists()
    assert (home / ".decision-graph" / "claude_path").read_text() == str(fake_claude), "remembers the app's claude"

    # `dg` in a terminal (no CLAUDE_CODE_EXECPATH, not on PATH) still finds it
    r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                        "from dg_store import find_claude; print(find_claude())", str(SCRIPTS)],
                       capture_output=True, text=True, env=clean_env(home, PATH="/usr/bin:/bin"))
    assert r.stdout.strip() == str(fake_claude), r

    r = hook(env)
    assert "set up on this computer" not in r.stdout, "only the first session sets up"

    # never on a cloud or Cowork sandbox
    home4 = tmp / "h4"
    home4.mkdir()
    for extra in ({"CLAUDE_CODE_REMOTE": "true"}, {"CLAUDE_CODE_ENTRYPOINT": "remote_cowork"}):
        r = hook(clean_env(home4, DECISION_GRAPH_NO_SERVICE="1", **extra))
        assert "set up on this computer" not in r.stdout
        assert not (home4 / ".decision-graph" / "setup.json").exists()
    print("ok  first run: sets up dg once, remembers claude, never on a sandbox")


def test_discover(tmp: Path):
    sys.path.insert(0, str(SCRIPTS))
    import dg_service
    root = tmp / "home5"
    want = [root / "OneDrive" / "Desktop" / "Overseer", root / "code" / "a" / "b" / "deep"]
    skip = [root / "node_modules" / "pkg", root / ".cache" / "x", root / "AppData" / "y",
            root / "1" / "2" / "3" / "4" / "5" / "too-deep"]
    for p in want + skip:
        (p / ".claude" / "decisions").mkdir(parents=True)
        shutil.copy(FIXTURE, p / ".claude" / "decisions" / "decisions.jsonl")
    (root / "empty" / ".claude" / "decisions").mkdir(parents=True)        # no decisions file: not a project
    found = sorted(dg_service.discover([root]))
    assert found == sorted(want), found
    print("ok  service finds projects under home on its own, skipping hidden, heavy and very deep folders")


def main():
    if os.name == "nt":
        print("skipped: these tests use POSIX tools")
        return
    tmp = Path(tempfile.mkdtemp())
    try:
        test_launcher(tmp)
        test_first_run(tmp)
        test_discover(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("install tests passed")


if __name__ == "__main__":
    main()
