#!/usr/bin/env python3
"""
dg setup: make everything work on this computer, in one go.

  1. Check what the hooks need: they find Python on their own (python3, python or py), and run through `sh`,
     which on Windows comes with Git for Windows (offered with winget if it's missing).
  2. Install the `dg` command.
  3. Run the health check (dg doctor).

usage: dg setup [--yes] [--offline]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
BIN = Path.home() / ".local" / "bin"


def ask(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")


def git_bash() -> str | None:
    """Windows: the hooks run through Git Bash (`sh`), as Claude Code's own shell does. Where is it?"""
    import shutil
    env = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if env and Path(env).is_file():
        return env
    for c in (shutil.which("bash"), shutil.which("sh")):
        if c and "WindowsApps" not in c and "System32" not in c:
            return c
    git = shutil.which("git")
    roots = [Path(git).resolve().parent.parent] if git else []
    roots += [Path(os.environ.get(v, "")) / "Git" for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    roots += [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git"]
    for r in roots:
        b = r / "bin" / "bash.exe"
        if b.is_file():
            return str(b)
    return None


def python_for_hooks(yes: bool) -> bool:
    """The hooks find Python themselves (python3, python or py, skipping placeholders). What they need is `sh`."""
    print(f"  ok    Python {sys.version.split()[0]} ({sys.executable})")
    if os.name != "nt":
        return True
    gb = git_bash()
    if gb:
        print(f"  ok    hooks run through Git Bash ({gb})")
        return True
    print("  !!    Git for Windows (Git Bash) isn't installed. Claude Code runs the plugin's hooks with it, so\n"
          "        without it decisions aren't recorded.")
    import shutil
    if shutil.which("winget") and ask("        Install Git for Windows with winget now?", yes):
        r = subprocess.run(["winget", "install", "-e", "--id", "Git.Git", "--accept-package-agreements",
                            "--accept-source-agreements"])
        if r.returncode == 0:
            print("        Installed. Restart Claude Code so it picks it up.")
            return True
    print("        Install it from https://git-scm.com/download/win, then restart Claude Code.")
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="answer yes to every question")
    ap.add_argument("--offline", action="store_true", help="skip the live login check")
    ap.add_argument("--no-service", action="store_true", help="don't keep the live graph running in the background")
    a = ap.parse_args(argv)
    print("Setting up decision-graph\n")
    print("1. What the hooks need")
    ok = python_for_hooks(a.yes)
    print("\n2. The dg command")
    import install_cli
    target = install_cli.install(BIN, quiet=True)
    on_path = install_cli.on_path(BIN)
    if not on_path and os.name == "nt":
        try:
            import dg_firstrun
            if dg_firstrun.add_to_user_path_windows(BIN):
                print(f"  ok    {target}  (added {BIN} to your PATH: open a new terminal to use `dg`)")
                on_path = None
        except Exception:
            pass
    if on_path is not None:
        print(f"  ok    {target}" + ("" if on_path else f"  ({BIN} isn't on PATH yet: add it to your shell profile, "
                                                          f"e.g. export PATH=\"$HOME/.local/bin:$PATH\")"))
    print("\n3. Live graph in the background")
    import dg_service
    if a.no_service:
        print("  skipped (turn it on later with `dg service install`)")
    elif dg_service.installed() and dg_service.running():
        print("  ok    already running, and starts at login")
    else:
        yes = a.yes
        if not yes:
            try:
                ans = input("  Keep the live graph running in the background, so `dg view` opens instantly and Claude in\n"
                            "  Cowork can show it? It starts when you log in and only this computer can reach it. [Y/n] ")
            except EOFError:
                ans = "n"
            yes = ans.strip().lower() in ("", "y", "yes")
        if yes:
            dg_service.install(quiet=True)
            st = dg_service.running()
            print(f"  ok    running on http://127.0.0.1:{st['port']}, and starts at login" if st else
                  "  !!    installed, but it didn't start now; run `dg service run` to see why")
        else:
            print("  skipped (turn it on later with `dg service install`)")
    try:
        import dg_firstrun
        dg_firstrun.mark_done()
    except Exception:
        pass
    print("\n4. Health check")
    import dg_doctor
    rc = dg_doctor.main(["--offline"] if a.offline else [])
    print("\nNext: open Claude Code in a project and work as usual. Decisions are recorded when the conversation is\n"
          "compacted, each session starts with a one-line status, and `dg view` shows the graph. `dg demo` shows an example.")
    return 0 if ok and rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
