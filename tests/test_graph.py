"""Run: python tests/test_graph.py  (query script + SessionStart injection, no network)"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DECISION_GRAPH_NO_AUTOSETUP", "1")   # never set up the real computer from a test

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# D-1 Postgres -> superseded by D-4 SQLite. D-2 rests on D-1 (now stale). D-3 rests on D-2.
RECORDS = [
    {"id": "D-1", "decision": "Use Postgres as the primary store.", "rationale": ["concurrent writers"],
     "supersedes": None, "depends_on": [], "decided_by": "user", "confidence": 0.9, "source": {}},
    {"id": "D-2", "decision": "Use connection pooling via pgbouncer.", "rationale": ["many writers"],
     "supersedes": None, "depends_on": ["D-1"], "decided_by": "assistant_accepted", "confidence": 0.7, "source": {}},
    {"id": "D-3", "decision": "Deploy the DB proxy as a sidecar.", "rationale": [],
     "supersedes": None, "depends_on": ["D-2"], "decided_by": "user", "confidence": 0.8, "source": {}},
    {"id": "D-4", "decision": "Use SQLite instead of Postgres.", "rationale": ["single user now"],
     "alternatives_rejected": ["Postgres"], "supersedes": "D-1", "depends_on": [], "decided_by": "user",
     "confidence": 0.95, "source": {"decided_at": "2026-11-11T10:00:00Z", "session_id": "abcdef123456", "turn_uuids": ["u-06xxxxxx"]}},
    {"id": "D-5", "decision": "Lint with ruff — fast ✓", "rationale": [], "supersedes": None,
     "depends_on": [], "decided_by": "user", "confidence": 1.0, "source": {}},
]


def run(script, *args, project, stdin=""):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project))
    env.pop("DECISION_GRAPH_EXTRACTING", None)
    p = subprocess.run([sys.executable, str(SCRIPTS / script), *args], input=stdin.encode(),
                       capture_output=True, env=env)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def main():
    with tempfile.TemporaryDirectory() as d:
        project = Path(d)

        # empty project: query explains, injection is silent
        rc, out, _ = run("query_decisions.py", "active", project=project)
        assert rc == 0 and "No decisions recorded yet" in out
        rc, out, _ = run("inject_decisions.py", project=project, stdin='{"source":"startup"}')
        assert rc == 0 and "Decision graph is on for this project" in json.loads(out)["systemMessage"], out
        rc, out, _ = run("inject_decisions.py", project=project, stdin='{"source":"startup"}')
        assert rc == 0 and out == "", out   # announced once, then silent until there are decisions

        store = project / ".claude" / "decisions"
        store.mkdir(parents=True, exist_ok=True)
        (store / "decisions.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in RECORDS) + "\n{half-written", encoding="utf-8")

        rc, out, _ = run("query_decisions.py", "active", project=project)
        assert rc == 0 and "4 active decisions (5 recorded" in out, out
        assert "D-1:" not in out and "WARNING: rests on D-1, which was replaced by D-4" in out
        assert "indirectly affected: D-3 -> D-2" in out  # D-3 rests on D-2, which rests on replaced D-1

        rc, out, _ = run("query_decisions.py", "why", "d-3", project=project)  # case-insensitive id
        assert "D-2: Use connection pooling" in out and "SUPERSEDED by D-4" in out, out

        rc, out, _ = run("query_decisions.py", "impact", "D-1", project=project)
        assert "[directly] D-2" in out and "[2 steps away] D-3" in out, out

        rc, out, _ = run("query_decisions.py", "history", "D-1", project=project)
        assert out.index("D-1") < out.index("D-4") and "D-4" in out.splitlines()[1] and "in force" in out

        rc, out, _ = run("query_decisions.py", "stale", project=project)
        direct, indirect = out.split("Indirectly affected")
        assert "D-2" in direct and "replaced by D-4" in direct and "D-3:" not in direct, out
        assert "D-3:" in indirect and "via D-3 -> D-2" in indirect, out

        rc, out, _ = run("query_decisions.py", "search", "postgres", project=project)
        assert "D-1" in out and "D-4" in out and "SUPERSEDED" in out

        rc, out, _ = run("query_decisions.py", "show", "D-4", project=project)
        assert "rejected: Postgres" in out and "session abcdef12" in out

        rc, out, _ = run("query_decisions.py", "mermaid", project=project)
        assert out.startswith("graph TD") and "D1 -.->|superseded| D2" in out and "D2 --> D3" in out

        rc, _, err = run("query_decisions.py", "show", "D-99", project=project)
        assert rc != 0 and "no decision" in err

        # injection: lists only active decisions, flags stale, survives non-ASCII
        rc, raw, _ = run("inject_decisions.py", project=project, stdin='{"source":"compact"}')
        res = json.loads(raw)
        assert res["systemMessage"].startswith("Decision graph: 4 decisions in force, 1 stale (D-2)"), res["systemMessage"]
        out = res["hookSpecificOutput"]["additionalContext"]
        assert rc == 0 and "4 decisions currently in force" in out, out
        assert "- D-1:" not in out and "✓" in out and "D-1 was later replaced by D-4" in out
        lines = [l for l in out.splitlines() if l.startswith("- D-")]
        assert [l.split(":")[0] for l in lines] == ["- D-2", "- D-3", "- D-4", "- D-5"]

        # budget: oldest omitted first, with a note
        env_budget = dict(os.environ, CLAUDE_PROJECT_DIR=str(project), DECISION_GRAPH_INJECT_CHARS="420")
        p = subprocess.run([sys.executable, str(SCRIPTS / "inject_decisions.py")], input=b"{}",
                           capture_output=True, env=env_budget)
        o = json.loads(p.stdout.decode())["hookSpecificOutput"]["additionalContext"]
        assert "not listed here" in o and "D-5" in o and "- D-2" not in o, o

        # guard: silent inside the nested extraction call
        p = subprocess.run([sys.executable, str(SCRIPTS / "inject_decisions.py")], input=b"{}",
                           capture_output=True, env=dict(env_budget, DECISION_GRAPH_EXTRACTING="1"))
        assert p.stdout == b""

        # ---- round 3: offline comes back (restores D-1) + a reason added to D-3 (amendment)
        sys.path.insert(0, str(SCRIPTS))
        from dg_store import Store
        Store(project).append([{"id": "D-6", "decision": "The app must work fully offline again.", "rationale": [],
                                "supersedes": "D-4", "restores": ["D-1"], "depends_on": [], "source": {}},
                               {"id": "A-1", "kind": "amendment", "amends": "D-3", "decision": "Proxy as sidecar",
                                "rationale": ["keeps the app image small"], "source": {}}])

        rc, out, _ = run("query_decisions.py", "stale", project=project)
        assert "No active decision rests on a replaced one." in out, out
        assert "Resolved" in out and "rests on D-1, restored by D-6" in out, out

        rc, out, _ = run("query_decisions.py", "active", project=project)
        assert "WARNING" not in out and "[restores D-1]" in out and "A-1" not in out, out
        assert "4 active decisions (6 recorded" in out, out  # D-2, D-3, D-5, D-6; amendment not counted

        rc, out, _ = run("query_decisions.py", "show", "D-3", project=project)
        assert "because: keeps the app image small  (added later, A-1)" in out, out
        rc, out, _ = run("query_decisions.py", "show", "D-2", project=project)
        assert "restored by D-6, so it still holds" in out and "WARNING" not in out, out

        rc, out, _ = run("query_decisions.py", "why", "D-3", project=project)
        assert "RESTORED by D-6" in out, out

        rc, out, _ = run("query_decisions.py", "search", "image small", project=project)
        assert "A-1 (adds to D-3)" in out, out

        rc, out, _ = run("query_decisions.py", "mermaid", project=project)
        assert "D1 --> D2" in out and "D6 -.->|restores| D1" in out, out

        rc, raw, _ = run("inject_decisions.py", project=project, stdin="{}")
        out = json.loads(raw)["hookSpecificOutput"]["additionalContext"]
        assert "note:" not in out and "- D-6:" in out, out
        # new since the last session: D-7 recorded after the previous status line
        Store(project).append([{"id": "D-7", "decision": "Use Biome for linting.", "depends_on": [], "source": {},
                                "extracted_at": "2099-01-01T00:00:00+00:00"}])
        rc, raw, _ = run("inject_decisions.py", project=project, stdin='{"source":"compact"}')
        assert "1 recorded from that conversation (D-7)" in json.loads(raw)["systemMessage"], raw

        print("all tests passed")


if __name__ == "__main__":
    main()
