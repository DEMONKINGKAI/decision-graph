#!/usr/bin/env python3
"""
Query the project's decision graph.

usage: query_decisions.py [--project DIR] <command> [args]

  active               decisions currently in force (default)
  show <id>            one decision in full, with its links
  why <id>             the chain of decisions it rests on
  impact <id>          everything that rests on it ("what breaks if we undo this?")
  history <id>         every version of this decision, oldest first
  stale                active decisions whose justification has been superseded
  search <text...>     substring search over decisions, rationale and alternatives
  mermaid              the active graph as a Mermaid diagram
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dg_store import Graph, Store, id_num, project_dir, utf8_stdio  # noqa: E402


def one_line(r: dict) -> str:
    if r.get("kind") == "amendment":
        return f"{r['id']} (adds to {r['amends']}): {'; '.join(r.get('rationale') or [])}"
    s = f"{r['id']}: {r['decision']}"
    if r.get("confirmed"):
        s += "  [confirmed]"
    if r.get("depends_on"):
        s += f"  [rests on {', '.join(r['depends_on'])}]"
    if r.get("supersedes"):
        s += f"  [replaces {r['supersedes']}]"
    if r.get("restores"):
        s += f"  [restores {', '.join(r['restores'])}]"
    return s


def warnings(g: Graph, rid: str) -> list[str]:
    out = [f"rests on {d}, which was replaced by {now}" for d, now in g.stale_deps(rid)]
    out += [f"indirectly affected: {' -> '.join(path)}, and {anc} rests on a replaced decision"
            for anc, path in g.indirectly_stale(rid)]
    return out


def full(r: dict, g: Graph) -> str:
    lines = [one_line(r)]
    status = "ACTIVE" if g.is_active(r["id"]) else f"SUPERSEDED by {g.superseded_by[r['id']]} (now in force: {g.current(r['id'])})"
    if r.get("confirmed"):
        status += " (confirmed by you)"
    lines.append(f"  status: {status}")
    if r.get("edited"):
        lines.append(f"  reworded by you; originally: {r['original_decision']}")
    for x, src in g.rationale(r["id"]):
        lines.append(f"  because: {x}" + (f"  (added later, {src})" if src != r["id"] else ""))
    for x in r.get("alternatives_rejected") or []:
        lines.append(f"  rejected: {x}")
    deps = g.dependents.get(r["id"], [])
    if deps:
        lines.append(f"  relied on by: {', '.join(deps)}")
    for w in warnings(g, r["id"]):
        lines.append(f"  WARNING: {w}")
    for d, now in g.restored_deps(r["id"]):
        lines.append(f"  note: rests on {d}, which was replaced but later restored by {now}, so it still holds")
    src = r.get("source") or {}
    lines.append(f"  decided by: {r.get('decided_by', '?')}, confidence {r.get('confidence', '?')}, "
                 f"at {src.get('decided_at') or '?'}, session {str(src.get('session_id', '?'))[:8]}, "
                 f"turns {', '.join(u[:8] for u in src.get('turn_uuids') or []) or '?'}")
    return "\n".join(lines)


def need(g: Graph, rid: str) -> dict:
    rid = rid.upper() if rid.lower().startswith("d-") else rid
    if rid in g.rejected:
        sys.exit(f"{rid} was rejected by you as not a decision: \"{g.rejected[rid]['decision']}\". "
                 f"Undo with: dg confirm {rid}")
    if rid not in g.by_id:
        sys.exit(f"no decision {rid!r}. Run `active` or `search` to find ids.")
    return g.by_id[rid]


def main(argv=None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--project", type=Path)
    ap.add_argument("command", nargs="?", default="active")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args(argv)

    store = Store(a.project or project_dir())
    records = store.records()
    if not records:
        print(f"No decisions recorded yet in {store.records_path}. "
              "They are extracted automatically when the conversation is compacted (/compact).")
        return 0
    g = Graph(records)
    cmd = a.command

    if cmd == "active":
        act = sorted(g.active(), key=lambda r: id_num(r["id"]))
        print(f"{len(act)} active decisions ({len(g.by_id)} recorded in total):")
        for r in act:
            warn = "".join(f"  (WARNING: {w})" for w in warnings(g, r["id"]))
            print(" - " + one_line(r) + warn)
    elif cmd == "show":
        print(full(need(g, a.args[0]), g))
    elif cmd == "why":
        r = need(g, a.args[0])
        print(full(r, g))
        chain = g.why(r["id"])
        print("\nRests on:" if chain else "\nRests on no other recorded decision.")
        for depth, d in chain:
            dr = g.by_id.get(d)
            mark = "" if g.is_active(d) else f"  (SUPERSEDED by {g.current(d)})"
            reasons = [x for x, _ in g.rationale(d)]
            reason = f" -- because: {'; '.join(reasons)}" if reasons else ""
            if not g.is_active(d) and g.holds(d):
                mark = f"  (replaced, then RESTORED by {g.current(d)})"
            print("  " * (depth + 1) + (one_line(dr) if dr else d) + mark + reason)
    elif cmd == "impact":
        r = need(g, a.args[0])
        hits = g.impact(r["id"])
        if not hits:
            print(f"Nothing recorded rests on {r['id']}. Undoing it has no known downstream effect.")
        else:
            print(f"Undoing {r['id']} ({r['decision']}) would remove the justification for:")
            for dist, d in hits:
                tag = "directly" if dist == 1 else f"{dist} steps away"
                state = "" if g.is_active(d) else " (already superseded)"
                print(f"  - [{tag}] {one_line(g.by_id[d])}{state}")
    elif cmd == "history":
        r = need(g, a.args[0])
        for i, rid in enumerate(g.history(r["id"])):
            rr = g.by_id[rid]
            when = (rr.get("source") or {}).get("decided_at") or rr.get("extracted_at", "?")
            print(f"{i + 1}. {rid} ({when}): {rr['decision']}" + ("  <- in force" if g.is_active(rid) else ""))
    elif cmd == "stale":
        direct = [(r, g.stale_deps(r["id"])) for r in g.active()]
        direct = [(r, x) for r, x in direct if x]
        indirect = [(r, g.indirectly_stale(r["id"])) for r in g.active()]
        indirect = [(r, x) for r, x in indirect if x]
        restored = [(r, g.restored_deps(r["id"])) for r in g.active()]
        restored = [(r, x) for r, x in restored if x]
        if not direct and not indirect:
            print("No active decision rests on a replaced one.")
        if direct:
            print("Directly affected (a reason was replaced):")
        for r, x in direct:
            print("  " + one_line(r))
            for d, now in x:
                print(f"    rests on {d} ({g.by_id[d]['decision']}), replaced by {now} ({g.by_id[now]['decision']})")
        if indirect:
            print("Indirectly affected (something further up the chain lost its reason):")
        for r, x in indirect:
            print("  " + one_line(r))
            for anc, path in x:
                print(f"    via {' -> '.join(path)}")
        if restored:
            print("Resolved (a reason was replaced and later restored, so these hold):")
        for r, x in restored:
            print("  " + one_line(r))
            for d, now in x:
                print(f"    rests on {d}, restored by {now}")
    elif cmd == "search":
        q = " ".join(a.args).lower()
        if not q:
            sys.exit("search needs text")
        pool = list(g.by_id.values()) + [a for lst in g.amendments.values() for a in lst]
        hits = [r for r in pool if q in " ".join(
            [r.get("decision", "")] + (r.get("rationale") or []) + (r.get("alternatives_rejected") or [])).lower()]
        for r in sorted(hits, key=lambda r: (r.get("kind") == "amendment", id_num(r["id"]), r["id"])):
            state = "" if r.get("kind") == "amendment" or g.is_active(r["id"]) else f"  (SUPERSEDED by {g.current(r['id'])})"
            print(one_line(r) + state)
        if not hits:
            print("No matches.")
    elif cmd == "mermaid":
        print("graph TD")
        for r in g.active():
            label = r["decision"].replace('"', "'")
            label = label if len(label) <= 60 else label[:57] + "..."
            print(f'  {r["id"].replace("-", "")}["{r["id"]}: {label}"]')
        for r in g.active():
            for d in r.get("depends_on") or []:
                style = "-->" if g.holds(d) else "-.->|superseded|"
                print(f'  {d.replace("-", "")} {style} {r["id"].replace("-", "")}')
        for r in g.active():
            for x in r.get("restores") or []:
                print(f'  {r["id"].replace("-", "")} -.->|restores| {x.replace("-", "")}')
    else:
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
