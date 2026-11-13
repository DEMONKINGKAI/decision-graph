#!/usr/bin/env python3
"""
The decision-graph background service: keeps the live graph page running for every project on this computer,
so `dg view` opens instantly and Claude in Cowork can always show the live graph.

usage:
  dg service install     start it now and every time you log in
  dg service remove      stop it and stop starting it at login
  dg service status      is it installed, is it running, which projects it serves
  dg service run         run it in this terminal (what the login entry does)
  dg service scan        look for projects with a decision graph under your home folder now

It listens on 127.0.0.1 only (this computer), and each project's page needs that project's own access key.
Each project's link is written to <project>/.claude/decisions/live.json (kept out of git). Projects are added
when Claude Code starts a session in them, when you use `dg` in them, and by a scan of your home folder every few
minutes (which finds projects you only use through Cowork). `dg service scan` runs that scan now.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dg_store import utf8_stdio  # noqa: E402

HOME = Path(os.environ.get("DECISION_GRAPH_HOME") or Path.home() / ".decision-graph")
REGISTRY = HOME / "projects.json"
STATE = HOME / "service.json"
PORT = int(os.environ.get("DECISION_GRAPH_PORT", "47474"))
HERE = Path(__file__).resolve().parent


# ------------------------------------------------------------------ registry of projects
def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def key_for(project: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", project.name).strip("-")[:32] or "project"
    return f"{name}-{hashlib.sha1(str(project).lower().encode()).hexdigest()[:6]}"


def register(project: Path) -> None:
    """Remember a project so the service serves it. Cheap and silent; never raises."""
    try:
        project = Path(project).resolve()
        if not (project / ".claude" / "decisions").is_dir():
            return
        reg = _load(REGISTRY, {"projects": {}})
        if str(project) in reg.setdefault("projects", {}):
            return
        reg["projects"][str(project)] = {"key": key_for(project), "token": secrets.token_urlsafe(18)}
        HOME.mkdir(parents=True, exist_ok=True)
        tmp = REGISTRY.with_suffix(".tmp")
        tmp.write_text(json.dumps(reg, indent=1), encoding="utf-8")
        os.replace(tmp, REGISTRY)
    except Exception:
        pass


# ------------------------------------------------------------------ finding projects on its own
SKIP_DIRS = {"node_modules", "appdata", "library", "applications", "venv", "env", "__pycache__", "site-packages",
             "dist", "build", "target", "pictures", "music", "videos", "movies", "snap", "go", "program files",
             "program files (x86)", "windows", "vendor", "bower_components", "pods", "deriveddata"}
SCAN_EVERY_S = int(os.environ.get("DECISION_GRAPH_SCAN_SECONDS", "180"))


def scan_roots() -> list[Path]:
    env = os.environ.get("DECISION_GRAPH_SCAN_ROOTS")
    if env:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p.strip()]
    return [Path.home()]


def discover(roots: list[Path] | None = None, max_depth: int = 5, budget: int = 40000) -> list[Path]:
    """Folders under the user's home that have a decision graph (<folder>/.claude/decisions/decisions.jsonl).
    Finds projects used only through Cowork or Chat, which never ran anything on this computer. Skips hidden
    folders and the usual heavy ones, follows no links, and stops after `budget` folders so it stays cheap."""
    found: list[Path] = []
    seen = 0
    stack = [(Path(r), 0) for r in (roots or scan_roots())]
    while stack and seen < budget:
        d, depth = stack.pop()
        seen += 1
        try:
            if (d / ".claude" / "decisions" / "decisions.jsonl").is_file():
                found.append(d)
            if depth >= max_depth:
                continue
            with os.scandir(d) as it:
                for e in it:
                    n = e.name
                    if n.startswith(".") or n.startswith("$") or n.lower() in SKIP_DIRS:
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append((Path(e.path), depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def register_found(roots: list[Path] | None = None) -> list[Path]:
    """Register every discovered project that isn't known yet. Returns the new ones."""
    known = {k.lower() for k in _load(REGISTRY, {"projects": {}}).get("projects", {})}
    new = [p for p in discover(roots) if str(p.resolve()).lower() not in known]
    for p in new:
        register(p)
    return new


