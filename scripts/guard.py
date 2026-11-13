#!/usr/bin/env python3
"""
Contradiction warnings. No model call, so no added latency: the graph already records what was
rejected or replaced, and that's what gets checked.

  guard.py tool     PreToolUse hook. If Claude is about to run or write something the graph says was
                    rejected or replaced (e.g. `pnpm install` after "D-10: use Bun, replacing pnpm"),
                    pause once and show the user which decision it conflicts with. Read-only commands
                    (grep, cat, git log, ...) are never flagged. Each term pauses at most once per session;
                    after that Claude just gets a reminder.
  guard.py prompt   UserPromptSubmit hook. If the user's message mentions something that was rejected or
                    replaced, or touches a decision in force, Claude gets a short note with those decisions
                    so it can follow them or ask whether the user means to change them.

Set DECISION_GRAPH_GUARD=warn to never pause (Claude still gets the reminder), or =off to disable.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dg_store import GUARD_ENV, Graph, Store, project_dir, read_stdin_json, utf8_stdio  # noqa: E402

MODE = os.environ.get("DECISION_GRAPH_GUARD", "ask")

# Words too ordinary to act as a conflict signal on their own.
COMMON = set("""
about above after again against all also always among and another any are around back because been before being
below best better between both but can cannot change changes choose chosen code could data database decided decision
default does doing done down during each either else enough even every everything file files first for from full
get gets give good great has have heavy here high how instead into its just keep kept large later layer less light
like local long low made main make makes manager many maybe more most much must need needs never new next none not
now off old once one only other our out over package packages part per prefer project rather really rejected replace
replaced replacing same see should simple simpler small some still such than that the their them then there these
they thing things this those too tool tools under until use used uses using very want was way well were what when
where which while who why will with within without would yes yet you your app apps service services page pages
test tests testing build run runs fast faster slow slower easy hard free paid support supported requirement
requirements everything something nothing
""".split())
READ_ONLY = {"grep", "rg", "ag", "cat", "less", "more", "head", "tail", "ls", "dir", "find", "fd", "wc", "echo",
             "printf", "type", "which", "where", "whereis", "file", "stat", "tree", "diff", "dg", "select-string",
             "get-content", "get-childitem", "man", "help", "cd", "pushd", "popd", "pwd", "sort", "uniq", "cut",
             "tr", "column", "jq", "sed", "awk", "basename", "dirname", "realpath", "set-location", "test", "true"}
READ_ONLY_GIT = {"log", "diff", "show", "status", "blame", "grep", "branch", "shortlog"}
NEGATION = re.compile(r"\b(replacing|instead of|rather than|not|no|without|except|than)\s+(?:the\s+|a\s+)?([\w.+#/-]+(?:\s+[\w.+#/-]+){0,2})", re.I)
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9.+#_-]{2,}")


def tokens(text: str) -> set[str]:
    return {t.lower().strip(".-_") for t in TOKEN.findall(text or "")} - {""}


def distinctive(text: str) -> set[str]:
    return {t for t in tokens(text) if t not in COMMON and len(t) >= 3}


def affirmative(text: str) -> str:
    """The decision text without the parts it negates ("Use Bun, replacing pnpm" -> "Use Bun,")."""
    return NEGATION.sub(" ", text or "")


KNOWN_TOOLS = set("""
npm pnpm yarn bun deno node pip pipenv poetry uv conda mamba pdm hatch cargo rustup go gradle maven mvn sbt
docker podman kubectl helm terraform pulumi ansible vagrant jest vitest mocha jasmine ava cypress playwright puppeteer
webpack vite rollup esbuild parcel turbopack babel swc eslint prettier biome ruff black flake8 pylint mypy pyright
pytest tox nox sqlite postgres postgresql mysql mariadb mongodb mongo redis dynamodb firebase supabase prisma drizzle
sequelize typeorm knex sqlalchemy django flask fastapi express nest nestjs next nextjs nuxt remix astro svelte
sveltekit react vue angular solid tailwind bootstrap sass less styled-components emotion graphql rest grpc trpc
axios moment dayjs date-fns lodash zod yup joi celery kafka rabbitmq nginx apache caddy vercel netlify heroku render
fly cloudflare aws gcp azure electron tauri expo flutter
""".split())
TECH_MARK = re.compile(r"[0-9.@/+#]")


# Words that describe a kind of thing rather than name one. They never count as names on their own,
# but one right after a name stays with it: "Claude API" is avoided, the word "claude" alone isn't.
GENERIC = set("""api apis sdk cli client server model models llm llms ui ux json yaml orm library framework
service database db engine app tool tools plugin package module backend frontend""".split())
VERBISH = set("keep use using leave add make implement replace remove defer stay switch move".split())
INNER_MARK = re.compile(r"[0-9@/+#]|\.[a-z]", re.I)   # next.js, v0.2, @scope/pkg; not a sentence's final dot


def _clean(w: str) -> str:
    return w.strip(".,;:!?'\"()[]-_")


def _is_name(words: list[str], i: int, as_phrase: bool) -> bool:
    w = words[i]
    low = w.lower()
    if len(low) < 3 or low in GENERIC or low in VERBISH:
        return False
    if (as_phrase and i == 0 and len(words) > 1 and w[0].isupper() and words[1][:1].isupper()
            and words[1].lower() not in COMMON and words[1].lower() not in GENERIC):
        return True                                   # "Better Auth": an ordinary word that starts a name
    if low in COMMON:
        return False
    if INNER_MARK.search(w) or low in KNOWN_TOOLS:
        return True
    if not w[0].isupper():
        return False
    if i > 0:
        return True
    # first word: capitalised only because it starts the text, unless it's alone or clearly part of a name
    if len(words) == 1:
        return True
    nxt = words[1]
    return as_phrase and (nxt.lower() in GENERIC or (nxt[:1].isupper() and nxt.lower() not in COMMON))


def candidates(text: str, as_phrase: bool) -> set[str]:
    """Names in text: runs of name-like words ("Better Auth", "TinyLlama", "claude-sonnet-4-6"), plus a
    generic word that directly follows a single name ("Claude API"). Ordinary words never qualify."""
    words = [w for w in (_clean(t) for t in TOKEN.findall(text or "")) if w]
    out, i = set(), 0
    while i < len(words):
        if not _is_name(words, i, as_phrase):
            i += 1
            continue
        j = i + 1
        while j < len(words) and _is_name(words, j, as_phrase):
            j += 1
        run = [w.lower() for w in words[i:j]]
        if len(run) == 1 and j < len(words) and words[j].lower() in GENERIC:
            run.append(words[j].lower())
        out.add(" ".join(run[:3]))
        i = j
    return out


def avoid_terms(g: Graph) -> dict[str, list[dict]]:
    """term -> conflicts: names each decision in force rejected or replaced, minus names that any
    decision in force affirms."""
    affirmed = set()
    for r in g.active():
        affirmed |= tokens(affirmative(r["decision"]))
        affirmed |= candidates(affirmative(r["decision"]), False)
    out: dict[str, list[dict]] = {}
    for r in g.active():
        found = set()
        for alt in r.get("alternatives_rejected") or []:
            head = re.split(r"[,(;:]| - | because | as | since ", alt, maxsplit=1)[0]   # "Prisma, too heavy" -> "Prisma"
            found |= {(t, "rejected") for t in candidates(head, True)}
        for neg in NEGATION.finditer(r["decision"]):
            found |= {(t, "replaced") for t in candidates(neg.group(2), True)}
        prev, seen = r.get("supersedes"), set()
        while prev and prev in g.by_id and prev not in seen:             # everything this decision replaced
            seen.add(prev)
            found |= {(t, "replaced") for t in candidates(affirmative(g.by_id[prev]["decision"]), False)}
            prev = g.by_id[prev].get("supersedes")
        for t, how in found:
            if t in affirmed or any(part in affirmed for part in t.split()) and " " not in t:
                continue
            out.setdefault(t, [])
            if not any(c["id"] == r["id"] for c in out[t]):
                out[t].append({"id": r["id"], "decision": r["decision"], "how": how})
    return out


def find(text: str, terms: dict[str, list[dict]]) -> list[tuple[str, dict]]:
    low = (text or "").lower()
    hits = []
    for t, conflicts in terms.items():
        pat = r"[\s_/-]?".join(re.escape(p) for p in t.split())        # "better auth" matches better-auth, BetterAuth
        # not inside a path or a dotted name: ".claude/settings.json" isn't a use of "claude"
        if re.search(r"(?<![a-z0-9./\\])" + pat + r"(?![a-z0-9])", low):
            hits += [(t, c) for c in conflicts]
    return hits


# ------------------------------------------------------------------ tool calls
WRITE_REDIRECT = re.compile(r"(?<![0-9&])>>?(?!\s*&)|[0-9]>>?\s*(?!/dev/null|&|nul\b)[^\s&]", re.I)


def is_read_only(command: str) -> bool:
    cmd = re.sub(r"(['\"]).*?\1", "''", command or "")           # quoted text can't redirect or chain
    if WRITE_REDIRECT.search(cmd):
        return False                                              # writes a file: "> out.txt", ">> log"
    segments = re.split(r"\|\||&&|[|;]", cmd)
    for seg in segments:
        words = seg.strip().split()
        if not words:
            continue
        w0 = os.path.basename(words[0]).lower()
        if w0 in ("sudo", "env", "time", "npx", "bunx", "pnpx"):
            return False if w0 in ("npx", "bunx", "pnpx") else is_read_only(" ".join(words[1:]))
        if w0 == "git" and len(words) > 1 and words[1] in READ_ONLY_GIT:
            continue
        if w0 in ("python", "python3", "py") and any("dg.py" in w or "query_decisions" in w or "dg_" in w for w in words):
            continue
        if w0 not in READ_ONLY:
            return False
        if w0 == "find" and any(x in ("-delete", "-exec", "-execdir", "-ok", "-fprint") for x in words):
            return False
        if w0 == "sed" and any(x.startswith("-i") or x == "--in-place" for x in words):
            return False
        if w0 == "awk" and "system(" in seg:
            return False
    return True


def tool_text(tool: str, inp: dict) -> str:
    if tool == "Bash":
        cmd = inp.get("command", "")
        return "" if is_read_only(cmd) else cmd
    if tool in ("Write", "Edit", "MultiEdit"):
        path = str(inp.get("file_path", ""))
        if "/.claude/decisions/" in path.replace("\\", "/"):
            return ""
        body = inp.get("content") or inp.get("new_string") or ""
        if tool == "MultiEdit":
            body = "\n".join(e.get("new_string", "") for e in inp.get("edits") or [])
        return f"{path}\n{body[:200_000]}"
    return ""


def on_tool(payload: dict) -> dict | None:
    store = Store(project_dir(payload))
    records = store.records()
    if not records:
        return None
    g = Graph(records)
    text = tool_text(payload.get("tool_name", ""), payload.get("tool_input") or {})
    hits = find(text, avoid_terms(g)) if text else []
    if not hits:
        return None
    session = payload.get("session_id", "?")
    state = store.state()
    seen = set(state.setdefault("guard_seen", {}).get(session, []))
    term, c = hits[0]
    what = "rejected" if c["how"] == "rejected" else "replaced"
    claude_note = (f"Decision graph: this {payload.get('tool_name')} call uses \"{term}\", which decision {c['id']} {what} "
                   f"(\"{c['decision']}\"). Follow {c['id']} unless the user says otherwise; if they want to change it, "
                   f"say so plainly so the next compaction records a new decision.")
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": claude_note}}
    first = term not in seen
    if MODE == "ask" and first:
        out["hookSpecificOutput"]["permissionDecision"] = "ask"
        out["hookSpecificOutput"]["permissionDecisionReason"] = (
            f"Decision graph: this uses {term}, but {c['id']} says “{c['decision']}”. "
            f"Approve to go ahead anyway, or deny and Claude will stick to {c['id']}.")
    seen.add(term)
    try:
        with store:
            st = store.state()
            st.setdefault("guard_seen", {})[session] = sorted(seen)[-200:]
            store.save_state(st)
    except Exception:
        pass
    store.event("guard", where="tool", tool=payload.get("tool_name"), term=term, decision=c["id"],
                paused=bool(MODE == "ask" and first))
    return out


# ------------------------------------------------------------------ user prompts
def stems(text: str) -> set[str]:
    return {t[:6] for t in distinctive(text)}


def on_prompt(payload: dict) -> str | None:
    prompt = payload.get("prompt") or ""
    if not prompt.strip() or prompt.lstrip().startswith("/"):
        return None
    store = Store(project_dir(payload))
    records = store.records()
    if not records:
        return None
    g = Graph(records)
    lines = []
    conflicts = find(prompt, avoid_terms(g))
    flagged = set()
    for term, c in conflicts[:3]:
        what = "rejected" if c["how"] == "rejected" else "replaced"
        lines.append(f"- The message mentions \"{term}\", which {c['id']} {what}: \"{c['decision']}\". If the user wants "
                     f"to go back on {c['id']}, confirm that with them first; otherwise follow {c['id']}.")
        flagged.add(c["id"])
    ps = stems(prompt)
    related = []
    for r in g.active():
        if r["id"] in flagged:
            continue
        overlap = ps & stems(r["decision"] + " " + " ".join(r.get("rationale") or []))
        if len(overlap) >= 2 or any(len(o) >= 5 and o in ps for o in stems(r["decision"]) & ps):
            related.append((len(overlap), r))
    related.sort(key=lambda x: -x[0])
    for _, r in related[:4]:
        lines.append(f"- {r['id']} is in force: \"{r['decision']}\"" + (" (confirmed by the user)" if r.get("confirmed") else ""))
    if not lines:
        return None
    if conflicts:
        store.event("guard", where="prompt", term=conflicts[0][0], decision=conflicts[0][1]["id"], paused=False)
    return "Decision graph, decisions that bear on this message:\n" + "\n".join(lines)


def main() -> int:
    utf8_stdio()
    if os.environ.get(GUARD_ENV) or MODE == "off" or len(sys.argv) < 2:
        return 0
    try:
        payload = read_stdin_json()
        if sys.argv[1] == "tool":
            out = on_tool(payload)
            if out:
                print(json.dumps(out, ensure_ascii=False))
        elif sys.argv[1] == "prompt":
            ctx = on_prompt(payload)
            if ctx:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}},
                                 ensure_ascii=False))
    except Exception as e:  # a broken guard must never block work
        print(f"decision-graph guard: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
