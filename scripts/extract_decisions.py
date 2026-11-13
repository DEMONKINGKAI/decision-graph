#!/usr/bin/env python3
"""
PreCompact hook: extract decision records from a Claude Code transcript.

Reads the hook payload (JSON) on stdin, takes only transcript lines it has not
processed yet for this session, asks a model to pull out decisions, validates
them, and appends them to an append-only JSONL store:

    <project>/.claude/decisions/decisions.jsonl   one record per line
    <project>/.claude/decisions/state.json        per-session line cursors
    <project>/.claude/decisions/extract.log       diagnostics

Design rules:
  * Never block compaction: always exit 0, log problems instead.
  * Append-only: a reversed decision is a NEW record with `supersedes`.
  * Provenance: every record points back to transcript entry uuids.

Stdlib only; runs on Windows, macOS and Linux. Extraction backend is the `claude` CLI in headless mode (uses your
existing Claude Code login), configurable through env vars:
    DECISION_GRAPH_MODEL      model alias for extraction (default: haiku)
    DECISION_GRAPH_BACKEND    "claude-cli" (default) or "mock:<path to json>"
    DECISION_GRAPH_MAX_CHARS  max transcript chars per extraction call (default 120000)
    DECISION_GRAPH_CLAUDE_BIN explicit path to the claude executable (default: found on PATH)
    DECISION_GRAPH_AUTH       auto (default: subscription login, API key only if not logged in),
                              login, or api-key
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dg_store import GUARD_ENV, Graph, find_claude, Store, clean_topic, id_num, now_iso, project_dir, read_stdin_json, rejected_ids, utf8_stdio  # noqa: E402

MAX_CHARS = int(os.environ.get("DECISION_GRAPH_MAX_CHARS", "120000"))
MODEL = os.environ.get("DECISION_GRAPH_MODEL", "haiku")
BACKEND = os.environ.get("DECISION_GRAPH_BACKEND", "claude-cli")
AUTH_MODE = os.environ.get("DECISION_GRAPH_AUTH", "auto")  # auto | login | api-key
TOOL_RESULT_CHARS = 300
CLI_TIMEOUT_S = 240


def active_decisions(records: list[dict]) -> list[dict]:
    return Graph(records).active()


def next_id_counter(records: list[dict]) -> int:
    return max((id_num(r.get("id", "")) for r in records), default=0) + 1


# ------------------------------------------------------- transcript parsing
def render_content(content) -> str:
    """Turn a message's content (string or block list) into compact text."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            parts.append(block.get("text", ""))
        elif t == "tool_use":
            inp = block.get("input", {})
            target = inp.get("file_path") or inp.get("command") or inp.get("pattern") or ""
            parts.append(f"[tool call: {block.get('name')} {str(target)[:120]}]")
        elif t == "tool_result":
            body = block.get("content")
            if isinstance(body, list):
                body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
            body = str(body or "")
            if len(body) > TOOL_RESULT_CHARS:
                body = body[:TOOL_RESULT_CHARS] + " …[truncated]"
            parts.append(f"[tool result: {body}]")
        # thinking / images are skipped on purpose
    return "\n".join(p for p in parts if p.strip())


def load_turns(transcript: Path, start_line: int) -> tuple[list[dict], int]:
    """Return (turns, total_lines). Turns only come from lines >= start_line."""
    turns = []
    lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, raw in enumerate(lines):
        if i < start_line or not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue
        if entry.get("isCompactSummary") or entry.get("isMeta"):
            continue  # don't re-extract from Claude's own summaries
        msg = entry.get("message") or {}
        text = render_content(msg.get("content"))
        if not text.strip():
            continue
        if text.lstrip().startswith(PROMPT_SIGNATURE):
            return [], len(lines)  # this transcript is one of our own extraction calls: ignore it entirely
        turns.append({
            "uuid": entry.get("uuid", f"line-{i}"),
            "role": msg.get("role") or entry["type"],
            "timestamp": entry.get("timestamp"),
            "text": text,
        })
    return turns, len(lines)


def chunk_turns(turns: list[dict]) -> list[list[dict]]:
    chunks, cur, size = [], [], 0
    for t in turns:
        n = len(t["text"]) + 40
        if cur and size + n > MAX_CHARS:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += n
    if cur:
        chunks.append(cur)
    return chunks


