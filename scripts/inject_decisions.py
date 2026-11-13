#!/usr/bin/env python3
"""
SessionStart hook. Two audiences:

  Claude  gets the decisions currently in force (additionalContext).
  You     get one short status line in the session (systemMessage): how many decisions are in force,
          which are stale, what's new since your last session, and that `dg view` shows the graph.
          In a project with no decisions yet, you're told once that recording is on.

Fires on startup, resume, clear and compact.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dg_store import GUARD_ENV, Graph, Store, id_num, now_iso, project_dir, read_stdin_json, utf8_stdio  # noqa: E402

BUDGET = int(os.environ.get("DECISION_GRAPH_INJECT_CHARS", "6000"))  # stay well under the 10k cap


def last_failure(store: Store) -> str | None:
    """A factual notice when the most recent extraction failed, so it doesn't go unnoticed."""
    if not store.log_path.exists():
        return None
    lines = store.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    err = next((l for l in reversed(lines) if " ERROR " in l), None)
    ok = next((l for l in reversed(lines) if re.search(r"\+\d+ decisions|nothing new", l)), None)
    if not err or (ok and ok[:25] >= err[:25]):
        return None
    reason = err[26:].replace("ERROR ", "", 1).split("\n")[0][:160]
    return (f"Decision-graph notice for the user: the last automatic decision extraction failed "
            f"({err[:16].replace('T', ' ')} UTC: {reason}), so decisions from that conversation are not recorded yet. "
            f"Running `dg doctor` in a terminal shows the cause and the fix; failed parts are retried at the next /compact.")


def context_text(g: Graph, failure: str | None) -> str:
    active = sorted(g.active(), key=lambda r: id_num(r["id"]), reverse=True)  # newest first under budget
    header = (f"This project has a decision graph with {len(active)} decisions currently in force "
              f"(recorded from earlier conversations, which may no longer be in context). "
              f"They are listed below. The decisions skill can show the reasons behind any of them, "
              f"what depends on them, and their earlier versions.")
    body, used, omitted = [], len(header), 0
    for r in active:
        line = f"- {r['id']}: {r['decision']}" + (" (confirmed by the user)" if r.get("confirmed") else "")
        if r.get("depends_on"):
            line += f" (rests on {', '.join(r['depends_on'])})"
        for d, now in g.stale_deps(r["id"]):
            line += f" [note: {d} was later replaced by {now}, so this may need revisiting]"
        for anc, _ in g.indirectly_stale(r["id"]):
            line += f" [note: indirectly affected, because {anc} rests on a replaced decision]"
        if used + len(line) + 1 > BUDGET:
            omitted += 1
            continue
        body.append(line)
        used += len(line) + 1
    body.sort(key=lambda l: id_num(l[2:].split(":")[0]))
    out = [header] + body
    if omitted:
        out.append(f"({omitted} older active decisions are not listed here; the decisions skill can list all of them.)")
    if failure:
        out.append(failure)
    return "\n".join(out)


def status_line(g: Graph, new_ids: list[str], source: str) -> str:
    """One line for the user. Plain words, no jargon."""
    active = g.active()
    stale = [r["id"] for r in active if g.stale_deps(r["id"])]
    parts = [f"{len(active)} decision{'s' if len(active) != 1 else ''} in force"]
    if stale:
        parts.append(f"{len(stale)} stale ({', '.join(stale[:3])}{', ...' if len(stale) > 3 else ''})")
    if new_ids:
        what = "recorded from that conversation" if source == "compact" else "new since your last session"
        parts.append(f"{len(new_ids)} {what} ({', '.join(new_ids[:4])}{', ...' if len(new_ids) > 4 else ''})")
    return "Decision graph: " + ", ".join(parts) + ". Run `dg view` to explore."


def main() -> int:
    utf8_stdio()
    if os.environ.get(GUARD_ENV):
        return 0
    try:
        payload = read_stdin_json()
    except Exception:
        payload = {}
    first = None
    try:   # first session on this computer: set up `dg` and the background service for the user
        import dg_firstrun
        first = dg_firstrun.maybe_run()
        if not first:
            from dg_store import find_claude
            find_claude()    # remember where `claude` is, for `dg` in a terminal
    except Exception:
        pass
    try:   # the background service: know this project, and come back if it stopped (never slows the session)
        import dg_service
        if (project_dir(payload) / ".claude" / "decisions").is_dir():
            dg_service.register(project_dir(payload))
        dg_service.ensure_running()
    except Exception:
        pass
    try:
        store = Store(project_dir(payload))
        failure = last_failure(store)
        records = store.records()
        out: dict = {}
        with store:
            state = store.state()
            if not records:
                if failure:
                    out["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": failure}
                    out["systemMessage"] = "Decision graph: the last extraction failed, so nothing is recorded yet. Run `dg doctor` to see why."
                elif not state.get("announced"):
                    out["systemMessage"] = ("Decision graph is on for this project. Decisions you make with Claude are "
                                            "recorded when the conversation is compacted; `dg view` will show them.")
                    state["announced"] = True
                    store.save_state(state)
            else:
                g = Graph(records)
                since = state.get("last_status_at")      # None on the very first status: nothing counts as new yet
                new_ids = [r["id"] for r in g.active() if (r.get("extracted_at") or "") > since] if since else []
                out["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": context_text(g, failure)}
                msg = status_line(g, new_ids, payload.get("source", ""))
                if failure:
                    msg += " The last extraction failed; run `dg doctor`."
                out["systemMessage"] = msg
                state["last_status_at"] = now_iso()
                state["announced"] = True
                store.save_state(state)
        if first:
            out["systemMessage"] = first + ("\n" + out["systemMessage"] if out.get("systemMessage") else "")
        if out:
            print(json.dumps(out, ensure_ascii=False))
    except Exception as e:
        if first:
            print(json.dumps({"systemMessage": first}, ensure_ascii=False))
        print(f"decision-graph: could not load decisions ({e})", file=sys.stderr)
        return 1  # non-blocking
    return 0


if __name__ == "__main__":
    sys.exit(main())
