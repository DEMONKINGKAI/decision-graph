#!/usr/bin/env python3
"""
Measure decision extraction against conversations with known correct answers.

usage: python eval/run_eval.py [--runs 3] [--cases NAME] [--jobs 4] [--model haiku] [--label TEXT]

Each case in eval/cases.json is a conversation plus its gold answer: the decisions that should be
found, traps that must NOT become decisions, links (rests on / replaces / restores), amendments,
rejected alternatives, and who decided. The real extractor runs on every case several times
(the model isn't deterministic), and the results are scored.

Output: eval/results/<timestamp>/report.md and summary.json, plus every extracted record under work/.
eval/results/latest.md always holds the most recent report.

Testing the scorer itself: --mock-dir DIR makes the extractor return DIR/<case>.json instead of calling a model.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EXTRACT = ROOT / "scripts" / "extract_decisions.py"
NEGATION = re.compile(r"\b(no|not|without|don't|dont|won't|wont|never|skip|skipped|avoid|drop|dropped|instead of|"
                      r"rather than|rejected|kein|keine|keinen|nicht|ohne|statt|anstatt)\b", re.I)


# ----------------------------------------------------------------- transcripts
def build_transcript(turns: list[dict], path: Path) -> None:
    lines, t0 = [], dt.datetime(2026, 11, 1, 10, 0, 0, tzinfo=dt.timezone.utc)
    for i, t in enumerate(turns):
        ts = (t0 + dt.timedelta(seconds=30 * i)).isoformat().replace("+00:00", "Z")
        if "tool" in t:
            arg = t["tool"]["arg"]
            key = "command" if t["tool"]["name"] == "Bash" else "file_path"
            content = [{"type": "tool_use", "id": f"tu{i}", "name": t["tool"]["name"], "input": {key: arg}}]
            role = "assistant"
        elif "tool_result" in t:
            content = [{"type": "tool_result", "tool_use_id": f"tu{i - 1}", "content": t["tool_result"]}]
            role = "user"
        else:
            role = t["role"]
            content = t["text"] if role == "user" else [{"type": "text", "text": t["text"]}]
        lines.append(json.dumps({"type": role, "uuid": f"turn-{i:03d}", "timestamp": ts,
                                 "message": {"role": role, "content": content}}, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------- matching
def has_term(text: str, term: str) -> bool:
    """Whole-word match, allowing a plural ending ("magic link" matches "magic links")."""
    return re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?:s|es)?(?![a-z0-9])", text.lower()) is not None


def acceptable_negation(text: str, gold: dict) -> bool:
    """A decision NOT to do a trap thing ("no Redis for now") is fine where the gold allows it: neither right nor wrong."""
    return any(t.get("allow_negated") and any(has_term(text, x) for x in t["terms"]) and NEGATION.search(text)
               for t in gold.get("traps", []))


def matches(text: str, groups: list[list[str]]) -> bool:
    """Every group must match; a group matches if any of its alternatives occurs as a whole word."""
    return all(any(has_term(text, alt) for alt in g) for g in groups)


def max_matching(preds: list[dict], golds: list[dict]) -> dict[str, str]:
    """gold id -> predicted id, maximising the number of pairs (augmenting paths)."""
    cand = {g["id"]: [p["id"] for p in preds if matches(p["decision"], g["match"])] for g in golds}
    owner: dict[str, str] = {}

    def augment(gid, seen):
        for pid in cand[gid]:
            if pid in seen:
                continue
            seen.add(pid)
            if pid not in owner or augment(owner[pid], seen):
                owner[pid] = gid
                return True
        return False

    for g in golds:
        augment(g["id"], set())
    return {gid: pid for pid, gid in owner.items()}


# ----------------------------------------------------------------- reasons
STOP = set("""this that with from have will would should could they them their there what when where which while
about into onto over under than then also just only very more most much many some such been being were your yours
because since using used make makes made does done like need needs want wants keep keeps user users decision decided
project app the and for are but not you our its it's""".split())


def stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-zA-ZäöüÄÖÜß]{4,}", text.lower()) if w not in STOP}


