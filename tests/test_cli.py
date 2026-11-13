"""Run: python tests/test_cli.py  (dg command, view, doctor, installer, session-start notice; no network)"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DECISION_GRAPH_NO_AUTOSETUP", "1")   # never set up the real computer from a test

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "notes_app.jsonl"
sys.path.insert(0, str(SCRIPTS))


def dg(*args, cwd, env_extra=None, stdin=b""):
    env = dict(os.environ)
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, str(SCRIPTS / "dg.py"), *args], cwd=cwd, capture_output=True,
                       env=env, input=stdin)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def make_project(root: Path, name="proj") -> Path:
    proj = root / name
    (proj / ".claude" / "decisions").mkdir(parents=True)
    shutil.copy(FIXTURE, proj / ".claude" / "decisions" / "decisions.jsonl")
    return proj


def main():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        proj = make_project(root)

        # ---- dg dispatch
        rc, out, _ = dg(cwd=proj)
        assert rc == 0 and "12 active decisions (17 recorded" in out, out
        rc, out, _ = dg("stale", cwd=proj)
        assert "D-7" in out.split("Resolved")[0] and "restored by D-17" in out, out
        rc, out, _ = dg("show", "d-3", cwd=proj)
        assert "because: Type safety.  (added later, A-1)" in out, out
        rc, out, _ = dg("--project", str(proj), "why", "D-3", cwd=root)
        assert rc == 0 and "RESTORED by D-17" in out, out
        rc, out, _ = dg("frobnicate", cwd=proj)
        assert rc == 2 and "Unknown command" in out and "dg doctor" in out
        rc, out, _ = dg("--help", cwd=proj)
        assert rc == 0 and "dg view" in out

        # ---- view: data model and page
        import dg_view
        from dg_store import Store
        data = dg_view.build_data(Store(proj).records(), proj)
        st = {n["id"]: n["status"] for n in data["nodes"]}
        assert st["D-7"] == "stale" and st["D-2"] == "resolved" and st["D-3"] == "ok" and st["D-1"] == "replaced", st
        assert "A-1" not in st
        d3 = next(n for n in data["nodes"] if n["id"] == "D-3")
        assert {"text": "Type safety.", "from": "A-1"} in d3["reasons"]
        kinds = {(e["from"], e["to"], e["kind"]) for e in data["edges"]}
        assert ("D-1", "D-2", "depends") in kinds and ("D-17", "D-1", "restores") in kinds and ("D-6", "D-11", "supersedes") in kinds

        rc, out, _ = dg("view", "--no-open", cwd=proj)
        page = proj / ".claude" / "decisions" / "graph.html"
        assert rc == 0 and page.exists() and "12 active, 5 replaced" in out, out
        html = page.read_text(encoding="utf-8")
        assert "/*__DATA__*/null" not in html and '"D-17"' in html and "__TITLE__" not in html
        # a decision text containing </script> must not break the page
        nasty = make_project(root, "nasty")
        with open(nasty / ".claude/decisions/decisions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "D-18", "decision": "Use </script><b>x</b> safely", "depends_on": []}) + "\n")
        dg("view", "--no-open", cwd=nasty)
        nh = (nasty / ".claude/decisions/graph.html").read_text(encoding="utf-8")
        assert nh.count("</script>") == 1, "embedded data must not close the script tag"
        node = shutil.which("node")
        if node:
            js = html.split("<script>")[1].split("</script>")[0]
            (root / "v.js").write_text(js, encoding="utf-8")
            p = subprocess.run([node, "--check", str(root / "v.js")], capture_output=True)
            assert p.returncode == 0, p.stderr.decode()

        empty = root / "empty"
        empty.mkdir()
        rc, out, _ = dg("view", "--no-open", cwd=empty)
        assert rc == 0 and "No decisions recorded yet" in out and "dg demo" in out, out

        # ---- doctor (offline): healthy fixture
        env = {"DECISION_GRAPH_CLAUDE_BIN": sys.executable}  # any executable; offline skips the model call
        rc, out, _ = dg("doctor", "--offline", cwd=proj, env_extra=env)
        assert "17 decisions (12 in force), 1 amendment" in out and "no circular links" in out, out
        assert "rest on replaced reasons: D-7" in out, out

        # ---- doctor: damaged graph
        bad = make_project(root, "bad")
        with open(bad / ".claude/decisions/decisions.jsonl", "a", encoding="utf-8") as f:
            f.write('{"id": "D-18", "decision": "half-writ\n')
            f.write(json.dumps({"id": "D-19", "decision": "A rests on B", "depends_on": ["D-20"]}) + "\n")
            f.write(json.dumps({"id": "D-20", "decision": "B rests on A", "depends_on": ["D-19", "D-99"]}) + "\n")
            f.write(json.dumps({"id": "D-21", "decision": "Use Zod to validate all API inputs and form data!", "depends_on": []}) + "\n")
            f.write(json.dumps({"id": "D-22", "decision": "Also replaces pnpm", "supersedes": "D-5"}) + "\n")
        (bad / ".claude/decisions/extract.log").write_text(
            "2026-11-11T13:00:00+00:00 session s (auto): 3 turns from line 0, +2 decisions\n"
            "2026-11-11T14:00:00+00:00 ERROR RuntimeError: claude -p exited 1\n--- stderr ---\n\n--- stdout ---\nCredit balance is too low\n",
            encoding="utf-8")
        rc, out, _ = dg("doctor", "--offline", cwd=bad, env_extra=env)
        assert rc == 1, out
        assert "unreadable line" in out and "D-20 rests on missing D-99" in out, out
        assert "circular links" in out and ("D-19 -> D-20" in out or "D-20 -> D-19" in out), out
        assert "possible duplicate" in out and "D-14 ~ D-21" in out, out
        assert "replaced more than once" in out and "D-5 by D-10, D-22" in out, out
        assert "[FAIL] the most recent extraction failed" in out, out

        # ---- session-start notice for a failed extraction
        p = subprocess.run([sys.executable, str(SCRIPTS / "inject_decisions.py")], input=b"{}",
                           capture_output=True, env=dict(os.environ, CLAUDE_PROJECT_DIR=str(bad)))
        o = p.stdout.decode()
        assert "last automatic decision extraction failed" in o and "dg doctor" in o, o
        with open(bad / ".claude/decisions/extract.log", "a", encoding="utf-8") as f:
            f.write("2026-11-11T15:00:00+00:00 session s (auto): 2 turns from line 3, +1 decisions\n")
        p = subprocess.run([sys.executable, str(SCRIPTS / "inject_decisions.py")], input=b"{}",
                           capture_output=True, env=dict(os.environ, CLAUDE_PROJECT_DIR=str(bad)))
        assert "extraction failed" not in p.stdout.decode()
        # notice even when there are no decisions yet
        fresh = root / "fresh"
        (fresh / ".claude/decisions").mkdir(parents=True)
        (fresh / ".claude/decisions/extract.log").write_text(
            "2026-11-11T14:00:00+00:00 ERROR RuntimeError: claude -p exited 1\n", encoding="utf-8")
        p = subprocess.run([sys.executable, str(SCRIPTS / "inject_decisions.py")], input=b"{}",
                           capture_output=True, env=dict(os.environ, CLAUDE_PROJECT_DIR=str(fresh)))
        assert "extraction failed" in p.stdout.decode()

        # ---- installer
        bindir = root / "bin"
        p = subprocess.run([sys.executable, str(SCRIPTS / "install_cli.py"), "--dir", str(bindir)],
                           capture_output=True, env=dict(os.environ, PATH=os.environ.get("PATH", "")))
        o = p.stdout.decode()
        launcher = bindir / ("dg.cmd" if os.name == "nt" else "dg")
        assert p.returncode == 0 and launcher.exists() and "not on your PATH" in o, o
        if os.name != "nt":
            q = subprocess.run([str(launcher), "--project", str(proj), "active"], capture_output=True)
            assert q.returncode == 0 and b"12 active decisions" in q.stdout
        p = subprocess.run([sys.executable, str(SCRIPTS / "install_cli.py"), "--dir", str(bindir), "--uninstall"],
                           capture_output=True)
        assert not launcher.exists()

        print("all tests passed")


if __name__ == "__main__":
    main()