# ------------------------------------------------------------------ is it running?
def _get(url: str, timeout: float = 0.6):
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def running() -> dict | None:
    st = _load(STATE, None)
    if not st:
        return None
    try:
        if _get(f"http://127.0.0.1:{st['port']}/health").get("service") == "decision-graph":
            return st
    except Exception:
        pass
    return None


def detach(args: list[str]) -> None:
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        try:
            subprocess.Popen(args, creationflags=flags | 0x01000000, **kw)
        except OSError:
            subprocess.Popen(args, creationflags=flags, **kw)
    else:
        subprocess.Popen(args, start_new_session=True, **kw)


def python_quiet() -> str:
    """pythonw on Windows (no console window), the current interpreter elsewhere."""
    exe = Path(sys.executable)
    if os.name == "nt":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


def launcher() -> list[str]:
    """How to start `service run`: through the dg launcher, which follows plugin updates, if it's installed."""
    boot = HOME / "dg_boot.py"
    if boot.exists():
        return [python_quiet(), str(boot), "service", "run"]
    return [python_quiet(), str(HERE / "dg.py"), "service", "run"]


def ensure_running() -> None:
    """Called at session start: if the service is installed but not running (a crash, a sleep), start it."""
    try:
        if installed() and not running():
            detach(launcher())
    except Exception:
        pass


# ------------------------------------------------------------------ the service itself
def run() -> int:
    import threading
    import dg_view
    already = running()
    if already:
        print(f"Already running on port {already['port']}.")
        return 0
    hub = None
    for port in range(PORT, PORT + 20):
        try:
            hub = None
            secret = secrets.token_urlsafe(18)

            def on_request(handler, u, _secret=secret):
                if u.path == "/stop" and (u.query or "").endswith(_secret):
                    threading.Thread(target=lambda: (time.sleep(0.2), hub.httpd.shutdown()), daemon=True).start()
                    return (200, '{"stopping": true}')
                return None
            hub = dg_view._Hub(port, idle_minutes=None, prefixed=True, on_request=on_request)
            break
        except OSError:
            continue
    if hub is None:
        print(f"No free port between {PORT} and {PORT + 19}.")
        return 1
    HOME.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"port": hub.httpd.server_address[1], "pid": os.getpid(), "stop": secret,
                                 "started": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
    written: dict[str, str] = {}

    def sync():
        reg = _load(REGISTRY, {"projects": {}}).get("projects", {})
        for path, info in reg.items():
            project = Path(path)
            if not (project / ".claude" / "decisions").is_dir():
                continue
            hub.add(info["key"], project, info["token"])
            url = hub.url_for(info["key"])
            live = dg_view._live_file(project)
            current = _load(live, {})
            if written.get(path) != url or current.get("url") != url:
                # don't take over from a `dg view` that's running there right now
                if current.get("url") and not current.get("service") and dg_view.running_live(project):
                    continue
                dg_view.write_live(project, url, service=True)
                written[path] = url

    def scan_loop():
        time.sleep(2)
        while True:
            try:
                register_found()          # the registry change is picked up by watch()
            except Exception:
                pass
            time.sleep(SCAN_EVERY_S)

    def watch():
        last = None
        while True:
            try:
                m = REGISTRY.stat().st_mtime if REGISTRY.exists() else None
                if m != last:
                    last = m
                    sync()
                else:
                    sync_missing()
            except Exception:
                pass
            time.sleep(5)

    def sync_missing():                       # a live.json someone deleted, or a project folder that came back
        for path, url in list(written.items()):
            live = dg_view._live_file(Path(path))
            if not live.exists() and live.parent.is_dir():
                dg_view.write_live(Path(path), url, service=True)

    sync()
    threading.Thread(target=watch, daemon=True).start()
    threading.Thread(target=scan_loop, daemon=True).start()
    print(f"Decision graph service on http://127.0.0.1:{hub.httpd.server_address[1]}, serving {len(hub.sites)} project(s). "
          f"Ctrl+C stops it.", flush=True)
    try:
        hub.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.httpd.server_close()
        for path in written:                    # leave no links behind that point at nothing
            live = dg_view._live_file(Path(path))
            try:
                if _load(live, {}).get("service"):
                    live.unlink()
            except OSError:
                pass
        try:
            if _load(STATE, {}).get("pid") == os.getpid():
                STATE.unlink()
        except OSError:
            pass
    return 0


