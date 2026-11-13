#!/usr/bin/env python3
"""
First-run setup, done for the user at their first Claude Code session after installing the plugin.

The installers and `dg setup` do the same thing interactively. People who install the plugin some other way
(the plugin browser, the desktop app) never run those, so the session-start hook calls `maybe_run()`:

  1. the `dg` command, in ~/.local/bin (on Windows that folder is also added to the user's PATH);
  2. the live graph in the background, starting at login (skipped with DECISION_GRAPH_NO_SERVICE=1);
  3. remember where `claude` is, for extraction calls made from a terminal.

It runs once per computer (~/.decision-graph/setup.json), never on a cloud or Cowork sandbox (that isn't the
user's computer), and returns one line for the session status so the user knows what happened.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from dg_store import USER_HOME, find_claude  # noqa: E402

MARKER = USER_HOME / "setup.json"
BIN = Path.home() / ".local" / "bin"


def plugin_version() -> str:
    try:
        return json.loads((HERE.parent / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    except Exception:
        return "?"


def on_users_computer() -> bool:
    if os.environ.get("DECISION_GRAPH_NO_AUTOSETUP") or os.environ.get("CI"):
        return False
    if os.environ.get("CLAUDE_CODE_REMOTE", "").lower() == "true":
        return False
    entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").lower()
    return not ("remote" in entry or "cowork" in entry)


def add_to_user_path_windows(folder: Path) -> bool:
    """Append `folder` to the user's PATH in the registry (no setx: it truncates long PATHs). True if it's there."""
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE)
    try:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
        parts = [p for p in value.split(";") if p]
        norm = lambda p: os.path.normcase(os.path.expandvars(p)).rstrip("\\")
        if any(norm(p) == norm(str(folder)) for p in parts):
            return True
        winreg.SetValueEx(key, "Path", 0, kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else winreg.REG_EXPAND_SZ,
                          ";".join(parts + [str(folder)]))
    finally:
        winreg.CloseKey(key)
    try:   # tell Explorer, so new terminals see it without logging out
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 1000, None)
    except Exception:
        pass
    return True


def run_setup(service: bool = True) -> dict:
    """Do the setup steps; never raises. Returns what happened, for the status line and the marker."""
    done: dict = {"version": plugin_version(), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        import install_cli
        install_cli.install(BIN, quiet=True)
        done["dg"] = str(BIN)
        if install_cli.on_path(BIN):
            done["path"] = "ok"
        elif os.name == "nt":
            done["path"] = "added" if add_to_user_path_windows(BIN) else "missing"
        else:
            done["path"] = "missing"
    except Exception as e:
        done["dg_error"] = str(e)[:200]
    if service and not os.environ.get("DECISION_GRAPH_NO_SERVICE"):
        try:
            import dg_service
            if not dg_service.installed():
                dg_service.install(quiet=True)
            done["service"] = "running" if dg_service.running() else "installed"
        except Exception as e:
            done["service_error"] = str(e)[:200]
    claude = find_claude()
    done["claude"] = claude or ""
    return done


def message(done: dict) -> str:
    bits = []
    if done.get("dg"):
        if done.get("path") == "missing":
            bits.append(f"the `dg` command is in {done['dg']} (add that folder to your PATH to use it)")
        else:
            bits.append("the `dg` command is ready (in a new terminal)")
    if done.get("service") == "running":
        bits.append("the live graph runs in the background and starts at login (`dg service remove` turns that off)")
    elif done.get("service") == "installed":
        bits.append("the live graph will run in the background from your next login")
    text = "Decision graph is set up on this computer: " + "; ".join(bits) + "." if bits else \
        "Decision graph couldn't finish setting up on this computer."
    if not done.get("claude"):
        text += " It couldn't find the `claude` program it uses to record decisions; run `dg doctor` for the fix."
    if done.get("dg_error") or done.get("service_error"):
        text += " Some steps failed; `dg setup` in a terminal shows why."
    return text


def maybe_run() -> str | None:
    """Called by the session-start hook. Returns a line for the user the first time, None afterwards."""
    if MARKER.exists() or not on_users_computer():
        return None
    done = run_setup()
    try:
        USER_HOME.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(json.dumps(done, indent=1), encoding="utf-8")
    except OSError:
        return None          # couldn't remember it: stay quiet rather than repeat every session
    return message(done)


def mark_done(extra: dict | None = None) -> None:
    """`dg setup` calls this, so the hook doesn't set things up a second time."""
    try:
        USER_HOME.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(json.dumps({"version": plugin_version(), "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                      "by": "dg setup", **(extra or {})}, indent=1), encoding="utf-8")
    except OSError:
        pass
