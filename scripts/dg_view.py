#!/usr/bin/env python3
"""
Render the project's decision graph as a self-contained, offline HTML page.

usage: dg_view.py [--project DIR] [--out FILE] [--no-open]

Writes <project>/.claude/decisions/graph.html (by default) and opens it in the browser.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dg_store import Graph, Store, id_num, project_dir, utf8_stdio  # noqa: E402


def status_of(g: Graph, rid: str) -> str:
    if rid in g.rejected:
        return "rejected"
    if not g.is_active(rid):
        return "replaced"
    if g.stale_deps(rid):
        return "stale"
    if g.indirectly_stale(rid):
        return "indirect"
    if g.restored_deps(rid):
        return "resolved"
    return "ok"


def build_data(records: list[dict], project: Path, new_since: str | None = None) -> dict:
    g = Graph(records)
    nodes, edges = [], []
    for rid in sorted(g.by_id, key=id_num):
        r = g.by_id[rid]
        src = r.get("source") or {}
        warnings = [f"Rests on {d}, which was replaced by {now}." for d, now in g.stale_deps(rid)]
        warnings += [f"Indirectly affected: {' → '.join(p)}, and {a} rests on a replaced decision."
                     for a, p in g.indirectly_stale(rid)]
        notes = [f"Rests on {d}, which was replaced but later restored by {now}." for d, now in g.restored_deps(rid)]
        if not g.is_active(rid):
            now = g.current(rid)
            notes.append(f"Replaced by {g.superseded_by[rid]}; in force now: {now}."
                         + (f" Its content was restored by {now}." if g.holds(rid) else ""))
        nodes.append({
            "id": rid,
            "decision": r.get("decision", ""),
            "status": status_of(g, rid),
            "reasons": [{"text": t, "from": s} for t, s in g.rationale(rid)],
            "rejected": r.get("alternatives_rejected") or [],
            "depends_on": [d for d in r.get("depends_on") or [] if d in g.by_id],
            "dependents": sorted(g.dependents.get(rid, []), key=id_num),
            "supersedes": r.get("supersedes") if r.get("supersedes") in g.by_id else None,
            "superseded_by": g.superseded_by.get(rid),
            "restores": [x for x in r.get("restores") or [] if x in g.by_id],
            "history": g.history(rid),
            "warnings": warnings,
            "notes": notes,
            "new": bool(new_since and (r.get("extracted_at") or "") > new_since and g.is_active(rid)),
            "changed": bool(new_since and (r.get("extracted_at") or "") <= new_since and (
                any((c.get("at") or "") > new_since and c.get("action") in ("edit", "confirm") for c in g.corrections.get(rid, []))
                or any((x.get("extracted_at") or "") > new_since for x in g.amendments.get(rid, [])))),
            "topic": g.topics.get(rid),
            "confirmed": bool(r.get("confirmed")),
            "edited": bool(r.get("edited")),
            "original": r.get("original_decision"),
            "decided_by": r.get("decided_by", "unknown"),
            "confidence": r.get("confidence"),
            "decided_at": src.get("decided_at") or r.get("extracted_at"),
            "session": str(src.get("session_id") or "")[:8],
        })
        for d in r.get("depends_on") or []:
            if d in g.by_id:
                edges.append({"from": d, "to": rid, "kind": "depends"})
        if r.get("supersedes") in g.by_id:
            edges.append({"from": r["supersedes"], "to": rid, "kind": "supersedes"})
        for x in r.get("restores") or []:
            if x in g.by_id:
                edges.append({"from": rid, "to": x, "kind": "restores"})
    for rid in sorted(g.rejected, key=id_num):
        r = g.rejected[rid]
        nodes.append({
            "id": rid, "decision": r.get("decision", ""), "status": "rejected",
            "reasons": [{"text": t, "from": rid} for t in r.get("rationale") or []],
            "rejected": r.get("alternatives_rejected") or [], "depends_on": [], "dependents": [],
            "supersedes": None, "superseded_by": None, "restores": [], "history": [rid], "warnings": [],
            "notes": ["You marked this as not a decision. It's hidden from the graph and has no links."],
            "confirmed": False, "edited": False, "original": None, "decided_by": r.get("decided_by", "unknown"),
            "confidence": r.get("confidence"), "decided_at": (r.get("source") or {}).get("decided_at"),
            "session": str((r.get("source") or {}).get("session_id") or "")[:8],
        })
    return {
        "project": project.name,
        "project_path": str(project),
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "version": store_version(project),
        "nodes": nodes,
        "edges": edges,
    }


def plugin_version() -> str:
    try:
        return json.loads((Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    except Exception:
        return "?"


def store_version(project: Path) -> str:
    f = Store(project).records_path
    try:
        st = f.stat()
        return f"{st.st_mtime_ns}-{st.st_size}"
    except OSError:
        return "none"


def visit(project: Path) -> str | None:
    """Mark a visit: returns when the previous one was (for "new since your last visit") and logs it."""
    store = Store(project)
    prev = None
    try:
        with store:
            state = store.state()
            prev = state.get("view_seen_at")
            state["view_seen_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            store.save_state(state)
    except Exception:
        pass
    store.event("view")
    return prev


def render(data: dict, edit: dict | None = None) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    edit_js = json.dumps(edit) if edit else "null"
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return (template.replace("/*__DATA__*/null", payload).replace("/*__EDIT__*/null", edit_js)
            .replace("__TITLE__", _esc(data["project"])))


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _Site:
    """One project served by a hub: its folder, the access key in its link, and when it was last visited."""

    def __init__(self, project: Path, token: str | None = None):
        import secrets
        self.project, self.token, self.new_since = project, token or secrets.token_urlsafe(18), None


class _Hub:
    """Serves graph pages on 127.0.0.1 and accepts corrections from them.

    Every request needs the random access key from the page's link, writes must be JSON from the same origin, and
    the Host header must be this server's own address (so another web page can't reach it by DNS tricks). With
    prefixed=True one hub serves many projects, each under /p/<key>/ (the background service); otherwise it serves
    a single project at / (`dg view`)."""

    def __init__(self, port: int = 0, idle_minutes: float | None = 60, prefixed: bool = False, on_request=None):
        import http.server
        import re as _re
        import secrets
        import threading
        import time
        from urllib.parse import parse_qs, urlparse
        from dg_correct import apply_correction

        self.sites: dict[str, _Site] = {}
        self.prefixed, self.last = prefixed, time.time()
        outer = self

        def resolve(path):
            """(site, path within the site, redirect) for a request path."""
            if not outer.prefixed:
                return outer.sites.get(""), path, None
            m = _re.match(r"^/p/([A-Za-z0-9_.-]+)(/.*)?$", path)
            if not m or m.group(1) not in outer.sites:
                return None, path, None
            if m.group(2) is None:                     # /p/key -> /p/key/ so the page's relative links work
                return None, path, f"/p/{m.group(1)}/"
            return outer.sites[m.group(1)], m.group(2), None

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json", extra=None):
                data = body if isinstance(body, bytes) else body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _host_ok(self):
                port = outer.httpd.server_address[1]
                return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

            def do_GET(self):
                outer.last = time.time()
                if not self._host_ok():
                    return self._send(403, "wrong host", "text/plain")
                u = urlparse(self.path)
                if u.path == "/health":
                    return self._send(200, json.dumps({"service": "decision-graph", "projects": len(outer.sites)}))
                if on_request:
                    r = on_request(self, u)
                    if r is not None:
                        return self._send(*r)
                site, sub, redirect = resolve(u.path)
                if redirect:
                    return self._send(301, "", "text/plain", {"Location": redirect + ("?" + u.query if u.query else "")})
                tok = (parse_qs(u.query).get("t") or [""])[0]
                if not site or not (isinstance(tok, str) and secrets.compare_digest(tok, site.token)):
                    return self._send(403, "Open the link that `dg view` printed.", "text/plain")
                if sub == "/api/version":
                    return self._send(200, json.dumps({"v": store_version(site.project)}))
                if sub == "/":
                    site.new_since = visit(site.project)
                    data = build_data(Store(site.project).records(), site.project, site.new_since)
                    return self._send(200, render(data, {"token": site.token, "live": True}), "text/html")
                if sub == "/api/data":
                    data = build_data(Store(site.project).records(), site.project, site.new_since)
                    return self._send(200, json.dumps(data, ensure_ascii=False))
                return self._send(404, "not found", "text/plain")

            def do_POST(self):
                outer.last = time.time()
                if not self._host_ok():
                    return self._send(403, '{"ok": false, "message": "wrong host"}')
                u = urlparse(self.path)
                site, sub, _ = resolve(u.path)
                if not site or sub != "/api/correct":
                    return self._send(404, '{"ok": false, "message": "not found"}')
                origin = self.headers.get("Origin")
                if origin and origin != f"http://{self.headers.get('Host', '')}":
                    return self._send(403, '{"ok": false, "message": "wrong origin"}')
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    return self._send(415, '{"ok": false, "message": "JSON only"}')
                try:
                    n = min(int(self.headers.get("Content-Length") or 0), 100_000)
                    body = json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    return self._send(400, '{"ok": false, "message": "bad request"}')
                tok = body.get("t")
                if not (isinstance(tok, str) and secrets.compare_digest(tok, site.token)):
                    return self._send(403, '{"ok": false, "message": "bad token"}')
                ok, msg, rec = apply_correction(site.project, str(body.get("target", "")), str(body.get("action", "")),
                                                body.get("text"), body.get("note"))
                data = build_data(Store(site.project).records(), site.project, site.new_since)
                return self._send(200 if ok else 400, json.dumps({"ok": ok, "message": msg, "data": data}, ensure_ascii=False))

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

        if idle_minutes:
            def idle_watch():
                while True:
                    time.sleep(20)
                    if time.time() - outer.last > idle_minutes * 60:
                        print(f"\nNo activity for {idle_minutes} minutes; stopping.")
                        self.httpd.shutdown()
                        return
            threading.Thread(target=idle_watch, daemon=True).start()

    def add(self, key: str, project: Path, token: str | None = None) -> _Site:
        site = self.sites.get(key)
        if not site or site.project != project:
            site = self.sites[key] = _Site(project, token)
        return site

    def url_for(self, key: str) -> str:
        site = self.sites[key]
        return f"{self.base}/p/{key}/?t={site.token}" if self.prefixed else f"{self.base}/?t={site.token}"


class _Server(_Hub):
    """One project at / with an idle timeout: what `dg view` runs."""

    def __init__(self, project: Path, port: int = 0, idle_minutes: int = 60):
        super().__init__(port, idle_minutes, prefixed=False)
        self.project = project
        self.token = self.add("", project).token
        self.url = self.url_for("")

    @property
    def new_since(self):
        return self.sites[""].new_since


def _live_file(project: Path) -> Path:
    return Store(project).dir / "live.json"


def write_live(project: Path, url: str, service: bool = False) -> None:
    """Record where this project's live page is, so `dg view` and Claude (also in Cowork) can open it."""
    live = _live_file(project)
    try:
        live.parent.mkdir(parents=True, exist_ok=True)
        # the link carries the page's access key: keep it out of git even if the decisions folder is committed
        ign = live.parent / ".gitignore"
        lines = ign.read_text(encoding="utf-8").splitlines() if ign.exists() else []
        if "live.json" not in lines:
            ign.write_text("\n".join(lines + ["live.json"]) + "\n", encoding="utf-8")
        live.write_text(json.dumps({"url": url, "pid": os.getpid(), "service": service,
                                    "started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                    "note": ("Written by the decision-graph background service" if service else "Written by `dg view` while it runs")
                                            + "; open the url in a browser on this computer."}), encoding="utf-8")
    except OSError:
        pass


