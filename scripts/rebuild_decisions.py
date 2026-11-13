#!/usr/bin/env python3
"""
Rebuild a project's decision graph from all of its saved Claude Code transcripts,
using the current extraction prompt. Use after upgrading the plugin, or to repair a graph.

usage: python rebuild_decisions.py [--project DIR] [--yes] [--resume]

The existing store is MOVED aside (to .claude/decisions.bak-<timestamp>), never deleted.
Transcripts are read from <claude config dir>/projects/<project slug>/*.jsonl, oldest first.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dg_store import Graph, Store, project_dir, utf8_stdio  # noqa: E402
import extract_decisions  # noqa: E402


def transcripts_dir(project: Path) -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(project.resolve()))
    return base / "projects" / slug


def first_timestamp(path: Path) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                ts = json.loads(line).get("timestamp")
            except json.JSONDecodeError:
                continue
            if ts:
                return ts
    return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat()


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()


def reattach(project: Path, corrections: list[dict]) -> None:
    """Decision ids change on rebuild; corrections follow their decision by wording."""
    import difflib
    from dg_correct import next_correction_id
    with Store(project) as store:
        records = store.records()
        decisions = [r for r in records if not r.get("kind")]
        moved, lost = [], []
        for c in corrections:
            best, score = None, 0.0
            for d in decisions:
                s = difflib.SequenceMatcher(None, _norm(c.get("target_text")), _norm(d["decision"])).ratio()
                if s > score:
                    best, score = d, s
            if best and score >= 0.75:
                rec = dict(c, id=next_correction_id(records + moved), target=best["id"], target_text=best["decision"],
                           reattached_from=c.get("target"))
                moved.append(rec)
            else:
                lost.append(c)
        store.append(moved)
    print(f"Re-attached {len(moved)} correction(s).")
    for c in lost:
        print(f"  could not find \"{c.get('target_text')}\" again; its {c.get('action')} was not carried over")


def main(argv=None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path)
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--resume", action="store_true", help="continue an interrupted rebuild (keeps the current graph)")
    a = ap.parse_args(argv)
    project = (a.project or project_dir()).resolve()
    tdir = transcripts_dir(project)
    files = sorted(tdir.glob("*.jsonl"), key=first_timestamp) if tdir.is_dir() else []
    if not files:
        print(f"No transcripts found in {tdir}")
        return 1

    store_dir = project / ".claude" / "decisions"
    print(f"Project:     {project}")
    print(f"Transcripts: {len(files)} in {tdir}")
    if store_dir.exists():
        what = "will be continued" if a.resume else "will be moved aside, not deleted"
        print(f"Existing graph: {len(Store(project).records())} records ({what})")
    if not a.yes and input("Rebuild? [y/N] ").strip().lower() != "y":
        return 1

    old_corrections = [r for r in Store(project).records() if r.get("kind") == "correction"] if store_dir.exists() else []
    if old_corrections and not a.resume:
        print(f"Your {len(old_corrections)} correction(s) will be re-attached to the matching re-extracted decisions.")
    if store_dir.exists() and not a.resume:
        backup = store_dir.with_name("decisions.bak-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.move(str(store_dir), str(backup))
        print(f"Moved old graph to {backup}")

    os.environ["CLAUDE_PROJECT_DIR"] = str(project)
    for i, f in enumerate(files, 1):
        turns, _ = extract_decisions.load_turns(f, 0)
        if not turns:
            print(f"[{i}/{len(files)}] {f.name}: skipped (an earlier extraction call, or no conversation)")
            continue
        print(f"[{i}/{len(files)}] {f.name}: {len(turns)} turns ...", flush=True)
        try:
            extract_decisions.run({"session_id": f.stem, "transcript_path": str(f), "trigger": "rebuild"})
        except Exception as e:
            Store(project).log(f"ERROR during rebuild of {f.name}: {e}")
            print(f"Failed on {f.name}: {str(e).splitlines()[0]}\n"
                  f"Details in {store_dir / 'extract.log'}. Fix the cause, then run again with --resume.")
            return 1

    if old_corrections and not a.resume:
        reattach(project, old_corrections)
    g = Graph(Store(project).records())
    print(f"Done: {len(g.by_id)} decisions ({len(g.active())} active), "
          f"{sum(len(v) for v in g.amendments.values())} amendments.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