# -------------------------------------------------------------- extraction
PROMPT_SIGNATURE = "You extract DECISIONS from a software project conversation"
PROMPT = """You extract DECISIONS from a software project conversation between a user and an AI assistant.

A decision is a choice that constrains future work: a technology, architecture, design, scope, naming,
process or requirement that was AGREED (by the user, or proposed by the assistant and accepted by the user).
NOT decisions: options merely discussed, assistant suggestions the user did not accept, transient debugging
steps, facts about the code that were not chosen, and the order in which to do the work ("build the engine
first, then the API", "dockerize after the RAG work"): a work plan stops mattering once it is done. A sequence
counts only if it is a lasting rule for the product itself (e.g. "acts unlock in order"). Choosing WHAT is in
scope (which features or improvement items get built, which are dropped or deferred) IS a decision; only the
ORDER in which agreed work is done is not.

EXISTING ACTIVE DECISIONS (reference these ids when relevant):
{existing}

EXISTING AREAS (reuse these names for "topic"; add a new one only if none fits):
{areas}

EARLIER DECISIONS THAT WERE REPLACED (only for "restores"):
{replaced}

CONVERSATION (each turn is labeled [T<n>]):
{transcript}

Return ONLY a JSON array (possibly empty), no prose, no code fences. Each element:
{{
  "key": "short local label for this new decision, e.g. n1, n2",
  "decision": "one sentence stating what was decided, self-contained; never mention ids or keys in it",
  "rationale": ["a reason that was actually SAID in the conversation, short", "..."],
  "alternatives_rejected": ["option considered and rejected", "..."],
  "supersedes": "id (D-n) or new key (n1) of the decision this one reverses, replaces or overrides, else null",
  "depends_on": ["ids (D-n) or new keys (n1) of decisions that are REASONS for this one"],
  "restores": ["ids of EARLIER REPLACED decisions this one brings back into force, else empty"],
  "amends": "id (D-n) of an existing decision this item only adds a reason to, else null",
  "topic": "the area of the project this belongs to, 1-3 lowercase words (e.g. narrator, storage, frontend style, testing); reuse one of the EXISTING AREAS when it fits",
  "decided_by": "user" | "assistant_accepted",
  "confidence": 0.0-1.0,
  "source_turns": [turn numbers where it was made, e.g. 4, 5]
}}
Linking rules:
- A reversal or change of an earlier decision ("actually, switch to X", "drop Y") is its own record whose
  "supersedes" points at the decision it overrides. This applies whether the earlier decision is an existing
  D-n or a new key from this same conversation. Never express a reversal through "depends_on".
- "depends_on" means "this rests on that": it was chosen BECAUSE of that, OR it only makes sense while that
  holds, because it configures, tunes, extends or fixes something that decision set up. Examples: "map the verb
  flee to combat" rests on "action classification uses keyword matching" (switch to an LLM classifier and the
  verb map is moot); "the narrator model is Qwen 7B" rests on "the narrator runs on the HuggingFace API";
  "the README uses the frontend's gothic style" rests on "the frontend uses a gothic style". Ask: if that
  decision were reversed, would this one need revisiting? If yes, link it. Never link a reversal this way.
- Never link to a work plan, batch, roadmap or task list ("implement Tier 1 now", "fix the first four audit
  items") just because something was built as part of it. A feature does not rest on the plan it was built
  under: changing the plan doesn't make the feature wrong.
- Record both the original decision and its reversal; history matters.
- Widening the scope of an earlier decision ("do Tier 2 now too", later "and Tiers 3 and 4") is one change of
  that decision. Within this conversation, write ONE record with the final scope ("implement all four tiers
  now") that supersedes the original, not a chain where each widening replaces the previous one.
- If a reversal brings back something that was decided before and later replaced ("offline is a requirement
  again", "go back to pnpm"), set "supersedes" to the decision it overrides now AND "restores" to the earlier
  replaced decision it reinstates.
- If the user only adds a reason, detail or justification to a decision that already exists, without changing
  it ("we also picked Drizzle for its type safety"), output an item with "amends" set to that decision's id,
  "rationale" holding the new reason(s), and "decision" restating the existing decision. Do NOT create a
  duplicate decision, and do NOT drop the new reason.

Reasons:
- "rationale" holds only reasons someone actually gave in the conversation (the user, or the assistant in a
  proposal the user accepted). Paraphrase briefly; keep the meaning, translate to English if needed.
- If no reason was given, use []. An empty list is correct and expected for many decisions.
- Never invent or infer a motive ("keeps things simple", "for consistency", "best practice"), and never write
  where the decision came from ("stated by the user", "user requirement"). Those are not reasons.
- A reason can come from a linked decision only if the user said so ("because we're on SQLite").

One choice per record:
- Each record holds ONE choice that could later be changed on its own. Split compound statements:
  "Vitest for unit tests and Playwright for e2e, all tests under /tests" is THREE records, because switching
  e2e tools later must not touch the unit-test runner or the folder layout.
- Keep together only what can't change independently (a convention and how it is applied, e.g.
  "snake_case columns, camelCase in code, mapped in the repository layer").
- Choices made in the same breath don't rest on each other just because they were made together; link them
  only when one is a reason for the other or only makes sense because of it.
- Several instances of the SAME kind of change made together (mapping three verbs, renaming four fields,
  adding two config keys) are ONE record listing them, not one record each.

Acceptance by reference:
- Users often accept a proposal without restating it: "sounds good", "go with your suggestion", "do that",
  "yes good idea, do that for all routes", "yes, the second option", "ok let's do it", "ok, dann wie du sagst".
  The decided_by value depends on who PROPOSED the choice, not who said yes: if the assistant proposed it and
  the user agreed, it is "assistant_accepted", even when the user adds a detail like "for all routes". When the assistant's previous turn recommended something
  concrete and the user accepts it this way, that IS a decision: write out what was accepted in full
  (e.g. "Use Tailwind CSS for styling."), set "decided_by" to "assistant_accepted", and cite both the
  assistant turn and the accepting user turn in "source_turns".
- If the assistant offered several options without recommending one, a vague "sounds good" is not a decision.
- A user rejecting or postponing a proposal ("nah", "not now", "maybe later") is not a decision to adopt it.
Only include decisions that are clearly present. Prefer fewer, high-confidence records."""


