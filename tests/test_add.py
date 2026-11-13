"""Run: python tests/test_add.py  (dg add, used by /decision-graph:save in Chat and Cowork; no network)"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DG = ROOT / "scripts" / "dg.py"
sys.path.insert(0, str(ROOT / "scripts"))


def dg(project, *args, stdin=None, env=None):
    e = dict(os.environ, **(env or {}))
    e.pop("DECISION_GRAPH_BACKEND", None)
    return subprocess.run([sys.executable, str(DG), "--project", str(project), *args], input=stdin,
                          capture_output=True, text=True, env=e)


def records(project):
    f = project / ".claude" / "decisions" / "decisions.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


def main():
    from dg_store import Graph, Store
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "proj"
        p.mkdir()
        first = [{"key": "n1", "decision": "Store data in SQLite.", "topic": "storage", "decided_by": "user"},
                 {"key": "n2", "decision": "Use Drizzle as the ORM.", "depends_on": ["n1"], "decided_by": "assistant_accepted",
                  "rationale": ["Typed queries."], "alternatives_rejected": ["Prisma"]}]
        f = Path(d) / "a.json"
        f.write_text("Here you go:\n```json\n" + json.dumps(first) + "\n```\n")   # pasted with prose and fences

        # dry run changes nothing
        r = dg(p, "add", "--file", str(f), "--dry-run")
        assert r.returncode == 0 and "Would add 2 decision(s)" in r.stdout, r.stdout + r.stderr
        assert records(p) == []

        # from a file
        r = dg(p, "add", "--file", str(f), "--session", "storage choices")
        assert r.returncode == 0 and "Added 2 decision(s)" in r.stdout, r.stdout + r.stderr
        recs = records(p)
        assert [x["id"] for x in recs] == ["D-1", "D-2"] and recs[1]["depends_on"] == ["D-1"]
        assert recs[0]["source"]["session_id"] == "chat: storage choices" and recs[0]["source"]["decided_at"]
        assert recs[0]["recorded_from"] == "chat" and recs[0]["topic"] == "storage"

        # from stdin: a replacement of an existing decision, an added reason, and a link to nothing
        second = [{"key": "n1", "decision": "Store data in Postgres.", "supersedes": "D-1", "decided_by": "user",
                   "rationale": ["Several writers now."], "depends_on": ["D-77"]},
                  {"decision": "Use Drizzle as the ORM.", "amends": "D-2", "rationale": ["Works with Postgres too."]}]
        r = dg(p, "add", "--session", "chat: switch db", stdin=json.dumps(second))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "replaces D-1" in r.stdout and "new reason for D-2" in r.stdout and "D-77" in r.stdout, r.stdout
        assert "stale now: D-2" in r.stdout, r.stdout
        g = Graph(Store(p).records())
        assert not g.is_active("D-1") and g.current("D-1") == "D-3"
        assert [t for t, _ in g.rationale("D-2")] == ["Typed queries.", "Works with Postgres too."]
        events = [json.loads(l) for l in (p / ".claude/decisions/events.jsonl").read_text().splitlines()]
        assert [e["trigger"] for e in events if e["kind"] == "extracted"] == ["chat save", "chat save"]

        # from the clipboard (a stand-in clipboard tool on PATH)
        if os.name != "nt":
            bin_ = Path(d) / "bin"
            bin_.mkdir()
            tool = "pbpaste" if sys.platform == "darwin" else "xclip"
            (bin_ / tool).write_text("#!/bin/sh\ncat <<'EOF'\n" + json.dumps([{"decision": "Use pnpm.", "decided_by": "user"}]) + "\nEOF\n")
            (bin_ / tool).chmod(0o755)
            r = dg(p, "add", "--clipboard", env={"PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}"})
            assert r.returncode == 0 and "Use pnpm." in r.stdout, r.stdout + r.stderr

        # bad input never writes anything
        n = len(records(p))
        r = dg(p, "add", stdin="no json here")
        assert r.returncode == 1 and "Couldn't read" in r.stdout
        r = dg(p, "add", stdin=json.dumps([{"nope": 1}, "text"]))
        assert r.returncode == 0 and "Nothing was added" in r.stdout
        assert len(records(p)) == n
    print("all tests passed")


if __name__ == "__main__":
    main()
