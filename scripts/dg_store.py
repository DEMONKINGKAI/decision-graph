"""Shared storage + graph helpers for the decision-graph plugin (stdlib only, Windows/macOS/Linux)."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

GUARD_ENV = "DECISION_GRAPH_EXTRACTING"  # set inside our own nested `claude -p` call

if os.name == "nt":
    import msvcrt

    def _lock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # retries ~10s, then raises

    def _unlock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(f):
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock(f):
        fcntl.flock(f, fcntl.LOCK_UN)


USER_HOME = Path.home() / ".decision-graph"


def find_claude() -> str | None:
    """The `claude` program for extraction calls. PATH first; then the one running this hook (the desktop app
    ships its own, not on PATH); then the last one a hook saw (so `dg` in a terminal finds it too)."""
    import shutil
    env = os.environ.get("DECISION_GRAPH_CLAUDE_BIN")
    if env:
        return env
    remembered = USER_HOME / "claude_path"
    running = os.environ.get("CLAUDE_CODE_EXECPATH")
    if running and Path(running).is_file() and Path(running).name.lower().startswith("claude"):
        try:
            if not remembered.exists() or remembered.read_text(encoding="utf-8").strip() != running:
                USER_HOME.mkdir(parents=True, exist_ok=True)
                remembered.write_text(running, encoding="utf-8")
        except OSError:
            pass
    found = shutil.which("claude")
    if found:
        return found
    if running and Path(running).is_file() and Path(running).name.lower().startswith("claude"):
        return running
    try:
        r = remembered.read_text(encoding="utf-8").strip()
        if r and Path(r).is_file():
            return r
    except OSError:
        pass
    for c in (Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude"),
              Path.home() / ".claude" / "local" / ("claude.exe" if os.name == "nt" else "claude")):
        if c.is_file():
            return str(c)
    return None


def utf8_stdio() -> None:
    """Windows consoles default to cp1252; hooks exchange UTF-8."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def read_stdin_json() -> dict:
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
    return json.loads(raw) if raw else {}


def clean_topic(t) -> str | None:
    """An area label: 1-3 lowercase words, e.g. "narrator", "vector storage"."""
    if not isinstance(t, str):
        return None
    t = re.sub(r"[^a-z0-9 &/+.-]", "", t.strip().lower())
    t = " ".join(t.split()[:3])[:32].strip()
    return t or None


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def project_dir(payload: dict | None = None) -> Path:
    payload = payload or {}
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd())


