#!/usr/bin/env python3
"""
dg doctor: check the decision-graph setup and the current project's graph.

usage: dg_doctor.py [--project DIR] [--offline]

  --offline   skip the live login check (which makes one small Haiku call)

Exit code 0 when nothing failed (warnings are fine), 1 otherwise.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from dg_store import Graph, find_claude, Store, project_dir, utf8_stdio  # noqa: E402

PLUGIN_ROOT = HERE.parent


class Report:
    def __init__(self):
        self.fails = 0
        self.warns = 0

    def ok(self, msg):
        print(f"  [ok]   {msg}")

    def info(self, msg):
        print(f"  [info] {msg}")

    def warn(self, msg, fix=None):
        self.warns += 1
        print(f"  [warn] {msg}")
        if fix:
            print(f"         fix: {fix}")

    def fail(self, msg, fix=None):
        self.fails += 1
        print(f"  [FAIL] {msg}")
        if fix:
            print(f"         fix: {fix}")


def run(cmd: list[str], timeout=30, **kw) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None


# ------------------------------------------------------------------ setup checks
def check_setup(r: Report, offline: bool) -> None:
    print("Setup")
    v = sys.version_info
    if v < (3, 9):
        r.fail(f"Python {v.major}.{v.minor} is too old", "install Python 3.9 or newer")
    else:
        r.ok(f"Python {v.major}.{v.minor}.{v.micro}")

    # the hooks run `sh run-hook.sh`, which finds Python itself; on Windows `sh` comes with Git for Windows
    if os.name == "nt":
        try:
            import dg_setup
            gb = dg_setup.git_bash()
        except Exception:
            gb = None
        if gb:
            r.ok(f"hooks run through Git Bash ({gb})")
        else:
            r.fail("Git for Windows (Git Bash) isn't installed, and Claude Code runs the plugin's hooks with it",
                   "winget install Git.Git  (or https://git-scm.com/download/win), then restart Claude Code")
    elif not shutil.which("sh"):
        r.fail("`sh` isn't on PATH, so the hooks can't start", "install a POSIX shell (it's standard on macOS and Linux)")
    else:
        r.ok("hooks will find this Python on their own")

    exe = find_claude()
    if not exe:
        r.fail("`claude` isn't on PATH, so extraction can't call the model",
               "install Claude Code (Windows: irm https://claude.ai/install.ps1 | iex), then open a new terminal")
        return
    p = run([exe, "--version"])
    r.ok(f"claude {p.stdout.strip() if p and p.returncode == 0 else '(version unknown)'} at {exe}")

    if os.environ.get("ANTHROPIC_API_KEY"):
        mode = os.environ.get("DECISION_GRAPH_AUTH", "auto")
        if mode == "api-key":
            r.info("ANTHROPIC_API_KEY is set and DECISION_GRAPH_AUTH=api-key: extraction is billed to that API key")
        else:
            r.warn("ANTHROPIC_API_KEY is set. Extraction ignores it and uses your Claude login, but other "
                   "`claude -p` scripts you run will use the key (and fail if it has no credit)",
                   "if you don't need it: [Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY',$null,'User') "
                   "on Windows, or remove it from your shell profile")

    # installed plugin vs this folder
    p = run([exe, "plugin", "list", "--json"])
    try:
        plugins = json.loads(p.stdout) if p and p.returncode == 0 else []
    except json.JSONDecodeError:
        plugins = []
    mine = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")).get("version")
    entry = next((x for x in plugins if str(x.get("id", "")).startswith("decision-graph@")), None)
    if not entry:
        r.warn("the decision-graph plugin isn't installed in Claude Code, so nothing is captured automatically",
               f'claude plugin marketplace add "{PLUGIN_ROOT.as_posix()}" then '
               "claude plugin install decision-graph@neuro-graph")
    else:
        inst = entry.get("version")
        if not entry.get("enabled", True):
            r.warn(f"plugin {entry['id']} is installed but disabled", f"claude plugin enable {entry['id']}")
        if inst != mine and entry.get("folderVersion", mine) == mine:
            market = entry["id"].split("@", 1)[1]
            r.fail(f"Claude Code runs plugin {inst}, but this folder has {mine}: your latest changes aren't active",
                   f"claude plugin marketplace update {market}; claude plugin update {entry['id']}; "
                   "then restart Claude Code")
        else:
            r.ok(f"plugin {entry['id']} {inst} installed and up to date")

    try:
        import dg_service
        st = dg_service.running()
        if st:
            r.ok(f"background service running on http://127.0.0.1:{st['port']}" + ("" if dg_service.installed() else ", but not set to start at login"))
        elif dg_service.installed():
            r.warn("the background service is set to start at login but isn't running", "dg service run (shows why it stops)")
        else:
            r.info("background service off: `dg view` starts its own page, and Cowork shows a snapshot (`dg service install` turns it on)")
    except Exception:
        pass
    if offline:
        r.info("skipped the live login check (--offline)")
        return
    import extract_decisions
    try:
        out = extract_decisions.call_model("Reply with exactly: ok")
        if "ok" in out.lower():
            r.ok("a test extraction call works with your login")
        else:
            r.warn(f"test call answered unexpectedly: {out.strip()[:80]}")
    except Exception as e:
        msg = str(e)
        hint = "run `claude` once and log in"
        if "credit balance" in msg.lower():
            hint = "the API key in use has no credit; remove ANTHROPIC_API_KEY or set DECISION_GRAPH_AUTH=login"
        elif "not logged in" in msg.lower() or "/login" in msg:
            hint = "run `claude` in a terminal and complete the login"
        r.fail("the test extraction call failed: " + " / ".join(l for l in msg.splitlines() if l.strip()
                                                               and not l.startswith("---"))[:300], hint)


# ------------------------------------------------------------------ graph checks
def norm(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def find_cycles(adj: dict[str, list[str]]) -> list[list[str]]:
    cycles, color, stack = [], {}, []

    def visit(u):
        color[u] = 1
        stack.append(u)
        for v in adj.get(u, []):
            if color.get(v) == 1:
                cycles.append(stack[stack.index(v):] + [v])
            elif not color.get(v):
                visit(v)
        stack.pop()
        color[u] = 2

    for u in list(adj):
        if not color.get(u):
            visit(u)
    return cycles


def check_graph(r: Report, project: Path) -> None:
    print(f"\nProject graph: {project}")
    store = Store(project)
    if not store.records_path.exists():
        r.info("no decisions recorded yet. They appear after the first /compact in a Claude Code session here")
    else:
        raw = store.records_path.read_text(encoding="utf-8", errors="replace").splitlines()
        bad = []
        for i, line in enumerate(raw, 1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                bad.append(i)
        records = store.records()
        g = Graph(records)
        amend = sum(len(v) for v in g.amendments.values())
        ncorr = sum(len(v) for v in g.corrections.values())
        r.ok(f"{len(g.by_id)} decisions ({len(g.active())} in force), {amend} amendment{'' if amend == 1 else 's'}"
             + (f", {ncorr} correction{'' if ncorr == 1 else 's'} by you ({len(g.rejected)} rejected)" if ncorr else ""))
        if bad:
            r.warn(f"{len(bad)} unreadable line(s) in decisions.jsonl (line {', '.join(map(str, bad[:5]))}); "
                   "they are skipped", "usually a write cut off by a crash; `dg rebuild` regenerates the file")

        ids = set(g.by_id)
        dangling = []
        for rec in g.by_id.values():
            for d in rec.get("depends_on") or []:
                if d not in ids:
                    dangling.append(f"{rec['id']} rests on missing {d}")
            for k in ("supersedes",):
                if rec.get(k) and rec[k] not in ids:
                    dangling.append(f"{rec['id']} replaces missing {rec[k]}")
            for x in rec.get("restores") or []:
                if x not in ids:
                    dangling.append(f"{rec['id']} restores missing {x}")
        all_ids = ids | set(g.rejected)
        for target, cs in g.corrections.items():
            if target not in all_ids:
                dangling += [f"{c['id']} corrects missing {target}" for c in cs]
        for target, amends in g.amendments.items():
            if target not in ids:
                dangling += [f"{a['id']} adds to missing {target}" for a in amends]
        if dangling:
            r.warn("references to decisions that don't exist: " + "; ".join(dangling[:6]))
        else:
            r.ok("all references point to existing decisions")

        multi = {}
        for rec in g.by_id.values():
            if rec.get("supersedes"):
                multi.setdefault(rec["supersedes"], []).append(rec["id"])
        multi = {k: v for k, v in multi.items() if len(v) > 1}
        if multi:
            r.warn("decisions replaced more than once (only the last replacement counts): " +
                   "; ".join(f"{k} by {', '.join(v)}" for k, v in multi.items()))

        cyc = find_cycles({k: v.get("depends_on") or [] for k, v in g.by_id.items()})
        cyc += find_cycles({k: [v["supersedes"]] if v.get("supersedes") else [] for k, v in g.by_id.items()})
        if cyc:
            r.warn("circular links: " + "; ".join(" -> ".join(c) for c in cyc[:4]),
                   "`dg rebuild` with the current version prevents these")
        else:
            r.ok("no circular links")

        act = g.active()
        dupes = []
        for i, a in enumerate(act):
            for b in act[i + 1:]:
                if difflib.SequenceMatcher(None, norm(a["decision"]), norm(b["decision"])).ratio() > 0.85:
                    dupes.append(f"{a['id']} ~ {b['id']}")
        if dupes:
            r.warn("possible duplicate decisions in force: " + ", ".join(dupes[:6]),
                   "check with `dg show <id>`; restate the right one in conversation so the next compaction replaces the other")
        stale = [x["id"] for x in act if g.stale_deps(x["id"])]
        if stale:
            r.info(f"{len(stale)} decision(s) in force rest on replaced reasons: {', '.join(stale)} (see `dg stale`)")

    # last extraction
    if store.log_path.exists():
        lines = store.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        last_err = next((l for l in reversed(lines) if " ERROR " in l), None)
        last_ok = next((l for l in reversed(lines) if re.search(r"\+\d+ decisions|nothing new", l)), None)
        if last_err and (not last_ok or last_err[:25] > last_ok[:25]):
            r.fail(f"the most recent extraction failed: {last_err[26:200]}",
                   "fix the setup problem above, then run /compact again (failed turns are retried)")
        elif last_ok:
            r.ok(f"last extraction succeeded ({last_ok[:19].replace('T', ' ')} UTC)")

    # transcripts (needed for rebuild)
    import rebuild_decisions
    tdir = rebuild_decisions.transcripts_dir(project)
    n = len(list(tdir.glob("*.jsonl"))) if tdir.is_dir() else 0
    r.info(f"{n} saved Claude Code transcript(s) for this project" + (" (`dg rebuild` can re-extract them)" if n else ""))


def main(argv=None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path)
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args(argv)
    r = Report()
    check_setup(r, a.offline)
    check_graph(r, (a.project or project_dir()).resolve())
    print()
    if r.fails:
        print(f"{r.fails} problem(s) need fixing" + (f", {r.warns} warning(s)." if r.warns else "."))
    elif r.warns:
        print(f"Working, with {r.warns} warning(s).")
    else:
        print("Everything looks good.")
    return 1 if r.fails else 0


if __name__ == "__main__":
    sys.exit(main())
