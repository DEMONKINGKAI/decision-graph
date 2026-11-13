#!/usr/bin/env python3
"""
dg report: is the decision graph earning its place in this project?

Answers the three questions worth tracking during a trial:
  1. Did warnings fire, and on what? (You judge whether each one caught something real.)
  2. How often was a recorded decision wrong? (Measured by your corrections: rejections and rewordings.)
  3. Do you actually look at the graph? (How often `dg view` was opened.)

usage: dg report [--days 14] [--project DIR]
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dg_store import Graph, Store, project_dir, utf8_stdio  # noqa: E402


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "n/a"


def main(argv=None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--project", type=Path)
    a = ap.parse_args(argv)
    project = (a.project or project_dir()).resolve()
    store = Store(project)
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)).isoformat(timespec="seconds")
    events = [e for e in store.events() if e.get("at", "") >= since]
    records = store.records()
    g = Graph(records)

    print(f"Decision graph report: {project.name}, last {a.days} days\n")
    if not records and not events:
        print("Nothing yet. Decisions are recorded when a Claude Code conversation in this project is compacted.")
        return 0

    # recording
    ext = [e for e in events if e["kind"] == "extracted"]
    recorded = [r for r in records if not r.get("kind") and (r.get("extracted_at") or "") >= since]
    print("Recording")
    print(f"  {len(ext)} compaction(s) processed, {len(recorded)} decision(s) recorded; "
          f"{len(g.active())} in force now, {sum(1 for r in g.active() if g.stale_deps(r['id']))} stale.")

    # 1. warnings
    guard = [e for e in events if e["kind"] == "guard"]
    paused = [e for e in guard if e.get("paused")]
    reminders = [e for e in guard if not e.get("paused") and e.get("where") == "tool"]
    prompt_notes = [e for e in guard if e.get("where") == "prompt"]
    print("\n1. Warnings")
    if not guard:
        print("  None fired. That's fine if nothing contradicted a decision; if something did, the decision may be missing\n"
              "  its rejected alternatives (check with `dg show <id>`).")
    else:
        print(f"  {len(paused)} time(s) a tool call was paused for you to approve, {len(reminders)} reminder(s) to Claude "
              f"after that, and {len(prompt_notes)} note(s) on your own messages.")
        top = Counter((e.get("decision"), e.get("term")) for e in guard).most_common(5)
        print("  Most frequent: " + "; ".join(f"{d} ({t}) x{n}" for (d, t), n in top))
        print("  Recent pauses (did each one catch something real?):")
        for e in paused[-5:]:
            print(f"    {e['at'][:16].replace('T', ' ')}  {e.get('tool', '')}: \"{e.get('term')}\" vs {e.get('decision')}")

    # 2. wrong records
    corr = [r for r in records if r.get("kind") == "correction" and (r.get("at") or "") >= since]
    rej = [c for c in corr if c.get("action") == "reject"]
    edits = [c for c in corr if c.get("action") == "edit"]
    conf = [c for c in corr if c.get("action") == "confirm"]
    base = max(len(recorded), 1)
    print("\n2. Wrong records")
    print(f"  {len(rej)} rejected as not a decision ({pct(len(rej), base)} of those recorded), {len(edits)} reworded, "
          f"{len(conf)} confirmed as right.")
    if rej:
        print("  Rejected: " + "; ".join(f"{c['target']} \"{c.get('target_text', '')[:50]}\"" for c in rej[-5:]))
    if not corr:
        print("  No corrections yet. Skimming `dg view` now and then and clicking \"Not a decision\" on anything wrong is\n"
              "  what makes this number meaningful.")

    # 3. looking at it
    views = [e for e in events if e["kind"] == "view"]
    days = sorted({e["at"][:10] for e in views})
    print("\n3. Looking at the graph")
    print(f"  `dg view` opened {len(views)} time(s), on {len(days)} different day(s)." if views else
          "  `dg view` wasn't opened. The session-start status line and the warnings still work without it.")

    print("\nRule of thumb after two weeks: keep it if warnings caught at least one real mistake and fewer than about\n"
          "1 in 5 recorded decisions needed rejecting. If rejections are higher, send the rejected ones to whoever\n"
          "tunes the extraction prompt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
