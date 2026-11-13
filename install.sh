#!/bin/sh
# Decision graph installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/DEMONKINGKAI/neuro-graph/main/install.sh | sh
#
# or, from a local copy:   sh install.sh /path/to/neuro-graph
#
# Checks Claude Code and Python, installs the plugin from its marketplace, installs the `dg` command and
# runs a health check. Safe to run again: it updates an existing install.
set -e
SOURCE="${1:-DEMONKINGKAI/neuro-graph}"
step() { printf '\n\033[36m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mok\033[0m  %s\n' "$1"; }
die()  { printf '  \033[33m!!\033[0m  %s\n' "$1"; exit 1; }
export PATH="$HOME/.local/bin:$PATH"

echo "Installing decision-graph"

step "1. Claude Code"
if ! command -v claude >/dev/null 2>&1; then
  printf '  Claude Code isn'"'"'t installed. Install it now? [Y/n] '
  read -r a < /dev/tty || a=n
  case "$a" in ""|y|Y|yes) curl -fsSL https://claude.ai/install.sh | bash ;; esac
  command -v claude >/dev/null 2>&1 || die "Install Claude Code first: https://code.claude.com/docs/en/setup"
fi
ok "claude $(claude --version)"

step "2. Python"
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || die "Python 3.9+ is needed. macOS: brew install python  Ubuntu/Debian: sudo apt install python3"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9 or newer is needed ($("$PY" --version))."
ok "$("$PY" --version) at $PY"

step "3. Plugin"
claude plugin marketplace add "$SOURCE" >/dev/null 2>&1 || true
claude plugin marketplace update neuro-graph >/dev/null 2>&1 || true
if claude plugin list --json | "$PY" -c 'import json,sys; sys.exit(0 if any(p["id"].startswith("decision-graph@") for p in json.load(sys.stdin)) else 1)'; then
  claude plugin update decision-graph@neuro-graph
else
  claude plugin install decision-graph@neuro-graph
fi
DIR="$(claude plugin list --json | "$PY" -c 'import json,sys; print(next(p["installPath"] for p in json.load(sys.stdin) if p["id"].startswith("decision-graph@")))')"
[ -n "$DIR" ] || die "The plugin didn't install. Try: claude plugin install decision-graph@neuro-graph"
ok "installed in $DIR"

step "4. Setup"
"$PY" "$DIR/scripts/dg.py" setup --yes || true
printf '\nDone. Open a new terminal, then try:  dg demo\n'
