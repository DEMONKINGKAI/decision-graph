#!/usr/bin/env python3
"""
dg add: record decisions that were extracted somewhere else (a Chat or Cowork conversation, via
/decision-graph:save), with the same checks as automatic recording.

usage:
  dg add --file decisions.json          read a JSON array from a file
  dg add --clipboard                    read it from the clipboard (copy the block Claude gave you first)
  dg add < decisions.json               read it from standard input
options: --project DIR, --session "chat: what it was about", --dry-run (show what would be added, change nothing)

Each item uses the extractor's format:
  {"key": "n1", "decision": "...", "rationale": [...], "alternatives_rejected": [...],
   "supersedes": "D-12" or "n1" or null, "depends_on": ["D-3", "n2"], "restores": [], "amends": null,
   "topic": "storage", "decided_by": "user" | "assistant_accepted", "confidence": 0.0-1.0}
The checks are the same as for compaction: no circular links, a replaced decision is replaced in its current
version, only replaced decisions can be restored, references to unknown ids are dropped.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dg_store import Graph, Store, now_iso, project_dir, utf8_stdio  # noqa: E402


def read_clipboard() -> str:
    cmds = ([["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"]] if os.name == "nt" else
            [["pbpaste"]] if sys.platform == "darwin" else
            [["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]])
    for c in cmds:
        if shutil.which(c[0]):
            p = subprocess.run(c, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if p.returncode == 0:
                return p.stdout
    raise RuntimeError("couldn't read the clipboard here. Save the block to a file and use --file instead.")


def main(argv=None) -> int:
    import extract_decisions as ex
    utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path)
    ap.add_argument("--file", type=Path)
    ap.add_argument("--clipboard", action="store_true")
    ap.add_argument("--session", default="chat")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    project = (a.project or project_dir()).resolve()

    try:
        if a.file:
            text = a.file.read_text(encoding="utf-8")
        elif a.clipboard:
            text = read_clipboard()
        elif not sys.stdin.isatty():
            text = sys.stdin.read()
        else:
            print("Give the decisions with --file FILE or --clipboard, or pipe them in. See `dg add --help`.")
            return 2
        raw = ex.parse_json_array(text)
    except Exception as e:
        print(f"Couldn't read the decisions: {e}")
        return 1
    if not raw:
        print("The list is empty; nothing to add.")
        return 0

    store = Store(project)
    session = ("chat: " + a.session) if not a.session.startswith("chat") else a.session
    with store:
        records = store.records()
        before_log = store.log_path.stat().st_size if store.log_path.exists() else 0
        counter = ex.next_id_counter(records)
        new, _ = ex.validate([dict(x) if isinstance(x, dict) else x for x in raw], [], records, session, counter, store)
        notes = []
        if store.log_path.exists():
            with open(store.log_path, encoding="utf-8", errors="replace") as f:
                f.seek(before_log)
                notes = [l.split(" ", 1)[1].strip() for l in f if " " in l]
        stamp = now_iso()
        for r in new:
            r.setdefault("source", {})["decided_at"] = stamp
            r["source"]["session_id"] = session
            if not r.get("kind"):
                r["recorded_from"] = "chat"
        if a.dry_run:
            if store.log_path.exists():          # a dry run leaves no trace, not even in the log
                with open(store.log_path, "r+", encoding="utf-8") as f:
                    f.truncate(before_log)
        else:
            store.append(new)
            store.event("extracted", session=session, trigger="chat save", turns=0, added=len(new))

    g = Graph(records + ([] if a.dry_run else new)) if a.dry_run else Graph(Store(project).records())
    verb = "Would add" if a.dry_run else "Added"
    decisions = [r for r in new if not r.get("kind")]
    amends = [r for r in new if r.get("kind") == "amendment"]
    if not new:
        print("Nothing was added: every item was malformed or pointed at something that doesn't exist.")
    else:
        print(f"{verb} {len(decisions)} decision(s){f' and {len(amends)} added reason(s)' if amends else ''} to {project.name}:")
        for r in decisions:
            extra = []
            if r.get("supersedes"):
                extra.append(f"replaces {r['supersedes']}")
            if r.get("depends_on"):
                extra.append("rests on " + ", ".join(r["depends_on"]))
            if r.get("topic"):
                extra.append(f"area: {r['topic']}")
            print(f"  {r['id']}: {r['decision']}" + (f"  ({'; '.join(extra)})" if extra else ""))
        for r in amends:
            print(f"  {r['id']}: new reason for {r['amends']}: {'; '.join(r.get('rationale') or [])}")
    for n in notes:
        print(f"  note: {n}")
    if not a.dry_run and new:
        stale = [r["id"] for r in g.active() if g.stale_deps(r["id"])]
        print(f"{len(g.active())} decisions in force" + (f"; stale now: {', '.join(stale)}" if stale else "") + ".")
        print("Undo any of them with `dg reject <id>`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
