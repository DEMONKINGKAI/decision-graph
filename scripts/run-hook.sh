#!/bin/sh
# Runs one of the plugin's hook scripts with a working Python 3.9+, whatever it's called on this computer.
#
#   run-hook.sh <script.py> [args...]
#
# Tries, in order: $DECISION_GRAPH_PYTHON, a cached choice, python3, python, py -3. Skips the Microsoft Store
# placeholder (…\WindowsApps\python*.exe) and macOS's /usr/bin/python3 when the developer tools that back it
# aren't installed (running it would pop up an install dialog). With no Python at all, the session-start hook
# tells the user once per day what to install; every other hook does nothing, so Claude Code keeps working.
script="$1"
[ -n "$script" ] || exit 0
shift

cache_dir="${HOME:-$USERPROFILE}/.decision-graph"
cache="$cache_dir/python"

usable() {   # usable <command...>: is this a real Python 3.9+?
  case "$1" in
    *WindowsApps*) return 1 ;;
  esac
  if [ "$1" = /usr/bin/python3 ] && [ "$(uname -s 2>/dev/null)" = Darwin ]; then
    xcode-select -p >/dev/null 2>&1 || return 1
  fi
  "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' </dev/null >/dev/null 2>&1
}

found=""
if [ -n "$DECISION_GRAPH_PYTHON" ] && usable "$DECISION_GRAPH_PYTHON"; then
  found="$DECISION_GRAPH_PYTHON"
elif [ -f "$cache" ]; then
  c="$(cat "$cache" 2>/dev/null)"
  [ -n "$c" ] && [ -e "$c" ] && found="$c"     # checked when cached; existence is enough on the fast path
fi
if [ -z "$found" ]; then
  for name in python3 python; do
    p="$(command -v "$name" 2>/dev/null)" || continue
    if usable "$p"; then found="$p"; break; fi
  done
  if [ -n "$found" ]; then
    mkdir -p "$cache_dir" 2>/dev/null && printf '%s' "$found" > "$cache" 2>/dev/null
  fi
fi
if [ -n "$found" ]; then
  exec "$found" "$script" "$@"
fi
if command -v py >/dev/null 2>&1 && usable py -3; then   # Windows launcher (not cached: it needs the -3)
  exec py -3 "$script" "$@"
fi

# No Python. Say so at session start (at most once a day), stay silent everywhere else.
case "$script" in
  *inject_decisions.py)
    stamp="$cache_dir/no-python-notice"
    today="$(date +%Y-%m-%d 2>/dev/null)"
    if [ "$(cat "$stamp" 2>/dev/null)" != "$today" ]; then
      mkdir -p "$cache_dir" 2>/dev/null && printf '%s' "$today" > "$stamp" 2>/dev/null
      case "$(uname -s 2>/dev/null)" in
        Darwin) how="Install it with: xcode-select --install  (or from python.org), then start a new session." ;;
        Linux)  how="Install python3 with your package manager (for example: sudo apt install python3), then start a new session." ;;
        *)      how="Install it from python.org (tick \\\"Add python.exe to PATH\\\") or run: winget install Python.Python.3.12  — then restart Claude." ;;
      esac
      printf '{"systemMessage": "Decision graph is installed but can'"'"'t run: it needs Python 3.9 or newer, which wasn'"'"'t found. %s"}\n' "$how"
    fi
    ;;
esac
exit 0
