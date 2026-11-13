"""Run: python tests/test_eval.py  (the eval scorer gives perfect answers a perfect score; no network)"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    with tempfile.TemporaryDirectory() as d:
        p = subprocess.run([sys.executable, str(ROOT / "eval" / "run_eval.py"), "--mock-dir", str(ROOT / "eval" / "oracle"),
                            "--runs", "1", "--out", d], capture_output=True, text=True, encoding="utf-8")
        assert p.returncode == 0, p.stdout + p.stderr
        s = json.loads((Path(d) / "summary.json").read_text(encoding="utf-8"))["summary"]
        assert s["f1"] == 1.0 and s["trap_hits"] == 0 and s["links_ok"] == s["links"], s
        assert s["amend_ok"] == s["amend"] and s["dupes"] == 0 and s["attr_ok"] == s["attr"] and s["rej_ok"] == s["rej"], s
        # every case has a perfect answer, so a new case can't silently go unchecked
        cases = json.loads((ROOT / "eval" / "cases.json").read_text(encoding="utf-8"))["cases"]
        missing = [c["name"] for c in cases if not (ROOT / "eval" / "oracle" / f"{c['name']}.json").exists()]
        assert not missing, f"cases without a perfect answer in eval/oracle: {missing}"
    # the Tailwind change recorded as two decisions (as Haiku often does) is just as right as one
    with tempfile.TemporaryDirectory() as d:
        mock = Path(d) / "mock"
        mock.mkdir()
        (mock / "05_cross_session_changes.json").write_text(json.dumps([
            {"decision": "Use bun as the package manager.", "supersedes": "D-2", "decided_by": "user"},
            {"decision": "Paid services are allowed.", "supersedes": "D-3", "decided_by": "user"},
            {"decision": "Use Tailwind CSS only for the marketing site.", "supersedes": "D-1", "decided_by": "user"},
            {"decision": "Use CSS modules for styling the app.", "decided_by": "user"}]))
        out = Path(d) / "out"
        subprocess.run([sys.executable, str(ROOT / "eval" / "run_eval.py"), "--mock-dir", str(mock), "--cases", "05",
                        "--runs", "1", "--out", str(out)], capture_output=True, text=True, encoding="utf-8")
        s = json.loads((out / "summary.json").read_text(encoding="utf-8"))["summary"]
        assert s["f1"] == 1.0 and s["links_ok"] == s["links"], s
    print("all tests passed")


if __name__ == "__main__":
    main()
