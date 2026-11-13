"""Run: python tests/test_topics.py  (areas, dg topics, "changed since your last visit"; no network, mock model)"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
DG = ROOT / "scripts" / "dg.py"


def dec(i, text, **kw):
    return {"id": f"D-{i}", "decision": text, "rationale": [], "depends_on": [], "supersedes": None,
            "decided_by": "user", "confidence": 0.9, "source": {"session_id": "s1", "turn_uuids": [],
            "decided_at": f"2026-11-0{i}T10:00:00Z"}, "extracted_at": f"2026-11-0{i}T10:00:00+00:00", **kw}


def main():
    from dg_store import Graph, Store, clean_topic
    import dg_view
    assert clean_topic("  Vector Storage!! ") == "vector storage"
    assert clean_topic("a b c d e") == "a b c" and clean_topic("") is None and clean_topic(None) is None

    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / ".claude" / "decisions").mkdir(parents=True)
        recs = [dec(1, "Use SQLite.", topic="Storage"), dec(2, "Use Vitest."), dec(3, "Use Playwright for e2e.")]
        (p / ".claude" / "decisions" / "decisions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))

        # an area recorded with the decision is used; untagged ones have none yet
        g = Graph(Store(p).records())
        assert g.topics == {"D-1": "storage"}, g.topics

        # dg topics tags the rest in one call, and keeps existing areas
        mock = p / "mock.json"
        mock.write_text(json.dumps({"D-2": "Testing", "D-3": "testing", "D-99": "ignored"}))
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(p), DECISION_GRAPH_BACKEND=f"mock:{mock}")
        r = subprocess.run([sys.executable, str(DG), "--project", str(p), "topics"], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "testing: 2" in r.stdout and "storage: 1" in r.stdout, r.stdout
        g = Graph(Store(p).records())
        assert g.topics == {"D-1": "storage", "D-2": "testing", "D-3": "testing"}, g.topics
        assert len(g.by_id) == 3, "a topics record must not count as a decision"

        # nothing left to tag: no model call, just the summary
        mock.write_text("not json")
        r = subprocess.run([sys.executable, str(DG), "--project", str(p), "topics"], capture_output=True, text=True, env=env)
        assert r.returncode == 0 and "2 areas" in r.stdout, r.stdout

        # the page gets areas, and decisions reworded after the last visit are marked as changed
        since = "2026-11-10T00:00:00+00:00"
        r = subprocess.run([sys.executable, str(DG), "--project", str(p), "edit", "D-2", "Use Vitest for unit tests."],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        data = dg_view.build_data(Store(p).records(), p, since)
        by = {n["id"]: n for n in data["nodes"]}
        assert by["D-1"]["topic"] == "storage" and by["D-2"]["topic"] == "testing"
        assert by["D-2"]["changed"] and not by["D-2"]["new"], by["D-2"]
        assert not by["D-1"]["changed"] and not by["D-3"]["changed"]
        data = dg_view.build_data(Store(p).records(), p, "2099-01-01T00:00:00+00:00")
        assert not any(n["changed"] for n in data["nodes"]), "nothing changed since a visit after every change"

        # the other commands keep working with a topics record in the file
        for cmd in (["active"], ["stale"], ["doctor", "--offline"], ["export", "--out", str(p / "D.md")]):
            r = subprocess.run([sys.executable, str(DG), "--project", str(p), *cmd], capture_output=True, text=True,
                               env=dict(env, DECISION_GRAPH_CLAUDE_BIN=sys.executable))
            assert "Traceback" not in r.stderr, (cmd, r.stderr)
    # recording fills in an area the model left out, with one follow-up call (fake claude answers both)
    if os.name != "nt":
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            fake = p / "claude"
            fake.write_text("#!/usr/bin/env python3\nimport sys\nq = sys.stdin.read()\n"
                            "print('[{\"decision\": \"Use SQLite.\", \"source_turns\": [0]}]' if 'You extract DECISIONS' in q "
                            "else '{\"D-1\": \"Storage\"}')\n")
            fake.chmod(0o755)
            tr = p / "t.jsonl"
            tr.write_text(json.dumps({"type": "user", "uuid": "u-01", "timestamp": "2026-11-12T10:00:01Z",
                                      "message": {"role": "user", "content": "Use SQLite."}}) + "\n")
            env = dict(os.environ, CLAUDE_PROJECT_DIR=str(p), DECISION_GRAPH_CLAUDE_BIN=str(fake))
            env.pop("DECISION_GRAPH_BACKEND", None); env.pop("DECISION_GRAPH_EXTRACTING", None)
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "extract_decisions.py")], capture_output=True, text=True, env=env,
                               input=json.dumps({"session_id": "s1", "transcript_path": str(tr), "cwd": str(p), "trigger": "manual"}))
            assert r.returncode == 0, r.stderr
            assert Graph(Store(p).records()).topics == {"D-1": "storage"}
    print("all tests passed")


if __name__ == "__main__":
    main()