def running_live(project: Path) -> str | None:
    """The link of a live page already being served for this project, if its server still answers."""
    import urllib.request
    try:
        url = json.loads(_live_file(project).read_text(encoding="utf-8"))["url"]
        base, tok = url.split("/?t=")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{base}/api/version?t={tok}", timeout=2) as r:
            if r.status == 200:
                return url
    except Exception:
        pass
    return None


def serve(project: Path, port: int = 0, open_browser: bool = True, banner: str | None = None) -> int:
    srv = _Server(project, port)
    live = _live_file(project)
    write_live(project, srv.url)
    if banner:
        print(banner)
    print(f"Decision graph (live): {srv.url}", flush=True)
    print("It updates live as decisions are recorded, and corrections you make on the page are saved right away.")
    print("Press Ctrl+C here to stop. It also stops by itself after an hour without use.", flush=True)
    if open_browser:
        webbrowser.open(srv.url)
    try:
        srv.httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        srv.httpd.server_close()
        try:
            if json.loads(live.read_text(encoding="utf-8")).get("pid") == os.getpid():
                live.unlink()
        except Exception:
            pass
    return 0


def main(argv=None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--static", action="store_true", help="write the page and exit (no live updates, no corrections)")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--live", action="store_true",
                    help="serve the live page even without a terminal (for running in the background); "
                         "reuses the one already running for this project")
    a = ap.parse_args(argv)
    project = (a.project or project_dir()).resolve()
    store = Store(project)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if a.live or (not a.static and not a.out and interactive):
        existing = running_live(project)
        if existing:
            print(f"Decision graph (live, already running): {existing}", flush=True)
            if not a.no_open:
                webbrowser.open(existing)
            return 0
        return serve(project, a.port, not a.no_open)
    data = build_data(store.records(), project, visit(project))
    out = a.out or store.dir / "graph.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data), encoding="utf-8")
    active = sum(1 for n in data["nodes"] if n["status"] not in ("replaced", "rejected"))
    if not data["nodes"]:
        print(f"Wrote {out}. No decisions recorded yet: they appear after the first /compact in a Claude Code "
              f"session in this project. Try `dg demo` to see an example.")
    else:
        print(f"Wrote {out} ({active} active, {len(data['nodes']) - active} replaced or rejected) "
              f"with decision-graph {plugin_version()} from {Path(__file__).resolve().parent.parent}")
    if not a.no_open:
        webbrowser.open(out.resolve().as_uri())
    return 0


def demo(argv=None) -> int:
    """Open an example graph (a notes app's decisions) in a throwaway project, so you can try everything."""
    import shutil
    import tempfile
    utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--static", action="store_true")
    a = ap.parse_args(argv)
    fixture = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "notes_app.jsonl"
    proj = Path(tempfile.mkdtemp(prefix="dg-demo-")) / "notes-app (example)"
    (proj / ".claude" / "decisions").mkdir(parents=True)
    shutil.copy(fixture, proj / ".claude" / "decisions" / "decisions.jsonl")
    if a.static or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return main(["--project", str(proj), "--static"] + (["--no-open"] if a.no_open else []))
    return serve(proj, 0, not a.no_open,
                 banner="This is an example: the decisions from building a small notes app. Try selecting D-2 or D-7,\n"
                        "or correcting something. It's a throwaway copy; your own projects open with `dg view`.")


TEMPLATE_PATH = Path(__file__).resolve().parent / "graph_template.html"


if __name__ == "__main__":
    sys.exit(main())
