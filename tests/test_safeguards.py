"""Run: python tests/test_safeguards.py  (extractor refuses to create broken graphs; mock model)"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "scripts" / "extract_decisions.py"


def line(uuid, text):
    return json.dumps({"type": "user", "uuid": uuid, "timestamp": "2026-11-11T12:00:00Z",
                       "message": {"role": "user", "content": text}})


def extract(project: Path, reply, session):
    tr = project / f"{session}.jsonl"
    tr.write_text(line(session + "-1", "something") + "\n")
    mock = project / "mock.json"
    mock.write_text(json.dumps(reply))
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project), DECISION_GRAPH_BACKEND=f"mock:{mock}")
    env.pop("DECISION_GRAPH_EXTRACTING", None)
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(
        {"session_id": session, "transcript_path": str(tr)}), capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    recs = [json.loads(l) for l in (project / ".claude/decisions/decisions.jsonl").read_text().splitlines()]
    return {r["id"]: r for r in recs}


def main():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        # D-1 pnpm, D-2 bun replaces D-1
        extract(proj, [{"key": "a", "decision": "Use pnpm."},
                       {"key": "b", "decision": "Use bun.", "supersedes": "a"}], "s1")

        # replacing an already-replaced decision -> redirected to the version in force
        r = extract(proj, [{"decision": "Use npm.", "supersedes": "D-1"}], "s2")
        assert r["D-3"]["supersedes"] == "D-2", r["D-3"]

        # circular "rests on" within one batch -> second link dropped
        r = extract(proj, [{"key": "x", "decision": "X", "depends_on": ["y"]},
                           {"key": "y", "decision": "Y", "depends_on": ["x"]}], "s3")
        assert r["D-4"]["depends_on"] == ["D-5"] and r["D-5"]["depends_on"] == [], (r["D-4"], r["D-5"])

        # replacement loop within one batch -> dropped
        r = extract(proj, [{"key": "p", "decision": "P"},
                           {"key": "q", "decision": "Q", "supersedes": "p"},
                           {"key": "p2", "decision": "P again", "supersedes": "q"}], "s4")
        assert r["D-7"]["supersedes"] == "D-6" and r["D-8"]["supersedes"] == "D-7"  # a chain, not a loop

        # restoring something still in force -> dropped; restoring a replaced one -> kept
        r = extract(proj, [{"decision": "Bring back X", "restores": ["D-4"]},
                           {"decision": "Back to pnpm", "supersedes": "D-3", "restores": ["D-1"]}], "s5")
        assert r["D-9"]["restores"] == [] and r["D-10"]["restores"] == ["D-1"], (r["D-9"], r["D-10"])

        log = (proj / ".claude/decisions/extract.log").read_text()
        assert "already replaced by D-2; replacing D-2" in log
        assert "would create a circular link" in log
        assert "'restores D-4', D-4 is still in force" in log
        print("all tests passed")


if __name__ == "__main__":
    main()