def case_text(case: dict) -> str:
    parts = []
    for t in case["turns"]:
        parts.append(t.get("text") or t.get("tool_result") or (t.get("tool") or {}).get("arg", ""))
    parts += [r["decision"] + " " + " ".join(r.get("rationale") or []) for r in case["existing"]]
    return "\n".join(parts)


def reason_supported(reason: str, gold_alts, said: set[str]) -> bool:
    """A recorded reason is supported if it matches the gold reason or most of its words were actually said."""
    if gold_alts and any(a.lower() in reason.lower() for a in gold_alts):
        return True
    words = stems(reason)
    return bool(words) and len(words & said) / len(words) >= 0.6


# ----------------------------------------------------------------- scoring
def score_case(case: dict, records: list[dict]) -> dict:
    gold = case["gold"]
    existing = {r["id"] for r in case["existing"]}
    new = [r for r in records if r["id"] not in existing]
    preds = [r for r in new if r.get("kind") != "amendment"]
    amends = [r for r in new if r.get("kind") == "amendment"]
    by_id = {r["id"]: r for r in new}
    required = [g for g in gold["decisions"] if not g.get("optional")]
    optional = [g for g in gold["decisions"] if g.get("optional")]
    m = max_matching(preds, required)                       # required answers get first pick
    rest = [p for p in preds if p["id"] not in set(m.values())]
    m_opt = max_matching(rest, optional)                    # optional answers: fine if present, fine if absent
    matched_preds = set(m.values()) | set(m_opt.values())
    # a decision NOT to do a trap thing is neutral, but only if it isn't a real answer itself
    neutral = [r for r in preds if r["id"] not in matched_preds and acceptable_negation(r["decision"], case["gold"])]
    preds = [r for r in preds if r not in neutral]
    m.update(m_opt)
    res = {"found": {}, "decided_by": {}, "extra": [], "neutral": [r["decision"] for r in neutral], "traps": [], "links": {}, "forbidden": [],
           "amendments": {}, "duplicates": [], "rejected": {}, "extra_links": []}

    for g in required:
        res["found"][g["id"]] = m.get(g["id"])
    res["optional"] = {g["id"]: m.get(g["id"]) for g in optional}
    for g in gold["decisions"]:
        if g.get("decided_by") and m.get(g["id"]):
            res["decided_by"][g["id"]] = by_id[m[g["id"]]].get("decided_by") == g["decided_by"]
    for p in preds:
        if p["id"] not in matched_preds:
            res["extra"].append(p["decision"])
    # reasons: captured where one was given; none invented
    said = stems(case_text(case))
    gold_by_pred = {pid: g for g in gold["decisions"] for gid, pid in m.items() if gid == g["id"]}
    res["reasons_expected"], res["reasons_captured"], res["reasons_total"], res["reasons_unsupported"] = 0, 0, 0, []
    for g in gold["decisions"]:
        if g.get("reason") and m.get(g["id"]):
            res["reasons_expected"] += 1
            rat = " ".join(by_id[m[g["id"]]].get("rationale") or []).lower()
            res["reasons_captured"] += any(a.lower() in rat for a in g["reason"])
    for p in preds:
        g = gold_by_pred.get(p["id"])
        alts = (g or {}).get("reason") or []
        for reason in p.get("rationale") or []:
            res["reasons_total"] += 1
            if not reason_supported(reason, alts, said):
                res["reasons_unsupported"].append(f"{p['decision']}  <-  \"{reason}\"")
    # precision counts required matches and extras; optional matches are neutral
    opt_ids = set(m_opt.values())
    res["found_preds"] = [p["id"] for p in preds if p["id"] not in opt_ids]
    for trap in gold.get("traps", []):
        for p in preds:
            if any(has_term(p["decision"], t) for t in trap["terms"]):
                if trap.get("allow_negated") and NEGATION.search(p["decision"]):
                    continue
                res["traps"].append({"terms": trap["terms"], "why": trap["why"], "decision": p["decision"]})

    def rid(x):
        return x if x in existing else m.get(x)

    def has_link(kind, a, b):
        ra = by_id.get(a) if a else None
        if not ra or not b:
            return False
        if kind == "depends":
            return b in (ra.get("depends_on") or [])
        if kind == "supersedes":
            return ra.get("supersedes") == b
        if kind == "restores":
            return b in (ra.get("restores") or [])
        return False

    expected = set()
    for kind, a, b in gold.get("links", []):
        ok = has_link(kind, rid(a), rid(b))
        res["links"][f"{kind} {a}->{b}"] = ok
        if rid(a) and rid(b):
            expected.add((kind, rid(a), rid(b)))
    for kind, a, b in gold.get("forbidden_links", []):
        if has_link(kind, rid(a), rid(b)):
            res["forbidden"].append(f"{kind} {a}->{b}")
    for p in preds:  # links the model added that the gold doesn't have
        for d in p.get("depends_on") or []:
            if ("depends", p["id"], d) not in expected:
                res["extra_links"].append(f"{p['id']} rests on {d}")
        if p.get("supersedes") and ("supersedes", p["id"], p["supersedes"]) not in expected:
            res["extra_links"].append(f"{p['id']} replaces {p['supersedes']}")
        for x in p.get("restores") or []:
            if ("restores", p["id"], x) not in expected:
                res["extra_links"].append(f"{p['id']} restores {x}")

    for a in gold.get("amendments", []):
        res["amendments"][a["target"]] = any(
            x.get("amends") == a["target"] and any(has_term(" ".join(x.get("rationale") or []), t) for t in a["reason"])
            for x in amends)
    for nd in gold.get("no_duplicate", []):
        for p in preds:
            if any(has_term(p["decision"], t) for t in nd["match"]) and p.get("supersedes") != nd["of"]:
                res["duplicates"].append(f"{p['decision']}  (duplicates {nd['of']})")
    for rj in gold.get("rejected", []):
        pid = m.get(rj["of"])
        alts = " ".join(by_id[pid].get("alternatives_rejected") or []) if pid else ""
        res["rejected"][f"{rj['of']}: {', '.join(rj['match'])}"] = bool(pid) and any(has_term(alts, t) for t in rj["match"])
    return res


