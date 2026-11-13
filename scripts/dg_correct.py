#!/usr/bin/env python3
"""
Correct the decision graph by hand. Corrections are append-only records, so they can be undone.

  dg confirm D-3 [--note TEXT]     this decision is right (confidence becomes 100%)
  dg reject D-9 [--note TEXT]      this isn't a decision: hide it, drop its links
  dg edit D-3 "new wording"        fix how the decision is worded (the original is kept)
  dg corrections                   list every correction

Undo a rejection with `dg confirm <id>`.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dg_store import Graph, Store, now_iso, project_dir, utf8_stdio  # noqa: E402

ACTIONS = ("confirm", "reject", "edit")


def norm_id(rid: str) -> str:
    rid = rid.strip()
    return rid.upper() if re.fullmatch(r"[dD]-\d+", rid) else rid


def next_correction_id(records: list[dict]) -> str:
    nums = [int(m.group(1)) for r in records if (m := re.fullmatch(r"C-(\d+)", str(r.get("id", ""))))]
    return f"C-{max(nums, default=0) + 1}"


def apply_correction(project: Path, target: str, action: str, text: str | None = None,
                     note: str | None = None) -> tuple[bool, str, dict | None]:
    """Validate and append one correction. Returns (ok, message, record)."""
    if action not in ACTIONS:
        return False, f"unknown action {action!r}", None
    target = norm_id(target)
    with Store(project) as store:
        records = store.records()
        g = Graph(records)
        raw = next((r for r in records if r.get("id") == target and not r.get("kind")), None)
        if raw is None:
            return False, f"no decision {target!r}", None
        is_rejected = target in g.rejected
        current = (g.rejected.get(target) or g.by_id.get(target) or raw)
        if action == "reject" and is_rejected:
            return False, f"{target} is already rejected (undo with: dg confirm {target})", None
        if action == "edit":
            text = (text or "").strip()
            if not text:
                return False, "edit needs the new wording", None
            if is_rejected:
                return False, f"{target} is rejected; confirm it first (dg confirm {target})", None
            if text == current["decision"]:
                return False, "that's already the wording", None
        rec = {"id": next_correction_id(records), "kind": "correction", "target": target, "action": action,
               "target_text": raw["decision"], "by": "user", "at": now_iso()}
        if action == "edit":
            rec["text"] = text
        if note:
            rec["note"] = note.strip()
        store.append([rec])
    if action == "confirm":
        msg = f"{target} restored and confirmed" if is_rejected else f"{target} confirmed"
    elif action == "reject":
        dependents = [d for d in g.dependents.get(target, []) if d in g.by_id]
        replaced = raw.get("supersedes")
        msg = f"{target} rejected: hidden from the graph and its links dropped"
        if replaced and replaced in g.by_id:
            msg += f"; {replaced}, which it replaced, is in force again"
        if dependents:
            msg += f"; {', '.join(dependents)} no longer rest on it"
    else:
        msg = f"{target} reworded (original kept: \"{raw['decision']}\")"
    return True, msg + f" ({rec['id']})", rec


def list_corrections(project: Path) -> int:
    records = Store(project).records()
    cs = [r for r in records if r.get("kind") == "correction"]
    if not cs:
        print("No corrections yet. Use dg confirm / dg reject / dg edit, or the buttons in dg view.")
        return 0
    for c in cs:
        what = {"confirm": "confirmed", "reject": "rejected", "edit": "reworded"}.get(c.get("action"), c.get("action"))
        line = f"{c['id']}  {c.get('at', '')[:16].replace('T', ' ')}  {what} {c.get('target')}: {c.get('target_text', '')}"
        if c.get("action") == "edit":
            line += f"\n      -> {c.get('text')}"
        if c.get("note"):
            line += f"\n      note: {c['note']}"
        print(line)
    return 0


def main(argv=None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="dg", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=ACTIONS + ("corrections",))
    ap.add_argument("target", nargs="?")
    ap.add_argument("text", nargs="*")
    ap.add_argument("--note")
    ap.add_argument("--project", type=Path)
    a = ap.parse_args(argv)
    project = (a.project or project_dir()).resolve()
    if a.action == "corrections":
        return list_corrections(project)
    if not a.target:
        print(f"usage: dg {a.action} D-<n>" + (' "new wording"' if a.action == "edit" else ""))
        return 2
    ok, msg, _ = apply_correction(project, a.target, a.action, " ".join(a.text) if a.text else None, a.note)
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
