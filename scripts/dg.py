#!/usr/bin/env python3
"""
dg: one command for the decision graph of the project you're in.

  dg                     decisions currently in force (same as `dg active`)
  dg view                open the interactive graph in your browser (updates live)
  dg demo                open an example graph, to see what this does
  dg stale               decisions whose reasons were replaced
  dg show D-3            one decision in full
  dg why D-3             what it rests on
  dg impact D-1          what depends on it
  dg history D-1         its versions
  dg search <words>      find decisions
  dg mermaid             the graph as Mermaid text
  dg confirm D-3         mark a decision as right
  dg reject D-9          "that's not a decision": hide it and drop its links
  dg edit D-3 "wording"  fix how a decision is worded
  dg corrections         list your corrections
  dg record              record decisions now, without waiting for a /compact
  dg add --clipboard     record decisions saved from a Chat or Cowork conversation (/decision-graph:save)
  dg export              write the decision log as Markdown (DECISIONS.md)
  dg topics              group the decisions into areas (narrator, storage, ...)
  dg rebuild             re-extract the whole project history with the current prompt
  dg service install     keep the live graph running in the background for all projects (starts at login)
  dg setup               set everything up on this computer (Python for the hooks, the dg command, a health check)
  dg doctor              check that everything is set up and healthy
  dg report              how it's been working for you: warnings, corrections, page opens

Options (any command): --project DIR   use another project instead of the current folder
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

QUERY = {"active", "show", "why", "impact", "history", "stale", "search", "mermaid"}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    # first positional argument that isn't the value of --project
    cmd, skip = None, False
    for a in argv:
        if skip:
            skip = False
            continue
        if a == "--project":
            skip = True
            continue
        if not a.startswith("-"):
            cmd = a
            break
    cmd = cmd or "active"
    try:   # let the background service know this project (cheap; only projects with decisions)
        import dg_service
        from dg_store import project_dir
        proj = Path(argv[argv.index("--project") + 1]) if "--project" in argv[:-1] else project_dir()
        dg_service.register(proj)
    except Exception:
        pass
    if cmd == "service":
        import dg_service
        rest = list(argv)
        rest.remove("service")
        if "--project" in rest:
            i = rest.index("--project"); del rest[i:i + 2]
        return dg_service.main(rest)
    if cmd in ("confirm", "reject", "edit", "corrections"):
        import dg_correct
        return dg_correct.main(argv)
    if cmd == "demo":
        import dg_view
        rest = list(argv)
        rest.remove("demo")
        return dg_view.demo(rest)
    if cmd == "setup":
        import dg_setup
        rest = list(argv)
        rest.remove("setup")
        return dg_setup.main(rest)
    if cmd == "add":
        import add_decisions
        rest = list(argv)
        rest.remove("add")
        return add_decisions.main(rest)
    if cmd == "record":
        import record_decisions
        rest = list(argv)
        rest.remove("record")
        return record_decisions.main(rest)
    if cmd == "topics":
        import dg_topics
        rest = list(argv)
        rest.remove("topics")
        return dg_topics.main(rest)
    if cmd == "export":
        import dg_export
        rest = list(argv)
        rest.remove("export")
        return dg_export.main(rest)
    if cmd == "report":
        import dg_report
        rest = list(argv)
        rest.remove("report")
        return dg_report.main(rest)
    if cmd in QUERY or cmd not in ("view", "rebuild", "doctor", "setup", "report", "demo"):
        if cmd not in QUERY:
            print(f"Unknown command {cmd!r}.\n")
            print(__doc__.strip())
            return 2
        import query_decisions
        return query_decisions.main(argv if argv else ["active"])
    rest = list(argv)
    if cmd in rest:
        rest.remove(cmd)
    if cmd == "view":
        import dg_view
        return dg_view.main(rest)
    if cmd == "rebuild":
        import rebuild_decisions
        return rebuild_decisions.main(rest)
    import dg_doctor
    return dg_doctor.main(rest)


def entry() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except BrokenPipeError:  # e.g. `dg active | head`
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(entry())
