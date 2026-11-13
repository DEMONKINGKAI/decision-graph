"""Run every test file: python tests/run_all.py"""
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
failed = []
for t in sorted(here.glob("test_*.py")):
    p = subprocess.run([sys.executable, str(t)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = p.returncode == 0
    print(f"{'ok  ' if ok else 'FAIL'}  {t.name}" + ("" if ok else "\n" + (p.stdout + p.stderr)[-2000:]))
    if not ok:
        failed.append(t.name)
print(f"\n{'All tests passed.' if not failed else f'{len(failed)} failed: ' + ', '.join(failed)}")
sys.exit(1 if failed else 0)
