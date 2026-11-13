"""Run: python tests/test_corrections.py  (confirm / reject / edit: graph effects, CLI, server, rebuild; no network)"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("DECISION_GRAPH_NO_AUTOSETUP", "1")   # never set up the real computer from a test

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "notes_app.jsonl"
sys.path.insert(0, str(SCRIPTS))


def dg(*args, cwd, env=None):
    e = dict(os.environ)
    e.pop("CLAUDE_PROJECT_DIR", None)
    e.update(env or {})
    p = subprocess.run([sys.executable, str(SCRIPTS / "dg.py"), *args], cwd=cwd, capture_output=True, env=e)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def project(root: Path, name: str) -> Path:
    p = root / name
    (p / ".claude" / "decisions").mkdir(parents=True)
    shutil.copy(FIXTURE, p / ".claude" / "decisions" / "decisions.jsonl")
    return p


def main():
    from dg_store import Graph, Store
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        p = project(root, "a")

        # ---- reject: hidden, links dropped, what it replaced comes back
        rc, out, _ = dg("reject", "d-17", "--note", "was only a test", cwd=p)
        assert rc == 0 and "D-4, which it replaced, is in force again" in out, out
        g = Graph(Store(p).records())
        assert "D-17" not in g.by_id and "D-17" in g.rejected and g.is_active("D-4")
        assert g.stale_deps("D-2") == [("D-1", "D-4")]  # SQLite's reason is gone again
        rc, out, _ = dg("active", cwd=p)
        assert "D-17" not in out and "12 active decisions" in out, out
        rc, out, err = dg("show", "D-17", cwd=p)
        assert rc != 0 and "rejected by you" in err and "dg confirm D-17" in err
        rc, out, _ = dg("reject", "D-17", cwd=p)
        assert rc == 1 and "already rejected" in out
        rc, out, _ = dg("edit", "D-17", "x", cwd=p)
        assert rc == 1 and "confirm it first" in out

        # ---- undo by confirming: back, confirmed
        rc, out, _ = dg("confirm", "D-17", cwd=p)
        assert rc == 0 and "restored and confirmed" in out
        g = Graph(Store(p).records())
        assert g.by_id["D-17"]["confirmed"] and g.by_id["D-17"]["confidence"] == 1.0 and not g.stale_deps("D-2")

        # ---- edit: new wording everywhere, original kept
        rc, out, _ = dg("edit", "D-3", "Use Drizzle ORM for all database access", cwd=p)
        assert rc == 0 and "original kept" in out
        rc, out, _ = dg("show", "D-3", cwd=p)
        assert "D-3: Use Drizzle ORM for all database access" in out and "originally: Use Drizzle ORM as the data layer." in out
        rc, out, _ = dg("search", "database access", cwd=p)
        assert "D-3" in out, out
        rc, out, _ = dg("edit", "D-3", "Use Drizzle ORM for all database access", cwd=p)
        assert rc == 1 and "already the wording" in out
        rc, out, _ = dg("confirm", "D-99", cwd=p)
        assert rc == 1 and "no decision" in out
        rc, out, _ = dg("corrections", cwd=p)
        assert out.count("C-") == 3 and "note: was only a test" in out and "-> Use Drizzle ORM for all database access" in out, out

        # ---- session start shows confirmed + edited text, not rejected
        dg("reject", "D-16", cwd=p)
        q = subprocess.run([sys.executable, str(SCRIPTS / "inject_decisions.py")], input=b"{}", capture_output=True,
                           env=dict(os.environ, CLAUDE_PROJECT_DIR=str(p)))
        o = q.stdout.decode()
        assert "D-17: The notes app must work fully offline; offline support is a requirement again. (confirmed by the user)" in o
        assert "Use Drizzle ORM for all database access" in o and "D-16" not in o, o

        # ---- view data
        import dg_view
        data = dg_view.build_data(Store(p).records(), p)
        st = {n["id"]: n for n in data["nodes"]}
        assert st["D-16"]["status"] == "rejected" and st["D-17"]["confirmed"] and st["D-3"]["edited"]
        assert st["D-3"]["original"] == "Use Drizzle ORM as the data layer."

        # ---- doctor sees them
        rc, out, _ = dg("doctor", "--offline", cwd=p, env={"DECISION_GRAPH_CLAUDE_BIN": sys.executable})
        assert "4 corrections by you (1 rejected)" in out, out

        # ---- extractor: a rejected decision can't be linked to or replaced
        mock = root / "mock.json"
        mock.write_text(json.dumps([{"decision": "Use Vitest's /tests folder layout.", "depends_on": ["D-16"]},
                                    {"decision": "Put tests next to source files.", "supersedes": "D-16"}]))
        tr = root / "t.jsonl"
        tr.write_text(json.dumps({"type": "user", "uuid": "u1", "message": {"role": "user", "content": "x"}}) + "\n")
        e = dict(os.environ, CLAUDE_PROJECT_DIR=str(p), DECISION_GRAPH_BACKEND=f"mock:{mock}")
        e.pop("DECISION_GRAPH_EXTRACTING", None)
        subprocess.run([sys.executable, str(SCRIPTS / "extract_decisions.py")], capture_output=True, env=e,
                       input=json.dumps({"session_id": "x", "transcript_path": str(tr)}).encode())
        recs = Store(p).records()
        new = [r for r in recs if r["id"] in ("D-18", "D-19")]
        assert new[0]["depends_on"] == [] and new[1]["supersedes"] is None, new

        # ---- rebuild carries corrections over by wording
        b = project(root, "b")
        dg("reject", "D-15", cwd=b)
        dg("edit", "D-7", "Host on Cloudflare Pages", cwd=b)
        config = root / "cfg"
        tdir = config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(b.resolve()))
        tdir.mkdir(parents=True)
        (tdir / "s.jsonl").write_text(json.dumps({"type": "user", "uuid": "u1", "timestamp": "2026-11-01T10:00:00Z",
                                                  "message": {"role": "user", "content": "decisions"}}) + "\n")
        reextracted = root / "re.json"   # ids come out in a different order after the rebuild
        reextracted.write_text(json.dumps([{"decision": "The app is hosted on the Cloudflare Pages free tier."},
                                           {"decision": "Use Playwright for end-to-end tests."},
                                           {"decision": "Something completely new."}]))
        e = dict(os.environ, CLAUDE_CONFIG_DIR=str(config), DECISION_GRAPH_BACKEND=f"mock:{reextracted}")
        e.pop("CLAUDE_PROJECT_DIR", None)
        e.pop("DECISION_GRAPH_EXTRACTING", None)
        q = subprocess.run([sys.executable, str(SCRIPTS / "rebuild_decisions.py"), "--project", str(b), "--yes"],
                           capture_output=True, env=e)
        o = q.stdout.decode()
        assert q.returncode == 0 and "Re-attached 2 correction(s)" in o, o + q.stderr.decode()
        g = Graph(Store(b).records())
        assert "D-2" in g.rejected and g.rejected["D-2"]["decision"].startswith("Use Playwright"), g.rejected
        assert g.by_id["D-1"]["decision"] == "Host on Cloudflare Pages"

        # ---- server: token, origin and content-type checks; a correction round-trip
        c = project(root, "c")
        srv = dg_view._Server(c)
        t = threading.Thread(target=srv.httpd.serve_forever, daemon=True)
        t.start()
        base = srv.url.split("/?")[0]

        def call(path, body=None, headers=None):
            r = urllib.request.Request(base + path, data=body, headers=headers or {},
                                       method="POST" if body is not None else "GET")
            try:
                with urllib.request.urlopen(r) as f:
                    return f.status, f.read().decode()
            except urllib.error.HTTPError as ex:
                return ex.code, ex.read().decode()

        J = {"Content-Type": "application/json"}
        assert call("/")[0] == 403
        code, html = call(f"/?t={srv.token}")
        assert code == 200 and srv.token in html and '"D-17"' in html
        assert call("/api/correct", json.dumps({"t": "nope", "target": "D-9", "action": "reject"}).encode(), J)[0] == 403
        assert call("/api/correct", b"t=" + srv.token.encode(), {"Content-Type": "application/x-www-form-urlencoded"})[0] == 415
        assert call("/api/correct", json.dumps({"t": srv.token, "target": "D-9", "action": "reject"}).encode(),
                    {**J, "Origin": "http://evil.example"})[0] == 403
        code, body = call("/api/correct", json.dumps({"t": srv.token, "target": "D-9", "action": "reject"}).encode(), J)
        out = json.loads(body)
        assert code == 200 and out["ok"] and any(n["id"] == "D-9" and n["status"] == "rejected" for n in out["data"]["nodes"])
        code, body = call("/api/correct", json.dumps({"t": srv.token, "target": "D-9", "action": "frob"}).encode(), J)
        assert code == 400 and not json.loads(body)["ok"]
        srv.httpd.shutdown()

        print("all tests passed")


if __name__ == "__main__":
    main()
