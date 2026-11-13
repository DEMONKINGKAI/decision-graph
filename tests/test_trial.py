"""Run: python tests/test_trial.py  (dg report, the dg launcher, and the live view's endpoints; no network, no model)"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "notes_app.jsonl"
sys.path.insert(0, str(ROOT / "scripts"))


def make_project(d: Path) -> Path:
    p = d / "proj"
    (p / ".claude" / "decisions").mkdir(parents=True)
    shutil.copy(FIXTURE, p / ".claude" / "decisions" / "decisions.jsonl")
    return p


def report(p: Path, *args) -> str:
    import dg_report
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert dg_report.main(["--project", str(p), *args]) == 0
    return buf.getvalue()


def test_report(d: Path):
    from dg_correct import apply_correction
    from dg_store import Store
    empty = d / "empty"
    empty.mkdir()
    assert "Nothing yet" in report(empty)

    p = make_project(d)
    s = Store(p)
    s.event("extracted", session="s1", trigger="manual", turns=40, added=5)
    s.event("guard", where="tool", tool="Bash", term="pnpm", decision="D-2", paused=True)
    s.event("guard", where="tool", tool="Bash", term="pnpm", decision="D-2", paused=False)
    s.event("guard", where="prompt", term="jest", decision="D-9", paused=False)
    s.event("view")
    ok, msg, _ = apply_correction(p, "D-3", "reject", None, "not a decision")
    assert ok, msg
    ok, msg, _ = apply_correction(p, "D-4", "confirm", None, None)
    assert ok, msg
    out = report(p)
    assert "1 compaction(s) processed" in out, out
    assert "1 time(s) a tool call was paused" in out and "1 reminder(s)" in out and "1 note(s)" in out, out
    assert "D-2 (pnpm) x2" in out, out
    assert "1 rejected as not a decision" in out and "1 confirmed" in out, out
    assert "opened 1 time(s)" in out, out

    # old events fall outside the window
    ev = s.events_path
    lines = [json.loads(l) for l in ev.read_text(encoding="utf-8").splitlines()]
    for e in lines:
        e["at"] = "2000-01-01T00:00:00+00:00"
    ev.write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
    out = report(p)
    assert "0 compaction(s)" in out and "None fired" in out and "wasn't opened" in out, out


def test_launcher(d: Path):
    if os.name == "nt":
        return  # the .cmd launcher is exercised by hand on Windows
    home = d / "home"
    cfg = home / ".claude"
    env = dict(os.environ, HOME=str(home), CLAUDE_CONFIG_DIR=str(cfg))
    bindir = home / ".local" / "bin"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "install_cli.py"), "--dir", str(bindir)],
                   env=env, check=True, capture_output=True)
    dg = bindir / "dg"
    assert dg.exists()
    proj = make_project(d / "l")
    r = subprocess.run([str(dg), "--project", str(proj), "stale"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    # a newer copy in the plugin cache wins over the folder the launcher was installed from
    newer = cfg / "plugins" / "cache" / "neuro-graph" / "decision-graph" / "9.9.9"
    shutil.copytree(ROOT / "scripts", newer / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    (newer / ".claude-plugin").mkdir(parents=True)
    (newer / ".claude-plugin" / "plugin.json").write_text('{"name": "decision-graph", "version": "9.9.9"}')
    (newer / "scripts" / "dg.py").write_text("print('from 9.9.9')\n")
    r = subprocess.run([str(dg), "help"], env=env, capture_output=True, text=True)
    assert r.stdout.strip() == "from 9.9.9", (r.stdout, r.stderr)

    subprocess.run([sys.executable, str(ROOT / "scripts" / "install_cli.py"), "--dir", str(bindir), "--uninstall"],
                   env=env, check=True, capture_output=True)
    assert not dg.exists()


_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url):
    with _direct.open(url, timeout=5) as r:
        return r.read().decode("utf-8")


def test_server(d: Path):
    import dg_view
    from dg_store import Store
    p = make_project(d / "s")
    srv = dg_view._Server(p)
    t = threading.Thread(target=srv.httpd.serve_forever, daemon=True)
    t.start()
    try:
        base, tok = srv.url.split("/?t=")
        get(srv.url)  # first visit: nothing is "new" yet
        html = get(srv.url)  # second visit: anything recorded after the first one is
        assert "D-1" in html and '"live": true' in html, "page should carry data and live mode"
        v1 = json.loads(get(f"{base}/api/version?t={tok}"))["v"]
        try:
            get(f"{base}/api/version?t=wrong")
            raise AssertionError("bad token accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # a decision recorded while the page is open changes the version and arrives marked new
        rec = {"id": "D-18", "decision": "Use Vitest browser mode for component tests", "rationale": [],
               "decided_by": "user", "confidence": "high", "supersedes": [], "depends_on": [],  # list on purpose: the viewer must tolerate it
               "alternatives_rejected": [], "source": {"session_id": "s9", "turn_uuids": [], "decided_at": "2099-01-01T00:00:00+00:00"},
               "extracted_at": "2099-01-01T00:00:00+00:00"}
        with Store(p) as s:
            s.append([rec])
        v2 = json.loads(get(f"{base}/api/version?t={tok}"))["v"]
        assert v2 != v1
        data = json.loads(get(f"{base}/api/data?t={tok}"))
        node = next(n for n in data["nodes"] if n["id"] == "D-18")
        assert node.get("new"), node
        assert any(e["kind"] == "view" for e in Store(p).events())
    finally:
        srv.httpd.shutdown()


def test_live_background(d: Path):
    """What the graph skill does: start the live page with no terminal, then ask again and get the same one."""
    p = make_project(d / "lb")
    env = dict(os.environ, NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    cmd = [sys.executable, str(ROOT / "scripts" / "dg.py"), "--project", str(p), "view", "--live", "--no-open"]
    srv = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    try:
        first = srv.stdout.readline()
        assert first.startswith("Decision graph (live): http://127.0.0.1:"), first
        url = first.split(": ", 1)[1].strip()
        assert '"live": true' in get(url)
        again = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=20)
        assert again.returncode == 0 and url in again.stdout and "already running" in again.stdout, again.stdout
    finally:
        srv.terminate()
        srv.wait(timeout=10)


def test_demo_and_empty(d: Path):
    import dg_view
    empty = d / "e"
    (empty / ".claude").mkdir(parents=True)
    out = empty / "g.html"
    buf = io.StringIO()
    with redirect_stdout(buf):
        dg_view.main(["--project", str(empty), "--static", "--no-open", "--out", str(out)])
    assert out.exists() and 'id="empty"' in out.read_text(encoding="utf-8")


def main():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        for name, fn in [("report", test_report), ("launcher", test_launcher), ("server", test_server), ("live in background", test_live_background),
                         ("empty page", test_demo_and_empty)]:
            sub = d / name.replace(" ", "_")
            sub.mkdir()
            fn(sub)
            print(f"ok  {name}")
    print("test_trial: all passed")


if __name__ == "__main__":
    main()
