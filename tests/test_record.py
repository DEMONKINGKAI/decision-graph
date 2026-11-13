"""Run: python tests/test_record.py  (dg record, the SessionEnd hook, dg export; no network, mock model)"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DG = ROOT / "scripts" / "dg.py"
RECORD = ROOT / "scripts" / "record_decisions.py"


def line(kind, uuid, content):
    return json.dumps({"type": kind, "uuid": uuid, "timestamp": f"2026-11-11T12:00:{uuid[-2:]}Z",
                       "message": {"role": kind, "content": content}})


def records(project):
    f = project / ".claude" / "decisions" / "decisions.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


def main():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        project, cfg = d / "proj", d / "cfg"
        project.mkdir()
        tdir = cfg / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project.resolve()))
        tdir.mkdir(parents=True)
        mock = d / "mock.json"
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg), CLAUDE_PROJECT_DIR=str(project),
                   DECISION_GRAPH_BACKEND=f"mock:{mock}")
        env.pop("DECISION_GRAPH_EXTRACTING", None)

        def say(session, text):
            (tdir / f"{session}.jsonl").write_text("\n".join([
                line("user", f"{session}-u01", text),
                line("assistant", f"{session}-a02", [{"type": "text", "text": "Agreed."}]),
            ]) + "\n")

        def reply(decision):
            mock.write_text(json.dumps([{"key": "n1", "decision": decision, "rationale": [], "depends_on": [],
                                         "decided_by": "user", "confidence": 0.9, "source_turns": [0]}]))

        # dg record: reads the conversation without a compaction
        say("s-a", "Use SQLite for storage.")
        reply("Use SQLite for storage.")
        r = subprocess.run([sys.executable, str(DG), "--project", str(project), "record"], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "D-1: Use SQLite for storage." in r.stdout, r.stdout
        assert [x["decision"] for x in records(project)] == ["Use SQLite for storage."]

        # a second run finds nothing new: the cursor moved on
        r = subprocess.run([sys.executable, str(DG), "--project", str(project), "record"], capture_output=True, text=True, env=env)
        assert "Nothing new" in r.stdout, r.stdout
        assert len(records(project)) == 1

        # SessionEnd: returns at once, the extraction finishes in the background
        say("s-b", "Use Vitest for tests.")
        reply("Use Vitest for tests.")
        payload = {"session_id": "s-b", "transcript_path": str(tdir / "s-b.jsonl"), "cwd": str(project),
                   "hook_event_name": "SessionEnd", "reason": "prompt_input_exit"}
        t0 = time.time()
        r = subprocess.run([sys.executable, str(RECORD), "--session-end"], input=json.dumps(payload),
                           capture_output=True, text=True, env=env, timeout=10)
        assert r.returncode == 0, r.stderr
        assert time.time() - t0 < 3, "the hook must not wait for the extraction"
        for _ in range(100):
            if len(records(project)) == 2:
                break
            time.sleep(0.1)
        recs = records(project)
        assert [x["decision"] for x in recs][-1] == "Use Vitest for tests.", recs
        events = [json.loads(l) for l in (project / ".claude" / "decisions" / "events.jsonl").read_text().splitlines()]
        assert any(e.get("trigger") == "session end" for e in events), events

        # inside our own extraction call the hook does nothing
        r = subprocess.run([sys.executable, str(RECORD), "--session-end"], input=json.dumps(payload),
                           capture_output=True, text=True, env=dict(env, DECISION_GRAPH_EXTRACTING="1"))
        assert r.returncode == 0

        # dg export: a decision log, and it won't overwrite a file it didn't write
        r = subprocess.run([sys.executable, str(DG), "--project", str(project), "export"], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        text = (project / "DECISIONS.md").read_text(encoding="utf-8")
        assert "# Decision log: proj" in text and "### D-2" in text and "Use Vitest for tests." in text
        mine = project / "NOTES.md"
        mine.write_text("my own notes")
        r = subprocess.run([sys.executable, str(DG), "--project", str(project), "export", "--out", str(mine)],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 1 and mine.read_text() == "my own notes", r.stdout
        r = subprocess.run([sys.executable, str(DG), "--project", str(project), "export"], capture_output=True, text=True, env=env)
        assert r.returncode == 0, "re-exporting over its own file is fine"
        print("all tests passed")


if __name__ == "__main__":
    main()
