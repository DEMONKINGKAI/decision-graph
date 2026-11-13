"""Run: python tests/test_guard.py  (contradiction warnings; no network, no model)"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "scripts" / "guard.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "notes_app.jsonl"


def hook(project, mode, payload, env=None):
    e = dict(os.environ, CLAUDE_PROJECT_DIR=str(project))
    e.pop("DECISION_GRAPH_EXTRACTING", None)
    e.pop("DECISION_GRAPH_GUARD", None)
    e.update(env or {})
    p = subprocess.run([sys.executable, str(GUARD), mode], input=json.dumps(payload).encode(), capture_output=True, env=e)
    assert p.returncode == 0, p.stderr.decode()
    out = p.stdout.decode().strip()
    return json.loads(out) if out else None


def bash(project, cmd, session="s1", env=None):
    return hook(project, "tool", {"session_id": session, "tool_name": "Bash", "tool_input": {"command": cmd}}, env)


def main():
    sys.path.insert(0, str(ROOT / "scripts"))
    import guard
    from dg_store import Graph, Store
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "proj"
        (p / ".claude" / "decisions").mkdir(parents=True)
        shutil.copy(FIXTURE, p / ".claude" / "decisions" / "decisions.jsonl")

        # only names that were rejected or replaced, never ordinary words or things still in force
        terms = guard.avoid_terms(Graph(Store(p).records()))
        assert set(terms) == {"pnpm", "prisma", "jest", "better auth"}, sorted(terms)
        assert terms["pnpm"][0]["id"] == "D-10" and terms["prisma"][0]["how"] == "rejected"

        # a conflicting command pauses once per session, then only reminds Claude
        out = bash(p, "pnpm install")["hookSpecificOutput"]
        assert out["permissionDecision"] == "ask" and "D-10" in out["permissionDecisionReason"]
        assert "D-10" in out["additionalContext"] and "pnpm" in out["additionalContext"]
        out = bash(p, "pnpm add zod")["hookSpecificOutput"]
        assert "permissionDecision" not in out and "D-10" in out["additionalContext"]
        out = bash(p, "pnpm add zod", session="s2")["hookSpecificOutput"]       # new session: pause again
        assert out["permissionDecision"] == "ask"

        # what doesn't count
        assert bash(p, "bun install") is None
        assert bash(p, "grep -rn pnpm src | head") is None                        # read-only
        assert bash(p, "git log --oneline -- package.json | grep pnpm") is None
        assert bash(p, 'python scripts/dg.py search pnpm') is None              # the tool's own commands
        assert bash(p, "npx prisma migrate dev", session="s3")["hookSpecificOutput"]["permissionDecision"] == "ask"
        assert bash(p, "npm i better-auth", session="s4")["hookSpecificOutput"]["permissionDecision"] == "ask"
        assert bash(p, "import { auth } from '@clerk/nextjs'") is None           # "auth" alone is not "Better Auth"

        # writes: code that brings back a rejected choice is flagged; the graph's own files are not
        w = hook(p, "tool", {"session_id": "s5", "tool_name": "Write",
                             "tool_input": {"file_path": "/x/src/db.ts", "content": "import { PrismaClient } from '@prisma/client'"}})
        assert w["hookSpecificOutput"]["permissionDecision"] == "ask"
        assert hook(p, "tool", {"session_id": "s5", "tool_name": "Write",
                                "tool_input": {"file_path": str(p / ".claude/decisions/x.md"), "content": "prisma"}}) is None
        e = hook(p, "tool", {"session_id": "s6", "tool_name": "Edit",
                             "tool_input": {"file_path": "/x/package.json", "old_string": "a", "new_string": '"jest": "^29"'}})
        assert e["hookSpecificOutput"]["permissionDecision"] == "ask" and "D-9" in e["hookSpecificOutput"]["permissionDecisionReason"]

        # modes
        assert "permissionDecision" not in bash(p, "pnpm i", session="s7", env={"DECISION_GRAPH_GUARD": "warn"})["hookSpecificOutput"]
        assert bash(p, "pnpm i", session="s8", env={"DECISION_GRAPH_GUARD": "off"}) is None
        assert bash(p, "pnpm i", session="s9", env={"DECISION_GRAPH_EXTRACTING": "1"}) is None

        # prompts: conflicts and related decisions go to Claude, nothing for unrelated messages or commands
        ctx = hook(p, "prompt", {"prompt": "let's switch back to pnpm"})["hookSpecificOutput"]["additionalContext"]
        assert "D-10 replaced" in ctx and "confirm that with them first" in ctx
        ctx = hook(p, "prompt", {"prompt": "how is the sqlite schema migrated?"})["hookSpecificOutput"]["additionalContext"]
        assert "D-2 is in force" in ctx
        assert hook(p, "prompt", {"prompt": "add a settings page with dark mode"}) is None
        assert hook(p, "prompt", {"prompt": "/compact"}) is None

        # usage is logged for `dg report`
        ev = [x for x in Store(p).events() if x["kind"] == "guard"]
        assert sum(1 for x in ev if x["paused"]) >= 6 and any(x["where"] == "prompt" for x in ev)

        # no decisions: nothing at all
        empty = Path(d) / "empty"
        empty.mkdir()
        assert bash(empty, "pnpm install") is None and hook(empty, "prompt", {"prompt": "pnpm"}) is None
        # from a real project (Threadfall): replacing the Claude API must not flag the claude CLI or .claude paths,
        # and ordinary words after "instead of" / "not" are never terms
        real = Path(d) / "real"
        (real / ".claude" / "decisions").mkdir(parents=True)
        recs = [
            {"id": "D-13", "decision": "Narration and action classification use the Claude API (claude-sonnet-4-6).", "supersedes": None},
            {"id": "D-15", "decision": "The narrator uses the HuggingFace Inference API instead of the Claude API.", "supersedes": "D-13",
             "alternatives_rejected": ["Claude API for narration"]},
            {"id": "D-18", "decision": "The narrator model is Qwen/Qwen2.5-7B-Instruct.", "depends_on": ["D-15"],
             "alternatives_rejected": ["zephyr-7b-beta", "Phi-3-mini", "Ollama", "Political intrigue campaign"]},
            {"id": "D-44", "decision": "Implement the Tier 4 improvement items now instead of deferring them.",
             "alternatives_rejected": ["Keep Tier 4 deferred"]},
            {"id": "D-48", "decision": "Compute all marginals in a single query instead of one query per node."},
            {"id": "D-32", "decision": "Stream narration token by token over Server-Sent Events from a POST /stream_action endpoint."},
        ]
        (real / ".claude" / "decisions" / "decisions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
        terms = guard.avoid_terms(Graph(Store(real).records()))
        assert set(terms) == {"claude api", "claude-sonnet-4-6", "zephyr-7b-beta", "phi-3-mini", "ollama"}, sorted(terms)
        for cmd in ["claude plugin list", "cat ~/.claude/settings.json", "ls .claude/decisions"]:
            assert bash(real, cmd) is None, cmd
        # looking around is never paused, even in a chain that starts with cd (from a real session)
        look = 'cd "C:/x/Threadfall" && grep -o \'"claude api"[^,]*\' src/config.json; ls src | head -20; find . -name "*.py" | head'
        assert bash(real, look, session="s8") is None
        assert guard.is_read_only(look)
        for write in ["cd x && grep -rl claude-sonnet-4-6 src > hits.txt", "sed -i s/a/b/ narrator.py", "find . -name '*.pyc' -delete"]:
            assert not guard.is_read_only(write), write
        out = bash(real, "python call_claude_api.py", session="s9")
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "ask", out
        print("all tests passed")


if __name__ == "__main__":
    main()