def stop() -> bool:
    st = running()
    if not st:
        return False
    try:
        _get(f"http://127.0.0.1:{st['port']}/stop?k={st['stop']}", timeout=2)
    except Exception:
        pass
    for _ in range(20):
        if not running():
            return True
        time.sleep(0.2)
    return not running()


# ------------------------------------------------------------------ start at login
def _autostart_path() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / \
            "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "decision-graph-service.vbs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / "com.decision-graph.service.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "decision-graph-service.desktop"


def installed() -> bool:
    return _autostart_path().exists()


def install(quiet: bool = False) -> int:
    if not (HOME / "dg_boot.py").exists():
        try:
            import install_cli
            install_cli.install(Path.home() / ".local" / "bin", quiet=True)
        except Exception:
            pass
    cmd = launcher()
    path = _autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        line = " ".join(f'""{c}""' if " " in c or "\\" in c else c for c in cmd)
        path.write_text(f'Set s = CreateObject("WScript.Shell")\r\ns.Run "{line}", 0, False\r\n', encoding="utf-8")
    elif sys.platform == "darwin":
        args = "".join(f"<string>{c}</string>" for c in cmd)
        path.write_text(f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                        f'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
                        f'<key>Label</key><string>com.decision-graph.service</string>'
                        f'<key>ProgramArguments</key><array>{args}</array>'
                        f'<key>RunAtLoad</key><true/></dict></plist>\n', encoding="utf-8")
    else:
        exec_line = " ".join(f'"{c}"' for c in cmd)
        path.write_text(f"[Desktop Entry]\nType=Application\nName=Decision graph service\nExec={exec_line}\n"
                        f"X-GNOME-Autostart-enabled=true\nNoDisplay=true\n", encoding="utf-8")
    if not running():
        detach(cmd)
        for _ in range(25):
            if running():
                break
            time.sleep(0.2)
    st = running()
    if not quiet:
        print(f"Installed: it starts when you log in ({path}).")
        print(f"Running on http://127.0.0.1:{st['port']}." if st else
              "It didn't start just now; it will at your next login, or run `dg service run` to see why.")
    return 0 if st else 1


def remove() -> int:
    path = _autostart_path()
    was = path.exists()
    try:
        path.unlink()
    except OSError:
        pass
    stopped = stop()
    print(("Removed the login entry. " if was else "It wasn't set to start at login. ")
          + ("Stopped the running service." if stopped else "It wasn't running."))
    return 0


def status() -> int:
    st = running()
    reg = _load(REGISTRY, {"projects": {}}).get("projects", {})
    print(f"Starts at login: {'yes' if installed() else 'no'} ({_autostart_path()})")
    print(f"Running: {'yes, on http://127.0.0.1:' + str(st['port']) if st else 'no'}")
    print(f"Projects ({len(reg)}):")
    for path in sorted(reg):
        print(f"  {path}" + ("" if Path(path, '.claude', 'decisions').is_dir() else "  (folder not found)"))
    if not installed():
        print("Turn it on with `dg service install`.")
    return 0


def main(argv=None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="dg service")
    ap.add_argument("action", nargs="?", default="status", choices=["install", "remove", "status", "run", "stop", "register", "scan"])
    ap.add_argument("--project", type=Path)
    a = ap.parse_args(argv)
    if a.action == "register":
        register((a.project or Path.cwd()))
        return 0
    if a.action == "scan":
        new = register_found()
        print("\n".join(f"Added {p}" for p in new) if new else "No new projects found.")
        return 0
    return {"install": install, "remove": remove, "status": status, "run": run, "stop": lambda: (print("Stopped." if stop() else "It wasn't running."), 0)[1]}[a.action]()


if __name__ == "__main__":
    sys.exit(main())