def build_prompt(chunk: list[dict], active: list[dict], replaced: list[dict] | None = None,
                 areas: list[str] | None = None) -> str:
    existing = "\n".join(f"- {r['id']}: {r['decision']}" for r in active) or "(none)"
    old = "\n".join(f"- {r['id']}: {r['decision']}" for r in replaced or []) or "(none)"
    transcript = "\n\n".join(f"[T{i}] {t['role'].upper()}: {t['text']}" for i, t in enumerate(chunk))
    return PROMPT.format(existing=existing, replaced=old, transcript=transcript,
                         areas=", ".join(areas or []) or "(none yet)")


def call_model(prompt: str) -> str:
    if BACKEND.startswith("mock:"):
        return Path(BACKEND[5:]).read_text(encoding="utf-8")
    exe = find_claude()  # resolves claude.cmd/.exe on Windows
    if not exe:
        raise RuntimeError("Claude Code (`claude`) wasn't found; install it (https://code.claude.com/docs/en/setup) or set DECISION_GRAPH_CLAUDE_BIN")
    def attempt(keep_api_key: bool):
        return subprocess.run(
            [exe, "-p", "--model", MODEL, "--output-format", "text",
             "--tools", "",                 # pure text task: no tool calls
             "--strict-mcp-config",         # no MCP servers / connectors
             "--no-session-persistence"],   # don't litter the session list
            input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=child_env(keep_api_key), cwd=tempfile.gettempdir(), timeout=CLI_TIMEOUT_S,
        )

    # Headless `claude -p` uses ANTHROPIC_API_KEY whenever it is set, even when the
    # user's interactive sessions run on their Claude subscription login. Prefer the
    # login ("auto"); fall back to the key only if there is no login.
    mode = AUTH_MODE if AUTH_MODE in ("auto", "login", "api-key") else "auto"
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    proc = attempt(keep_api_key=(mode == "api-key"))
    if (mode == "auto" and has_key and proc.returncode != 0
            and re.search(r"not logged in|/login", proc.stdout + proc.stderr, re.I)):
        proc = attempt(keep_api_key=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {proc.returncode}\n--- stderr ---\n{proc.stderr.strip()[:2000]}"
            f"\n--- stdout ---\n{proc.stdout.strip()[:2000]}")
    return proc.stdout


