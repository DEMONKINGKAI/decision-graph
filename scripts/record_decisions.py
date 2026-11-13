#!/usr/bin/env python3
"""
Record decisions now, without waiting for a compaction.

usage:
  dg record                      catch up every conversation in this project (only turns not yet read)
  record_decisions.py --session-end   SessionEnd hook: hands the work to a background process and returns at once

Each conversation keeps a cursor, so whichever of compaction, session end or `dg record` runs first does the
work, and the others only read what came after it.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dg_store import GUARD_ENV, Graph, Store, project_dir, read_stdin_json, utf8_stdio  # noqa: E402


def pending(project: Path) -> list[tuple[Path, int]]:
    """Conversations of this project with turns not yet read, oldest first, with how many turns are new."""
    import extract_decisions
    from rebuild_decisions import transcripts_dir
    d = transcripts_dir(project)
    if not d.is_dir():
        return []
    cursors = Store(project).state().get("cursors", {})
    out = []
    for f in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
        turns, _ = extract_decisions.load_turns(f, cursors.get(f.stem, 0))
        if turns:
            out.append((f, len(turns)))
    return out


def catch_up(project: Path, quiet: bool = False) -> int:
    import extract_decisions
    os.environ["CLAUDE_PROJECT_DIR"] = str(project)
    todo = pending(project)
    if not todo:
        if not quiet:
            print("Nothing new to record: every conversation in this project has been read.")
        return 0
    before = {r["id"] for r in Store(project).records()}
    for i, (f, n) in enumerate(todo, 1):
        if not quiet:
            print(f"[{i}/{len(todo)}] {f.stem[:8]}: {n} new turns ...", flush=True)
        extract_decisions.run({"session_id": f.stem, "transcript_path": str(f), "trigger": "record",
                               "cwd": str(project)})
    records = Store(project).records()
    g = Graph(records)
    new = [r for r in records if r["id"] not in before and not r.get("kind")]
    if not quiet:
        if new:
            print(f"Recorded {len(new)} decision(s):")
            for r in new:
                print(f"  {r['id']}: {r['decision']}" + (f"  (replaces {r['supersedes']})" if r.get("supersedes") else ""))
        else:
            print("No new decisions in those turns.")
        print(f"{len(g.active())} decisions in force.")
    return 0


def detach(args: list[str], env: dict) -> None:
    """Start a process that outlives this hook (and Claude Code exiting)."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, close_fds=True)
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(args, creationflags=flags | 0x01000000, **kw)   # | CREATE_BREAKAWAY_FROM_JOB
        except OSError:
            subprocess.Popen(args, creationflags=flags, **kw)                 # the job doesn't allow breaking away
    else:
        subprocess.Popen(args, start_new_session=True, **kw)


def on_session_end() -> int:
    if os.environ.get(GUARD_ENV):
        return 0
    payload = read_stdin_json()
    if not payload.get("transcript_path"):
        return 0
    project = project_dir(payload)
    payload["trigger"] = "session end"
    fd, path = tempfile.mkstemp(prefix="dg-session-end-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project))
    detach([sys.executable, str(Path(__file__).resolve()), "--from-payload", path], env)
    return 0


def from_payload(path: str) -> int:
    """The background half of --session-end: extract the rest of that conversation."""
    import extract_decisions
    p = Path(path)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    finally:
        try:
            p.unlink()
        except OSError:
            pass
    try:
        return extract_decisions.run(payload)
    except Exception as e:
        try:
            Store(project_dir(payload)).log(f"ERROR (session end) {type(e).__name__}: {e}")
        except Exception:
            pass
        return 1


def main(argv=None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path)
    ap.add_argument("--session-end", action="store_true")
    ap.add_argument("--from-payload")
    a = ap.parse_args(argv)
    if a.session_end:
        try:
            return on_session_end()
        except Exception:
            return 0   # never get in the way of a session ending
    if a.from_payload:
        return from_payload(a.from_payload)
    return catch_up((a.project or project_dir()).resolve())


if __name__ == "__main__":
    sys.exit(main())
