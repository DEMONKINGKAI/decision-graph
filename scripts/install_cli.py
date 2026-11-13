#!/usr/bin/env python3
"""
Install the `dg` command, so you can type `dg view` instead of a full script path.

usage: python install_cli.py [--dir DIR] [--uninstall]

Writes a launcher into ~/.local/bin (the folder the Claude Code installer already puts on your PATH):
dg.cmd on Windows, dg elsewhere. The launcher always runs the newest copy of the plugin it can find:
the one Claude Code installed (which changes folder on every update) or this folder, whichever is newer.
So `dg` keeps working after `claude plugin update`.
"""
from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOOT_DIR = Path.home() / ".decision-graph"

BOOT = r'''# Finds the newest decision-graph plugin and runs its dg.py. Written by install_cli.py.
import glob, json, os, runpy, sys
from pathlib import Path

FALLBACK = {fallback!r}


def version(scripts):
    try:
        v = json.loads((Path(scripts).parent / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0,)


config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
found = [os.path.dirname(p) for p in glob.glob(os.path.join(config, "plugins", "cache", "*", "decision-graph", "*", "scripts", "dg.py"))]
if os.path.exists(os.path.join(FALLBACK, "dg.py")):
    found.append(FALLBACK)
if not found:
    sys.exit("decision-graph isn't installed. Install it with: claude plugin install decision-graph@neuro-graph")
best = max(found, key=lambda s: (version(s), s != FALLBACK))
sys.path.insert(0, best)
sys.argv[0] = os.path.join(best, "dg.py")
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def on_path(d: Path) -> bool:
    norm = lambda p: os.path.normcase(os.path.abspath(p))
    return any(norm(p) == norm(d) for p in os.environ.get("PATH", "").split(os.pathsep) if p)


def install(bindir: Path, quiet: bool = False) -> Path:
    BOOT_DIR.mkdir(parents=True, exist_ok=True)
    boot = BOOT_DIR / "dg_boot.py"
    boot.write_text(BOOT.format(fallback=str(HERE)), encoding="utf-8")
    bindir.mkdir(parents=True, exist_ok=True)
    target = bindir / ("dg.cmd" if os.name == "nt" else "dg")
    if os.name == "nt":
        target.write_text(f'@echo off\r\n"{sys.executable}" "{boot}" %*\r\n', encoding="utf-8")
    else:
        target.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{boot}" "$@"\n', encoding="utf-8")
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if not quiet:
        print(f"Installed the dg command: {target}")
    return target


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=Path.home() / ".local" / "bin")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args(argv)
    target = a.dir / ("dg.cmd" if os.name == "nt" else "dg")
    if a.uninstall:
        for f in (target, BOOT_DIR / "dg_boot.py"):
            if f.exists():
                f.unlink()
                print(f"Removed {f}")
        return 0
    install(a.dir)
    if on_path(a.dir):
        print("Open a new terminal, then try:  dg doctor")
    else:
        print(f"\n{a.dir} is not on your PATH yet. Add it, then open a new terminal:")
        if os.name == "nt":
            print(f'  [Environment]::SetEnvironmentVariable("Path", '
                  f'[Environment]::GetEnvironmentVariable("Path","User") + ";{a.dir}", "User")')
        else:
            print(f'  echo \'export PATH="{a.dir}:$PATH"\' >> ~/.bashrc   (or ~/.zshrc)')
    return 0


if __name__ == "__main__":
    sys.exit(main())
