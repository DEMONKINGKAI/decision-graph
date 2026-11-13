"""Run: python tests/test_service.py  (the background service: install, serve many projects, restart, remove)"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("DECISION_GRAPH_NO_AUTOSETUP", "1")   # never set up the real computer from a test

ROOT = Path(__file__).resolve().parent.parent
DG = ROOT / "scripts" / "dg.py"
FIXTURE = ROOT / "tests" / "fixtures" / "notes_app.jsonl"
OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url, host=None):
    req = urllib.request.Request(url, headers={"Host": host} if host else {})
    with OPEN.open(req, timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def code(url, host=None):
    try:
        return get(url, host)[0]
    except urllib.error.HTTPError as e:
        return e.code


def wait(cond, secs=12):
    for _ in range(int(secs * 10)):
        if cond():
            return True
        time.sleep(0.1)
    return False


def main():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        port = str(49000 + os.getpid() % 900)
        env = dict(os.environ, DECISION_GRAPH_HOME=str(d / "home"), XDG_CONFIG_HOME=str(d / "cfg"), APPDATA=str(d / "appdata"),
                   HOME=str(d / "userhome"), DECISION_GRAPH_PORT=port, NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        env.pop("DECISION_GRAPH_EXTRACTING", None)
        (d / "userhome").mkdir()

        def dg(*args, timeout=30):
            return subprocess.run([sys.executable, str(DG), *args], capture_output=True, text=True, env=env, timeout=timeout)

        projects = []
        for name in ("alpha", "beta"):
            p = d / name
            (p / ".claude" / "decisions").mkdir(parents=True)
            shutil.copy(FIXTURE, p / ".claude" / "decisions" / "decisions.jsonl")
            projects.append(p)
            assert dg("--project", str(p), "stale").returncode == 0      # any dg command registers the project
        reg = json.loads((d / "home" / "projects.json").read_text())
        assert len(reg["projects"]) == 2

        try:
            r = dg("service", "install")
            assert r.returncode == 0 and "Running on http://127.0.0.1:" in r.stdout, r.stdout + r.stderr
            autostart = next((d / "cfg").rglob("decision-graph-service.*"), None) or next((d / "appdata").rglob("*.vbs"), None) \
                or next((d / "userhome").rglob("*.plist"), None)
            assert autostart and autostart.exists(), "a login entry should exist"

            live = [json.loads((p / ".claude/decisions/live.json").read_text()) for p in projects] if wait(
                lambda: all((p / ".claude/decisions/live.json").exists() for p in projects)) else None
            assert live and all(x["service"] for x in live), live
            assert all("live.json" in (p / ".claude/decisions/.gitignore").read_text() for p in projects)
            a_url, b_url = live[0]["url"], live[1]["url"]
            assert a_url.split("/p/")[0] == b_url.split("/p/")[0], "one process serves both"

            # each page works with its own key, and only with its own
            st, html = get(a_url)
            assert st == 200 and '"live": true' in html
            base_a, tok_a = a_url.split("?t=")
            base_b, tok_b = b_url.split("?t=")
            assert get(base_a + "api/version?t=" + tok_a)[0] == 200
            assert code(base_b + "?t=" + tok_a) == 403, "another project's key must not open this one"
            assert code(base_a.rstrip("/") + "?t=" + tok_a) in (200, 301)       # /p/key without the slash redirects
            assert code(a_url, host="evil.example:" + port) == 403, "only this computer's own address is accepted"

            # dg view reuses the service instead of starting its own server
            r = dg("--project", str(projects[0]), "view", "--live", "--no-open")
            assert "already running" in r.stdout and a_url in r.stdout, r.stdout

            # a new project is picked up within a few seconds
            g = d / "gamma"
            (g / ".claude" / "decisions").mkdir(parents=True)
            shutil.copy(FIXTURE, g / ".claude" / "decisions" / "decisions.jsonl")
            dg("--project", str(g), "stale")
            assert wait(lambda: (g / ".claude/decisions/live.json").exists()), "new project not picked up"
            assert get(json.loads((g / ".claude/decisions/live.json").read_text())["url"])[0] == 200

            # status reports it
            r = dg("service", "status")
            assert "Running: yes" in r.stdout and "Projects (3)" in r.stdout, r.stdout

            # a crashed service comes back at the next session start
            assert "Stopped" in dg("service", "stop").stdout
            assert wait(lambda: not (projects[0] / ".claude/decisions/live.json").exists(), 5), "links should be cleaned up"
            hook = subprocess.run([sys.executable, str(ROOT / "scripts" / "inject_decisions.py")], capture_output=True, text=True,
                                  input=json.dumps({"source": "startup", "cwd": str(projects[1])}),
                                  env=dict(env, CLAUDE_PROJECT_DIR=str(projects[1])))
            assert hook.returncode == 0, hook.stderr
            assert wait(lambda: (projects[0] / ".claude/decisions/live.json").exists()), "session start should restart it"
        finally:
            r = dg("service", "remove")
        assert "Removed the login entry" in r.stdout, r.stdout
        assert not autostart.exists()
        assert wait(lambda: not any((p / ".claude/decisions/live.json").exists() for p in projects), 5)
        assert "Running: no" in dg("service", "status").stdout
    print("all tests passed")


if __name__ == "__main__":
    main()