def child_env(keep_api_key: bool = False) -> dict:
    """Environment for the nested `claude -p`.

    Hooks inherit the parent session's environment, including CLAUDECODE and many
    CLAUDE_CODE_* session variables. Passed through, they make the child behave like
    part of the parent session (nested-session checks, a session-scoped auth source
    taking precedence over the stored login). Start it like a fresh terminal would:
    keep the user's own config (CLAUDE_CONFIG_DIR, ANTHROPIC_*), drop session state.
    """
    env = {k: v for k, v in os.environ.items()
           if not (k == "CLAUDECODE" or k.startswith("CLAUDE_")) or k == "CLAUDE_CONFIG_DIR"}
    if not keep_api_key:
        env.pop("ANTHROPIC_API_KEY", None)
    env[GUARD_ENV] = "1"
    return env


def parse_json_array(text: str) -> list:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON array in model output: {text[:200]}")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("model output is not a list")
    return data


def validate(raw: list, chunk: list[dict], records: list[dict], session_id: str,
             counter: int, store: Store) -> tuple[list[dict], int]:
    dead = rejected_ids(records)  # decisions the user rejected don't exist for linking purposes
    known_ids = {r["id"] for r in records if not r.get("kind") and r["id"] not in dead}
    amend_counter = max((int(r["id"][2:]) for r in records
                         if r.get("kind") == "amendment" and re.fullmatch(r"A-\d+", r.get("id", ""))), default=0) + 1
    key_to_id: dict[str, str] = {}
    staged = []
    # pass 1: assign ids so new records can depend on each other
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("decision", "")).strip():
            store.log(f"dropping malformed item: {str(item)[:120]}")
            continue
        if item.get("amends") and not item.get("supersedes"):
            new_id = f"A-{amend_counter}"
            amend_counter += 1
        else:
            item.pop("amends", None)
            new_id = f"D-{counter}"
            counter += 1
            if item.get("key"):
                key_to_id[str(item["key"])] = new_id
        staged.append((new_id, item))

    # graph state (existing + accepted so far in this batch) for the safety checks below
    succ = {r["supersedes"]: r["id"] for r in records
            if r.get("supersedes") and not r.get("kind") and r["id"] not in dead and r["supersedes"] in known_ids}
    adj = {r["id"]: list(r.get("depends_on") or []) for r in records if not r.get("kind") and r["id"] not in dead}

    def follow(x):
        seen = set()
        while x in succ and x not in seen:
            seen.add(x)
            x = succ[x]
        return x

    def reaches(start, goal):
        stack, seen = [start], set()
        while stack:
            x = stack.pop()
            if x == goal:
                return True
            if x in seen:
                continue
            seen.add(x)
            stack.extend(adj.get(x, []))
        return False

    out = []
    for new_id, item in staged:
        def resolve(ref):
            ref = str(ref)
            if ref in known_ids:
                return ref
            if ref in key_to_id and key_to_id[ref] != new_id:
                return key_to_id[ref]
            store.log(f"{new_id}: dropping unknown reference {ref!r}")
            return None

        sup = item.get("supersedes")
        sup = resolve(sup) if sup else None
        deps = [d for d in (resolve(x) for x in item.get("depends_on") or []) if d]
        if sup and sup in deps:  # a decision can't rest on the one it overrides
            store.log(f"{new_id}: removed {sup} from depends_on (it is superseded by this decision)")
            deps.remove(sup)

        # safety: never replace a decision twice; replace the version in force instead
        if sup and sup in succ:
            now = follow(sup)
            store.log(f"{new_id}: {sup} was already replaced by {succ[sup]}; replacing {now} (the version in force)")
            sup = now if now != new_id else None
        # safety: no replacement loops (a replaces b replaces a)
        if sup and follow(new_id) == sup:
            store.log(f"{new_id}: dropped 'replaces {sup}', it would create a loop")
            sup = None
        if sup and sup in deps:
            deps.remove(sup)
        # safety: no circular "rests on" links
        for d in list(deps):
            if reaches(d, new_id):
                store.log(f"{new_id}: dropped 'rests on {d}', it would create a circular link")
                deps.remove(d)

        restores = [x for x in (resolve(r) for r in item.get("restores") or []) if x and x != sup]
        # safety: only a replaced decision can be brought back
        for x in list(restores):
            if x not in succ and x != sup:
                store.log(f"{new_id}: dropped 'restores {x}', {x} is still in force")
                restores.remove(x)

        turns_idx = [i for i in item.get("source_turns") or [] if isinstance(i, int) and 0 <= i < len(chunk)]
        src = [chunk[i] for i in turns_idx]
        try:
            conf = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        source = {
            "session_id": session_id,
            "turn_uuids": [t["uuid"] for t in src],
            "decided_at": src[0]["timestamp"] if src else None,
        }
        if new_id.startswith("A-"):
            target = resolve(item["amends"])
            reasons = [str(x) for x in item.get("rationale") or []]
            if not target or not reasons:
                store.log(f"{new_id}: dropping amendment (target {item.get('amends')!r}, {len(reasons)} reasons)")
                continue
            out.append({"id": new_id, "kind": "amendment", "amends": target, "decision": str(item["decision"]).strip(),
                        "rationale": reasons, "confidence": conf, "source": source, "extracted_at": now_iso()})
            continue
        if sup:
            succ[sup] = new_id
        adj[new_id] = list(deps)
        out.append({
            "id": new_id,
            "decision": str(item["decision"]).strip(),
            "rationale": [str(x) for x in item.get("rationale") or []],
            "alternatives_rejected": [str(x) for x in item.get("alternatives_rejected") or []],
            "supersedes": sup,
            "depends_on": sorted(set(deps)),
            "restores": sorted(set(restores)),
            "topic": clean_topic(item.get("topic")),
            "decided_by": item.get("decided_by") if item.get("decided_by") in ("user", "assistant_accepted") else "unknown",
            "confidence": conf,
            "source": {
                "session_id": session_id,
                "turn_uuids": [t["uuid"] for t in src],
                "decided_at": src[0]["timestamp"] if src else None,
            },
            "extracted_at": now_iso(),
        })
    return out, counter