# ----------------------------------------------------------------- running
def run_one(case: dict, k: int, work: Path, args) -> dict:
    proj = work / f"{case['name']}-r{k}"
    if proj.exists():
        shutil.rmtree(proj)
    store = proj / ".claude" / "decisions"
    store.mkdir(parents=True)
    if case["existing"]:
        (store / "decisions.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in case["existing"]), encoding="utf-8")
    tr = proj / "transcript.jsonl"
    build_transcript(case["turns"], tr)
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(proj))
    env.pop("DECISION_GRAPH_EXTRACTING", None)
    if args.model:
        env["DECISION_GRAPH_MODEL"] = args.model
    if args.mock_dir:
        env["DECISION_GRAPH_BACKEND"] = f"mock:{Path(args.mock_dir).resolve() / (case['name'] + '.json')}"
    payload = json.dumps({"session_id": f"eval-{case['name']}-r{k}", "transcript_path": str(tr), "trigger": "eval"})
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, str(EXTRACT)], input=payload, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=400)
        rc, err = p.returncode, p.stderr.strip()
    except subprocess.TimeoutExpired:
        rc, err = -1, "timed out"
    secs = time.time() - t0
    recs = []
    if (store / "decisions.jsonl").exists():
        recs = [json.loads(l) for l in (store / "decisions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    log = (store / "extract.log").read_text(encoding="utf-8", errors="replace") if (store / "extract.log").exists() else ""
    out = {"case": case["name"], "run": k, "ok": rc == 0, "seconds": round(secs, 1), "error": err[-500:] if rc else "",
           "records": [r for r in recs if r["id"] not in {e["id"] for e in case["existing"]}],
           "log": log[-1500:]}
    out["score"] = score_case(case, recs) if rc == 0 else None
    return out


def pct(n, d):
    return f"{100 * n / d:.0f}%" if d else "n/a"


def aggregate(cases: list[dict], results: list[dict]) -> dict:
    ok = [r for r in results if r["ok"]]
    tot = {k: 0 for k in ("gold", "found", "preds", "traps", "trap_hits", "links", "links_ok", "forbidden",
                          "amend", "amend_ok", "dupes", "attr", "attr_ok", "rej", "rej_ok", "extra_links", "pred_links")}
    for r in ok:
        s, case = r["score"], next(c for c in cases if c["name"] == r["case"])
        tot["gold"] += len(s["found"])
        tot["found"] += sum(1 for v in s["found"].values() if v)
        tot["optional"] = tot.get("optional", 0) + sum(1 for v in s.get("optional", {}).values() if v)
        tot["preds"] += len(s["found_preds"])
        tot["traps"] += len(case["gold"].get("traps", []))
        tot["trap_hits"] += len({tuple(t["terms"]) for t in s["traps"]})
        tot["links"] += len(s["links"])
        tot["links_ok"] += sum(s["links"].values())
        tot["forbidden"] += len(s["forbidden"])
        tot["amend"] += len(s["amendments"])
        tot["amend_ok"] += sum(s["amendments"].values())
        tot["dupes"] += len(s["duplicates"])
        tot["attr"] += len(s["decided_by"])
        tot["attr_ok"] += sum(s["decided_by"].values())
        tot["rej"] += len(s["rejected"])
        tot["rej_ok"] += sum(s["rejected"].values())
        tot["extra_links"] += len(s["extra_links"])
        for k in ("reasons_expected", "reasons_captured", "reasons_total"):
            tot[k] = tot.get(k, 0) + s.get(k, 0)
        tot["reasons_unsupported"] = tot.get("reasons_unsupported", 0) + len(s.get("reasons_unsupported", []))
    precision = (tot["found"] / tot["preds"]) if tot["preds"] else 0
    recall = (tot["found"] / tot["gold"]) if tot["gold"] else 0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
    return {"runs": len(results), "failed_runs": len(results) - len(ok), "precision": precision, "recall": recall,
            "f1": f1, **tot, "avg_seconds": sum(r["seconds"] for r in ok) / len(ok) if ok else 0}


def report(cases, results, summ, meta) -> str:
    L = []
    L.append(f"# Extraction eval: {meta['label'] or meta['stamp']}")
    L.append(f"\nModel `{meta['model']}`, prompt version {meta['plugin_version']}, {meta['runs']} run(s) per case, "
             f"{len(cases)} cases, {meta['stamp']}.\n")
    L.append("| Metric | Result |\n|---|---|")
    L.append(f"| Decisions found (recall) | **{pct(summ['found'], summ['gold'])}** ({summ['found']}/{summ['gold']}) |")
    L.append(f"| Extracted that were right (precision) | **{pct(summ['found'], summ['preds'])}** ({summ['found']}/{summ['preds']}) |")
    L.append(f"| F1 | **{summ['f1']:.2f}** |")
    L.append(f"| Traps triggered, summed over runs (lower is better) | {summ['trap_hits']}/{summ['traps']} |")
    L.append(f"| Links right | {pct(summ['links_ok'], summ['links'])} ({summ['links_ok']}/{summ['links']}) |")
    L.append(f"| Wrong link kind (e.g. reversal as 'rests on') | {summ['forbidden']} |")
    L.append(f"| Amendments right | {summ['amend_ok']}/{summ['amend']} |")
    L.append(f"| Duplicates of existing decisions | {summ['dupes']} |")
    L.append(f"| Rejected alternatives recorded | {summ['rej_ok']}/{summ['rej']} |")
    L.append(f"| Reasons captured, where one was given | {pct(summ.get('reasons_captured', 0), summ.get('reasons_expected', 0))} ({summ.get('reasons_captured', 0)}/{summ.get('reasons_expected', 0)}) |")
    L.append(f"| Unsupported reasons, not said in the conversation (lower is better) | {summ.get('reasons_unsupported', 0)} of {summ.get('reasons_total', 0)} recorded |")
    L.append(f"| Who decided (user vs accepted suggestion) | {pct(summ['attr_ok'], summ['attr'])} ({summ['attr_ok']}/{summ['attr']}) |")
    L.append(f"| Links not in the gold answer | {summ['extra_links']} |")
    L.append(f"| Failed runs | {summ['failed_runs']}/{summ['runs']} |")
    L.append(f"| Avg seconds per extraction | {summ['avg_seconds']:.1f} |")

    for case in cases:
        rs = sorted([r for r in results if r["case"] == case["name"]], key=lambda r: r["run"])
        okr = [r for r in rs if r["ok"]]
        L.append(f"\n## {case['name']}\n\n{case['description']}\n")
        for r in rs:
            if not r["ok"]:
                L.append(f"- run {r['run']}: **FAILED** {r['error'][:300]}")
        if not okr:
            continue
        n = len(okr)
        for g in case["gold"]["decisions"]:
            if g.get("optional"):
                hits = sum(1 for r in okr if r["score"]["optional"].get(g["id"]))
                L.append(f"- `opt ` {g['id']} ({' + '.join('/'.join(x) for x in g['match'])}): recorded separately in {hits}/{n} (either way is fine)")
                continue
            hits = sum(1 for r in okr if r["score"]["found"][g["id"]])
            attr = [r["score"]["decided_by"].get(g["id"]) for r in okr if g["id"] in r["score"]["decided_by"]]
            att = f", attribution right {sum(attr)}/{len(attr)}" if attr else ""
            mark = "ok " if hits == n else ("MISS" if hits == 0 else "FLAKY")
            L.append(f"- `{mark}` {g['id']} ({' + '.join('/'.join(x) for x in g['match'])}): found {hits}/{n}{att}")
        for key in (case["gold"].get("links") or []):
            k = f"{key[0]} {key[1]}->{key[2]}"
            hits = sum(1 for r in okr if r["score"]["links"].get(k))
            L.append(f"- `{'ok ' if hits == n else 'MISS' if not hits else 'FLAKY'}` link {k}: {hits}/{n}")
        for a in case["gold"].get("amendments", []):
            hits = sum(1 for r in okr if r["score"]["amendments"].get(a["target"]))
            L.append(f"- `{'ok ' if hits == n else 'MISS' if not hits else 'FLAKY'}` amendment to {a['target']}: {hits}/{n}")
        for k in {k for r in okr for k in r["score"]["rejected"]}:
            hits = sum(1 for r in okr if r["score"]["rejected"].get(k))
            L.append(f"- `{'ok ' if hits == n else 'MISS' if not hits else 'FLAKY'}` rejected alternative {k}: {hits}/{n}")
        problems = []
        for r in okr:
            s = r["score"]
            problems += [f"run {r['run']}: trap ({t['why']}): \"{t['decision']}\"" for t in s["traps"]]
            problems += [f"run {r['run']}: wrong link kind {x}" for x in s["forbidden"]]
            problems += [f"run {r['run']}: duplicate: {x}" for x in s["duplicates"]]
            problems += [f"run {r['run']}: extra decision: \"{x}\"" for x in s["extra"]
                         if not any(x == t["decision"] for t in s["traps"])]
            problems += [f"run {r['run']}: link not in gold: {x}" for x in s["extra_links"]]
            problems += [f"run {r['run']}: acceptable (not counted): \"{x}\"" for x in s.get("neutral", [])]
            problems += [f"run {r['run']}: unsupported reason: {x}" for x in s.get("reasons_unsupported", [])]
        if problems:
            L.append("\nProblems:")
            L += [f"- {p}" for p in problems]
        L.append("\nExtracted (run 1):")
        for rec in (okr[0]["records"]):
            if rec.get("kind") == "amendment":
                L.append(f"- {rec['id']} adds to {rec['amends']}: {'; '.join(rec.get('rationale') or [])}")
            else:
                links = []
                if rec.get("depends_on"):
                    links.append("rests on " + ", ".join(rec["depends_on"]))
                if rec.get("supersedes"):
                    links.append("replaces " + rec["supersedes"])
                if rec.get("restores"):
                    links.append("restores " + ", ".join(rec["restores"]))
                L.append(f"- {rec['id']} [{rec.get('decided_by')}]: {rec['decision']}" + (f"  ({'; '.join(links)})" if links else ""))
    return "\n".join(L) + "\n"


def rescore(folder: Path) -> int:
    prev = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    cases = {c["name"]: c for c in json.loads((HERE / "cases.json").read_text(encoding="utf-8"))["cases"]}
    results = []
    for r in prev["results"]:
        case = cases[r["case"]]
        store = folder / "work" / f"{r['case']}-r{r['run']}" / ".claude" / "decisions" / "decisions.jsonl"
        recs = [json.loads(l) for l in store.read_text(encoding="utf-8").splitlines() if l.strip()] if store.exists() else []
        r = dict(r, records=[x for x in recs if x["id"] not in {e["id"] for e in case["existing"]}])
        r["score"] = score_case(case, recs) if r["ok"] else None
        results.append(r)
    used = [cases[n] for n in dict.fromkeys(r["case"] for r in results)]
    meta = dict(prev["meta"], label=(prev["meta"].get("label") or prev["meta"]["stamp"]) + " (re-scored)")
    summ = aggregate(used, results)
    md = report(used, results, summ, meta)
    (folder / "report.rescored.md").write_text(md, encoding="utf-8")
    (folder / "summary.rescored.json").write_text(json.dumps({"meta": meta, "summary": summ, "results": results},
                                                             indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Recall {pct(summ['found'], summ['gold'])}, precision {pct(summ['found'], summ['preds'])}, F1 {summ['f1']:.2f}, "
          f"traps {summ['trap_hits']}/{summ['traps']}, links {pct(summ['links_ok'], summ['links'])}")
    print(f"Report: {folder / 'report.rescored.md'}")
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--cases", help="only cases whose name contains this text")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--model")
    ap.add_argument("--label", default="")
    ap.add_argument("--mock-dir")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--rescore", type=Path, help="re-score an earlier results folder with the current cases/scorer; no model calls")
    a = ap.parse_args(argv)
    if a.rescore:
        return rescore(a.rescore)

    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))["cases"]
    if a.cases:
        cases = [c for c in cases if a.cases in c["name"]]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = a.out or HERE / "results" / stamp
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    version = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")).get("version")
    model = a.model or os.environ.get("DECISION_GRAPH_MODEL", "haiku")
    meta = {"stamp": stamp, "label": a.label, "model": "mock" if a.mock_dir else model,
            "plugin_version": version, "runs": a.runs}

    jobs = [(c, k) for c in cases for k in range(1, a.runs + 1)]
    print(f"Running {len(jobs)} extractions ({len(cases)} cases x {a.runs} runs, {a.jobs} at a time)...", flush=True)
    results, done = [], 0
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        futs = [ex.submit(run_one, c, k, work, a) for c, k in jobs]
        for f in futs:
            r = f.result()
            done += 1
            status = "ok" if r["ok"] else "FAILED"
            print(f"  [{done}/{len(jobs)}] {r['case']} run {r['run']}: {status} ({r['seconds']}s)", flush=True)
            results.append(r)

    summ = aggregate(cases, results)
    md = report(cases, results, summ, meta)
    (out / "report.md").write_text(md, encoding="utf-8")
    (out / "summary.json").write_text(json.dumps({"meta": meta, "summary": summ, "results": results},
                                                 indent=1, ensure_ascii=False), encoding="utf-8")
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "latest.md").write_text(md, encoding="utf-8")
    print(f"\nRecall {pct(summ['found'], summ['gold'])}, precision {pct(summ['found'], summ['preds'])}, "
          f"F1 {summ['f1']:.2f}, traps {summ['trap_hits']}/{summ['traps']}, links {pct(summ['links_ok'], summ['links'])}, "
          f"reasons captured {pct(summ['reasons_captured'], summ['reasons_expected'])}, "
          f"unsupported reasons {summ['reasons_unsupported']}/{summ['reasons_total']}, failed runs {summ['failed_runs']}")
    print(f"Report: {out / 'report.md'}")
    return 0 if not summ["failed_runs"] else 1


if __name__ == "__main__":
    sys.exit(main())
