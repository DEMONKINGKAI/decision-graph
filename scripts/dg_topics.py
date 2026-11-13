#!/usr/bin/env python3
"""
dg topics: group this project's decisions into areas (narrator, storage, frontend style, ...).

usage: dg topics [--redo] [--project DIR]

New decisions get their area when they're recorded. This tags the ones recorded before areas existed, in one
model call per 80 decisions, without re-extracting anything. --redo re-tags every decision in force.
Without arguments after tagging, it lists the areas.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dg_store import Graph, Store, clean_topic, id_num, now_iso, project_dir, utf8_stdio  # noqa: E402

PROMPT = """Group a software project's recorded decisions into areas of the project.

Give every decision below exactly one area: 1 to 3 lowercase words naming the part of the project it is about,
such as "narrator", "storage", "frontend style", "testing", "deployment", "game rules". Aim for roughly 5 to 12
areas in total, so each area holds several decisions; don't make an area for a single decision unless it fits
nowhere else. Reuse these existing areas when they fit: {areas}

DECISIONS:
{decisions}

Return ONLY a JSON object mapping each decision id to its area, no prose, no code fences, e.g.
{{"D-1": "narrator", "D-2": "storage"}}"""


def ask(decisions: list[dict], areas: list[str]) -> dict[str, str]:
    import extract_decisions
    text = extract_decisions.call_model(PROMPT.format(
        areas=", ".join(areas) or "(none yet)",
        decisions="\n".join(f"{r['id']}: {r['decision']}" for r in decisions)))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("the model didn't return a JSON object")
    raw = json.loads(m.group(0))
    ids = {r["id"] for r in decisions}
    return {k: t for k, v in raw.items() if k in ids and (t := clean_topic(v))}


def main(argv=None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path)
    ap.add_argument("--redo", action="store_true", help="re-tag every decision in force, not only untagged ones")
    a = ap.parse_args(argv)
    project = (a.project or project_dir()).resolve()
    store = Store(project)
    g = Graph(store.records())
    todo = [r for r in sorted(g.by_id.values(), key=lambda r: id_num(r["id"]))
            if a.redo or r["id"] not in g.topics]
    if not g.by_id:
        print("No decisions recorded yet.")
        return 0
    if todo:
        areas = [] if a.redo else sorted(set(g.topics.values()))
        print(f"Tagging {len(todo)} decision(s) with areas ...", flush=True)
        found: dict[str, str] = {}
        for i in range(0, len(todo), 80):
            found.update(ask(todo[i:i + 80], sorted(set(areas) | set(found.values()))))
        if not found:
            print("The model returned no areas; nothing changed.")
            return 1
        with store:
            n = sum(1 for r in store.records() if r.get("kind") == "topics") + 1
            store.append([{"id": f"T-{n}", "kind": "topics", "map": found, "at": now_iso()}])
        g = Graph(store.records())
    counts = Counter(g.topics[r["id"]] for r in g.active() if r["id"] in g.topics)
    untagged = sum(1 for r in g.active() if r["id"] not in g.topics)
    print(f"{len(counts)} areas:")
    for t, c in counts.most_common():
        print(f"  {t}: {c}")
    if untagged:
        print(f"  (no area: {untagged})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
