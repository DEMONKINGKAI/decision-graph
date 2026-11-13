"""Run: python tests/test_rebuild.py  (rebuild from saved transcripts, mock model, no network)"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def line(kind, uuid, text, ts):
    return json.dumps({"type": kind, "uuid": uuid, "timestamp": ts, "message": {"role": kind, "content": text}})


def main():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        project = (root / "my-proj").resolve()
        project.mkdir()
        config = root / "cfg"
        tdir = config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project))
        tdir.mkdir(parents=True)
        # two sessions; file names deliberately out of chronological order
        (tdir / "zzz-older.jsonl").write_text(line("user", "u1", "use bun", "2026-11-01T10:00:00Z") + "\n")
        (tdir / "aaa-newer.jsonl").write_text(line("user", "u2", "use vitest", "2026-11-02T10:00:00Z") + "\n")
        # a transcript left behind by an old extraction call (0.1.0 saved them as sessions): must be ignored
        (tdir / "mmm-extraction.jsonl").write_text(
            line("user", "x1", "You extract DECISIONS from a software project conversation ...\n[T0] USER: use bun",
                 "2026-11-01T12:00:00Z") + "\n" +
            line("assistant", "x2", '[{"decision": "Use bun"}]', "2026-11-01T12:00:05Z") + "\n")

        # an existing (old-prompt) graph that must be moved aside, not deleted
        old = project / ".claude" / "decisions"
        old.mkdir(parents=True)
        (old / "decisions.jsonl").write_text(json.dumps({"id": "D-1", "decision": "old"}) + "\n")

        mock = root / "mock.json"
        mock.write_text(json.dumps([{"decision": "from transcript", "source_turns": [0]}]))
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(config), DECISION_GRAPH_BACKEND=f"mock:{mock}")
        env.pop("CLAUDE_PROJECT_DIR", None)
        env.pop("DECISION_GRAPH_EXTRACTING", None)

        p = subprocess.run([sys.executable, str(SCRIPTS / "rebuild_decisions.py"), "--project", str(project), "--yes"],
                           capture_output=True, env=env)
        out = p.stdout.decode()
        assert p.returncode == 0, out + p.stderr.decode()
        assert out.index("zzz-older") < out.index("aaa-newer"), out  # chronological, not alphabetical
        assert "Done: 2 decisions" in out, out
        assert "mmm-extraction.jsonl: skipped" in out, out

        backups = list((project / ".claude").glob("decisions.bak-*"))
        assert len(backups) == 1 and "old" in (backups[0] / "decisions.jsonl").read_text()
        recs = [json.loads(l) for l in (old / "decisions.jsonl").read_text().splitlines()]
        assert [r["id"] for r in recs] == ["D-1", "D-2"]
        assert [r["source"]["session_id"] for r in recs] == ["zzz-older", "aaa-newer"]

        # failure midway: clear message, partial graph kept; --resume continues without re-extracting
        (tdir / "zzz-older.jsonl").write_text((tdir / "zzz-older.jsonl").read_text())  # unchanged
        (tdir / "bbb-newest.jsonl").write_text(line("user", "u3", "use zod", "2026-11-03T10:00:00Z") + "\n")
        mock.write_text("not json at all")
        p = subprocess.run([sys.executable, str(SCRIPTS / "rebuild_decisions.py"), "--project", str(project),
                            "--yes", "--resume"], capture_output=True, env=env)
        out = p.stdout.decode()
        assert p.returncode == 1 and "Failed on bbb-newest.jsonl" in out and "--resume" in out, out
        mock.write_text(json.dumps([{"decision": "zod", "source_turns": [0]}]))
        p = subprocess.run([sys.executable, str(SCRIPTS / "rebuild_decisions.py"), "--project", str(project),
                            "--yes", "--resume"], capture_output=True, env=env)
        out = p.stdout.decode()
        assert p.returncode == 0 and "Done: 3 decisions" in out, out  # earlier sessions not duplicated
        assert len(list((project / ".claude").glob("decisions.bak-*"))) == 1  # resume doesn't move the graph

        print("all tests passed")


if __name__ == "__main__":
    main()
