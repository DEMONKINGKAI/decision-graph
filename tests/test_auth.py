"""Run: python3 tests/test_auth.py  (POSIX only: uses a fake `claude` script)

Checks which credentials the nested `claude -p` call gets, using a fake claude that
reports what it saw in its environment.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "scripts" / "extract_decisions.py"

FAKE = r'''#!/usr/bin/env python3
import os, sys, json
sys.stdin.read()
log = os.environ["FAKE_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"key": os.environ.get("ANTHROPIC_API_KEY"),
                        "claudecode": os.environ.get("CLAUDECODE"),
                        "session": os.environ.get("CLAUDE_CODE_SESSION_ID"),
                        "config": os.environ.get("CLAUDE_CONFIG_DIR")}) + "\n")
logged_in = os.environ.get("FAKE_LOGGED_IN") == "1"
if os.environ.get("ANTHROPIC_API_KEY"):
    if os.environ.get("FAKE_KEY_HAS_CREDIT") == "1":
        print('[{"decision": "via api key", "topic": "auth", "source_turns": [0]}]'); sys.exit(0)
    print("Credit balance is too low"); sys.exit(1)
if logged_in:
    print('[{"decision": "via login", "topic": "auth", "source_turns": [0]}]'); sys.exit(0)
print("Not logged in · Please run /login"); sys.exit(1)
'''


def scenario(tmp: Path, name: str, **env_over):
    proj = tmp / name
    proj.mkdir()
    tr = proj / "t.jsonl"
    tr.write_text(json.dumps({"type": "user", "uuid": "u1", "message": {"role": "user", "content": "use X"}}) + "\n")
    fake = tmp / "claude"
    log = proj / "calls.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC", "DECISION_GRAPH"))}
    env.update(CLAUDE_PROJECT_DIR=str(proj), DECISION_GRAPH_CLAUDE_BIN=str(fake), FAKE_LOG=str(log),
               CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="parent", CLAUDE_CONFIG_DIR="/cfg")
    env.update(env_over)
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(
        {"session_id": name, "transcript_path": str(tr)}).encode(), capture_output=True, env=env)
    calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    recf = proj / ".claude/decisions/decisions.jsonl"
    recs = [json.loads(l) for l in recf.read_text().splitlines()] if recf.exists() else []
    return p, calls, recs


def main():
    if os.name == "nt":
        print("skipped on Windows")
        return
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fake = tmp / "claude"
        fake.write_text(FAKE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

        # Kai's case: logged in, plus a user-level API key with no credit -> uses login, one call
        p, calls, recs = scenario(tmp, "kai", ANTHROPIC_API_KEY="sk-empty", FAKE_LOGGED_IN="1")
        assert p.returncode == 0, p.stderr
        assert len(calls) == 1 and calls[0]["key"] is None
        assert recs[0]["decision"] == "via login"
        # session state stripped, user config kept
        assert calls[0]["claudecode"] is None and calls[0]["session"] is None and calls[0]["config"] == "/cfg"

        # API-key-only user: login attempt fails with "Not logged in" -> retry with key
        p, calls, recs = scenario(tmp, "keyonly", ANTHROPIC_API_KEY="sk-good", FAKE_KEY_HAS_CREDIT="1")
        assert p.returncode == 0, p.stderr
        assert [c["key"] for c in calls] == [None, "sk-good"] and recs[0]["decision"] == "via api key"

        # no key, not logged in -> one call, clear error, non-blocking exit
        p, calls, recs = scenario(tmp, "nothing")
        assert p.returncode == 1 and len(calls) == 1 and not recs
        assert "Not logged in" in (tmp / "nothing/.claude/decisions/extract.log").read_text()

        # explicit api-key mode never tries the login
        p, calls, recs = scenario(tmp, "forced", ANTHROPIC_API_KEY="sk-good", FAKE_KEY_HAS_CREDIT="1",
                                  FAKE_LOGGED_IN="1", DECISION_GRAPH_AUTH="api-key")
        assert [c["key"] for c in calls] == ["sk-good"] and recs[0]["decision"] == "via api key"

        print("all tests passed")


if __name__ == "__main__":
    main()
