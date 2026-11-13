---
name: setup
description: Set up or repair the decision-graph plugin on this computer. Use when the user asks to set it up, after installing it, or when decisions aren't being recorded, the status line never appears, or a notice says extraction failed.
---

# Set up decision-graph

Run the setup in a way the user can watch, and answer its questions with them:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/dg.py" setup
```

If `python` isn't found, run the same command with `python3` (macOS and Linux) or `py` (Windows). If none of
them exists, Python is what's missing: tell the user to install Python 3.9+ (Windows: `winget install
Python.Python.3.12`, or python.org with "Add python.exe to PATH"; macOS: `xcode-select --install` or python.org;
Linux: their package manager) and to restart Claude Code. The hooks find Python on their own once it's there.

If you can't answer its yes/no questions interactively, run it with `--yes` only after the user agrees to what it
will do: on Windows it may install Git for Windows with winget (the hooks run through Git Bash), and it turns on
the live graph in the background, starting at login. It always installs the `dg` command and finishes with a
health check.

Then tell the user, in two or three sentences:

- what was fixed or installed,
- anything still marked FAIL in the health check and the fix it suggests,
- what happens next: decisions are recorded when a conversation is compacted, each session starts with a one-line
  status, `dg view` shows the graph (it updates live), and `dg demo` shows an example right away.

If a new terminal is needed for the `dg` command or a PATH change, say so.
