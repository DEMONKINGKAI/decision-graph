"""Run: python3 tests/test_extract.py  (no network; uses the mock backend)"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "scripts" / "extract_decisions.py"


def line(kind, uuid, content, **extra):
    return json.dumps({"type": kind, "uuid": uuid, "timestamp": f"2026-11-11T12:00:{uuid[-2:]}Z",
                       "message": {"role": kind, "content": content}, **extra})


def run_hook(project, transcript, mock_reply, session="s1", extra_env=None):
    mock = project / "mock.json"
    mock.write_text(json.dumps(mock_reply))
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project), DECISION_GRAPH_BACKEND=f"mock:{mock}")
    env.pop("DECISION_GRAPH_EXTRACTING", None)
    env.update(extra_env or {})
    payload = {"session_id": session, "transcript_path": str(transcript), "cwd": str(project),
               "hook_event_name": "PreCompact", "trigger": "auto"}
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, env=env)
    return p


def records(project):
    f = project / ".claude" / "decisions" / "decisions.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


def main():
    with tempfile.TemporaryDirectory() as d:
        project = Path(d)
        tr = project / "t.jsonl"
        tr.write_text("\n".join([
            line("user", "u-01", "Let's store data in Postgres, we need concurrent writers."),
            line("assistant", "a-02", [{"type": "text", "text": "Agreed, Postgres."},
                                       {"type": "tool_use", "name": "Write", "input": {"file_path": "db.py"}}]),
            line("user", "u-03", [{"type": "tool_result", "content": "x" * 5000}]),
            line("user", "u-04", "summary", isCompactSummary=True),
            json.dumps({"type": "summary", "summary": "ignored"}),
        ]) + "\n")

        # --- run 1: two decisions, n2 depends on n1, plus one bad reference
        r = run_hook(project, tr, [
            {"key": "n1", "decision": "Use Postgres as the primary store.", "rationale": ["concurrent writers"],
             "supersedes": None, "depends_on": [], "decided_by": "user", "confidence": 0.9, "source_turns": [0, 1]},
            {"key": "n2", "decision": "Access the DB through db.py only.", "rationale": [],
             "depends_on": ["n1", "D-99"], "decided_by": "assistant_accepted", "confidence": 2, "source_turns": [1, 42]},
        ])
        assert r.returncode == 0, r.stderr
        recs = records(project)
        assert [x["id"] for x in recs] == ["D-1", "D-2"], recs
        assert recs[1]["depends_on"] == ["D-1"], "key n1 should resolve, D-99 should be dropped"
        assert recs[1]["confidence"] == 1.0
        assert recs[0]["source"]["turn_uuids"] == ["u-01", "a-02"]
        assert recs[1]["source"]["turn_uuids"] == ["a-02"], "out-of-range turn 42 dropped"

        # --- run 2 with no new lines: model must not be called (mock file missing would crash)
        (project / "mock.json").unlink()
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project), DECISION_GRAPH_BACKEND="mock:/nonexistent")
        p = subprocess.run([sys.executable, str(HOOK)], env=env, capture_output=True, text=True,
                           input=json.dumps({"session_id": "s1", "transcript_path": str(tr)}))
        assert p.returncode == 0 and len(records(project)) == 2, p.stderr

        # --- run 3: new turns reverse the DB choice -> supersession
        with open(tr, "a") as f:
            f.write(line("user", "u-06", "Actually switch to SQLite, it's single-user now.") + "\n")
        r = run_hook(project, tr, [
            {"key": "n1", "decision": "Use SQLite instead of Postgres.", "rationale": ["single user"],
             "alternatives_rejected": ["Postgres"], "supersedes": "D-1", "depends_on": [],
             "decided_by": "user", "confidence": 0.95, "source_turns": [0]},
        ])
        assert r.returncode == 0, r.stderr
        recs = records(project)
        assert recs[-1]["id"] == "D-3" and recs[-1]["supersedes"] == "D-1"
        assert recs[-1]["source"]["turn_uuids"] == ["u-06"]

        # --- guard env: nested call must be a no-op
        r = run_hook(project, tr, [], extra_env={"DECISION_GRAPH_EXTRACTING": "1"})
        assert r.returncode == 0

        # --- bad model output: non-blocking failure (exit 1), cursor NOT advanced
        with open(tr, "a") as f:
            f.write(line("user", "u-07", "Use ruff for linting.") + "\n")
        r = run_hook(project, tr, "I could not find any decisions, sorry!")
        assert r.returncode == 1 and "extraction failed" in r.stderr
        state = json.loads((project / ".claude/decisions/state.json").read_text())
        assert state["cursors"]["s1"] == 6, state  # still before u-07, will retry next time
        r = run_hook(project, tr, [{"decision": "Use ruff for linting.", "source_turns": [0]}])
        assert r.returncode == 0 and records(project)[-1]["id"] == "D-4"

        # --- same-batch reversal (Kai's notes-app case): supersedes a NEW key, and the model
        #     also lists it in depends_on -> supersession kept, bogus dependency removed
        tr2 = project / "t2.jsonl"
        tr2.write_text(line("user", "u-a1", "Must work offline.") + "\n" +
                       line("user", "u-a2", "Drop offline, online-only.") + "\n")
        r = run_hook(project, tr2, [
            {"key": "n1", "decision": "The app must work fully offline.", "source_turns": [0]},
            {"key": "n2", "decision": "The app is online-only.", "supersedes": "n1",
             "depends_on": ["n1"], "source_turns": [1]},
        ], session="s2")
        assert r.returncode == 0, r.stderr
        a, b = records(project)[-2:]
        assert b["supersedes"] == a["id"] and b["depends_on"] == [], b

        # prompt tells the model reversals go through supersedes, including same-batch keys
        sys.path.insert(0, str(HOOK.parent))
        import extract_decisions as ed
        p = ed.build_prompt([{"role": "user", "text": "x", "uuid": "u"}], [])
        assert "Never express a reversal through \"depends_on\"" in p and "new key (n1)" in p
        assert "go with your suggestion" in p and "assistant_accepted" in p
        assert "is THREE records" in p and "who PROPOSED the choice" in p
        assert "Never invent or infer a motive" in p and "use []" in p

        # --- 0.2: restores + amendments (round-3 cases), in a fresh project
        p3 = project / "p3"
        p3.mkdir()
        tr3 = p3 / "t.jsonl"
        tr3.write_text(line("user", "u-b1", "Must work offline.") + "\n" +
                       line("user", "u-b2", "Store notes in SQLite because of offline.") + "\n" +
                       line("user", "u-b3", "Drop offline.") + "\n")
        def hook3(reply, session):
            mock = p3 / "mock.json"
            mock.write_text(json.dumps(reply))
            env = dict(os.environ, CLAUDE_PROJECT_DIR=str(p3), DECISION_GRAPH_BACKEND=f"mock:{mock}")
            env.pop("DECISION_GRAPH_EXTRACTING", None)
            return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(
                {"session_id": session, "transcript_path": str(tr3)}), capture_output=True, text=True, env=env)
        r = hook3([
            {"key": "n1", "decision": "Must work offline.", "source_turns": [0]},
            {"key": "n2", "decision": "Store notes in SQLite.", "depends_on": ["n1"], "source_turns": [1]},
            {"key": "n3", "decision": "Online-only.", "supersedes": "n1", "source_turns": [2]},
        ], "a")
        assert r.returncode == 0, r.stderr
        with open(tr3, "a") as f:
            f.write(line("user", "u-b4", "Offline is a requirement again.") + "\n")
            f.write(line("user", "u-b5", "We also picked SQLite because it's zero-ops.") + "\n")
        r = hook3([
            {"key": "n1", "decision": "Must work offline again.", "supersedes": "D-3", "restores": ["D-1"], "source_turns": [0]},
            {"amends": "D-2", "decision": "Store notes in SQLite.", "rationale": ["zero-ops"], "source_turns": [1]},
            {"amends": "D-99", "decision": "Ghost", "rationale": ["x"]},          # unknown target -> dropped
            {"amends": "D-2", "decision": "No reason given", "rationale": []},     # no reason -> dropped
        ], "a")
        assert r.returncode == 0, r.stderr
        recs3 = [json.loads(l) for l in (p3 / ".claude/decisions/decisions.jsonl").read_text().splitlines()]
        ids = [x["id"] for x in recs3]
        assert ids == ["D-1", "D-2", "D-3", "D-4", "A-1"], ids
        assert recs3[3]["supersedes"] == "D-3" and recs3[3]["restores"] == ["D-1"]
        assert recs3[4]["kind"] == "amendment" and recs3[4]["amends"] == "D-2" and recs3[4]["rationale"] == ["zero-ops"]
        assert recs3[4]["source"]["turn_uuids"] == ["u-b5"]

        # the prompt shows replaced decisions (so "restores" can point at them) and explains amendments
        g = ed.Graph(recs3[:3])
        p = ed.build_prompt([{"role": "user", "text": "x", "uuid": "u"}], g.active(), g.replaced())
        replaced_section = p.split("EARLIER DECISIONS THAT WERE REPLACED")[1].split("CONVERSATION")[0]
        assert "D-1: Must work offline." in replaced_section and "D-2" not in replaced_section
        assert '"amends"' in p and "do NOT drop the new reason" in p

        print("all tests passed")
        print((project / ".claude/decisions/extract.log").read_text())


if __name__ == "__main__":
    main()