class Store:
    def __init__(self, project: Path):
        self.dir = project / ".claude" / "decisions"
        self.records_path = self.dir / "decisions.jsonl"
        self.state_path = self.dir / "state.json"
        self.log_path = self.dir / "extract.log"
        self._lock_file = None

    # -- locking (writers only) --
    def __enter__(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(self.dir / ".lock", "a+")
        _lock(self._lock_file)
        return self

    def __exit__(self, *exc):
        try:
            _unlock(self._lock_file)
        finally:
            self._lock_file.close()

    def log(self, msg: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {msg}\n")

    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    def event(self, kind: str, **data) -> None:
        """Append one usage event (warnings fired, page opens, extractions) for `dg report`. Never raises."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.events_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps({"at": now_iso(), "kind": kind, **data}, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        out = []
        for line in self.events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    def records(self) -> list[dict]:
        if not self.records_path.exists():
            return []
        out = []
        for line in self.records_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # a half-written last line is ignored, never fatal
        return out

    def append(self, recs: list[dict]) -> None:
        # If a previous write was cut off mid-line, start on a fresh line so the
        # new records don't get glued onto (and lost with) the broken one.
        needs_nl = False
        if self.records_path.exists() and self.records_path.stat().st_size > 0:
            with open(self.records_path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                needs_nl = f.read(1) != b"\n"
        with open(self.records_path, "a", encoding="utf-8", newline="\n") as f:
            if needs_nl:
                f.write("\n")
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.log("state.json corrupt, starting fresh")
        return {"cursors": {}}

    def save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)


# ------------------------------------------------------------ graph views
class Graph:
    """Decisions with three edge types, plus amendments.

    supersedes  (time)           D-4 replaces D-1
    restores    (time)           D-14 brings D-1's content back into force
    depends_on  (justification)  D-2 was chosen because of D-1
    Amendments (A-n) add reasons to an existing decision without replacing it.
    """

    def __init__(self, records: list[dict]):
        self.by_id: dict[str, dict] = {}
        self.amendments: dict[str, list[dict]] = {}
        self.superseded_by: dict[str, str] = {}
        self.dependents: dict[str, list[str]] = {}
        self.rejected: dict[str, dict] = {}       # decisions the user said aren't decisions
        self.corrections: dict[str, list[dict]] = {}
        self.topics: dict[str, str] = {}          # decision id -> area, from extraction or `dg topics`
        verdict, edits = corrections_state(records)
        for r in records:
            if not r.get("id"):
                continue
            kind = r.get("kind")
            if kind == "amendment":
                self.amendments.setdefault(r.get("amends"), []).append(r)
                continue
            if kind == "correction":
                self.corrections.setdefault(r.get("target"), []).append(r)
                continue
            if kind == "topics":                  # a later tagging overrides an earlier one
                self.topics.update({k: clean_topic(v) for k, v in (r.get("map") or {}).items() if clean_topic(v)})
                continue
            if kind:
                continue                          # a record type this version doesn't know
            rid = r["id"]
            if rid in edits or verdict.get(rid):
                r = dict(r)
                if rid in edits:
                    r["original_decision"], r["decision"] = r["decision"], edits[rid]
                    r["edited"] = True
                if verdict.get(rid) == "confirmed":
                    r["confirmed"], r["confidence"] = True, 1.0
            if verdict.get(rid) == "rejected":
                self.rejected[rid] = r
                continue
            self.by_id[rid] = r
        for rid, r in self.by_id.items():
            t = self.topics.get(rid) or clean_topic(r.get("topic"))
            if t:
                self.topics[rid] = t
        for r in self.by_id.values():
            sup = r.get("supersedes")
            if isinstance(sup, list):  # hand-edited files sometimes list it; the schema is one id or null
                r["supersedes"] = sup = next((x for x in sup if isinstance(x, str)), None)
            if not isinstance(sup, str):
                r["supersedes"] = sup = None
            if not isinstance(r.get("depends_on"), list):
                r["depends_on"] = []
            if sup in self.by_id:
                self.superseded_by[r["supersedes"]] = r["id"]
            for d in r.get("depends_on") or []:
                if d in self.by_id:
                    self.dependents.setdefault(d, []).append(r["id"])

    # -- basic views
    def is_active(self, rid: str) -> bool:
        return rid in self.by_id and rid not in self.superseded_by

    def active(self) -> list[dict]:
        return [r for rid, r in self.by_id.items() if self.is_active(rid)]

    def replaced(self) -> list[dict]:
        return [r for rid, r in self.by_id.items() if not self.is_active(rid)]

    def rationale(self, rid: str) -> list[tuple[str, str]]:
        """(reason, where it came from): own reasons first, then amendments in order."""
        r = self.by_id.get(rid, {})
        out = [(x, rid) for x in r.get("rationale") or []]
        for a in self.amendments.get(rid, []):
            out += [(x, a["id"]) for x in a.get("rationale") or []]
        return out

    def current(self, rid: str) -> str:
        """Follow supersession forward to the version in force."""
        seen = set()
        while rid in self.superseded_by and rid not in seen:
            seen.add(rid)
            rid = self.superseded_by[rid]
        return rid

    def history(self, rid: str) -> list[str]:
        """Full chain oldest -> newest that contains rid."""
        back, cur = [], rid
        rev = {v: k for k, v in self.superseded_by.items()}
        while cur in rev and cur not in back:
            cur = rev[cur]
            back.append(cur)
        chain = list(reversed(back)) + [rid]
        cur = rid
        while cur in self.superseded_by and self.superseded_by[cur] not in chain:
            cur = self.superseded_by[cur]
            chain.append(cur)
        return chain

    # -- justification health
    def holds(self, rid: str) -> bool:
        """Is what `rid` says currently in force, either directly or because a later decision restored it?"""
        if self.is_active(rid):
            return True
        now = self.current(rid)
        return self.is_active(now) and rid in (self.by_id[now].get("restores") or [])

    def stale_deps(self, rid: str) -> list[tuple[str, str]]:
        """(dep, replaced_by) pairs where a decision rests on something no longer in force."""
        r = self.by_id.get(rid, {})
        return [(d, self.current(d)) for d in r.get("depends_on") or []
                if d in self.by_id and not self.holds(d)]

    def restored_deps(self, rid: str) -> list[tuple[str, str]]:
        """(dep, restored_by): the reason was replaced, then brought back."""
        r = self.by_id.get(rid, {})
        return [(d, self.current(d)) for d in r.get("depends_on") or []
                if d in self.by_id and not self.is_active(d) and self.holds(d)]

    def indirectly_stale(self, rid: str) -> list[tuple[str, list[str]]]:
        """(stale ancestor, path from rid to it) for active ancestors that themselves rest on a dead reason."""
        out, seen = [], {rid}
        frontier = [(rid, [rid])]
        while frontier:
            nxt = []
            for cur, path in frontier:
                for d in self.by_id.get(cur, {}).get("depends_on") or []:
                    if d in seen or d not in self.by_id:
                        continue
                    seen.add(d)
                    if self.holds(d):
                        p2 = path + [d]
                        if self.stale_deps(d):
                            out.append((d, p2))
                        nxt.append((d, p2))
            frontier = nxt
        return out

    def why(self, rid: str, depth: int = 0, seen=None) -> list[tuple[int, str]]:
        seen = seen if seen is not None else set()
        out = []
        for d in self.by_id.get(rid, {}).get("depends_on") or []:
            if d in seen:
                continue
            seen.add(d)
            out.append((depth, d))
            out.extend(self.why(d, depth + 1, seen))
        return out

    def impact(self, rid: str) -> list[tuple[int, str]]:
        """Everything that transitively rests on rid (BFS, with distance)."""
        out, seen, frontier, dist = [], {rid}, [rid], 1
        while frontier:
            nxt = []
            for cur in frontier:
                for dep in self.dependents.get(cur, []):
                    if dep not in seen:
                        seen.add(dep)
                        out.append((dist, dep))
                        nxt.append(dep)
            frontier, dist = nxt, dist + 1
        return out


def corrections_state(records: list[dict]) -> tuple[dict, dict]:
    """Replay correction records in order: (verdict per decision, edited text per decision).
    verdict is "confirmed", "rejected" or absent; the latest confirm/reject wins, the latest edit wins."""
    verdict, edits = {}, {}
    for r in records:
        if r.get("kind") != "correction" or not r.get("target"):
            continue
        a = r.get("action")
        if a == "reject":
            verdict[r["target"]] = "rejected"
        elif a == "confirm":
            verdict[r["target"]] = "confirmed"
        elif a == "edit" and str(r.get("text", "")).strip():
            edits[r["target"]] = str(r["text"]).strip()
    return verdict, edits


def rejected_ids(records: list[dict]) -> set[str]:
    return {k for k, v in corrections_state(records)[0].items() if v == "rejected"}


def id_num(rid: str) -> int:
    m = re.fullmatch(r"D-(\d+)", rid or "")
    return int(m.group(1)) if m else 0