# -------------------------------------------------------------------- main
def run(payload: dict) -> int:
    raw = (payload.get("transcript_path") or "").strip()
    transcript = Path(raw).expanduser() if raw else None
    project = project_dir(payload)
    session_id = payload.get("session_id", "unknown")
    if transcript is None or not transcript.is_file():
        # Some hosts send no transcript (or a folder): nothing to read, and not a failure.
        return 0

    with Store(project) as store:
        state = store.state()
        cursor = state["cursors"].get(session_id, 0)
        turns, total_lines = load_turns(transcript, cursor)
        if not turns:
            store.log(f"session {session_id}: nothing new since line {cursor}")
            state["cursors"][session_id] = total_lines
            store.save_state(state)
            return 0

        records = store.records()
        counter = next_id_counter(records)
        added = 0
        for chunk in chunk_turns(turns):
            g = Graph(records)
            raw = parse_json_array(call_model(build_prompt(chunk, g.active(), g.replaced(), sorted(set(g.topics.values())))))
            new, counter = validate(raw, chunk, records, session_id, counter, store)
            store.append(new)
            records.extend(new)  # later chunks see earlier chunks' decisions
            added += len(new)

        # only advance the cursor after everything succeeded
        state["cursors"][session_id] = total_lines
        store.save_state(state)
        # the model sometimes leaves out the area; fill gaps with one small call, never failing the extraction
        untagged = [r for r in records[-added:] if added and not r.get("kind") and not r.get("topic")] if added else []
        if untagged:
            try:
                import dg_topics
                g = Graph(records)
                found = dg_topics.ask(untagged, sorted(set(g.topics.values())))
                if found:
                    n = sum(1 for r in records if r.get("kind") == "topics") + 1
                    store.append([{"id": f"T-{n}", "kind": "topics", "map": found, "at": now_iso()}])
            except Exception as e:
                store.log(f"areas for {len(untagged)} new decision(s) not set: {e}")
        store.event("extracted", session=session_id, trigger=payload.get("trigger", "?"), turns=len(turns), added=added)
        store.log(f"session {session_id} ({payload.get('trigger', '?')}): "
                  f"{len(turns)} turns from line {cursor}, +{added} decisions")
    return 0


def main() -> int:
    utf8_stdio()
    if os.environ.get(GUARD_ENV):
        return 0  # we're inside our own nested extraction call
    payload = {}
    try:
        payload = read_stdin_json()
        return run(payload)
    except Exception as e:  # never block compaction
        try:
            Store(project_dir(payload)).log(f"ERROR {type(e).__name__}: {e}")
            e = str(e).splitlines()[0]
        except Exception:
            pass
        print(f"decision-graph: extraction failed ({e}); see .claude/decisions/extract.log", file=sys.stderr)
        return 1  # non-blocking error: compaction proceeds


if __name__ == "__main__":
    sys.exit(main())
