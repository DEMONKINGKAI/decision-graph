---
name: save
description: Save the decisions made in this conversation into a project's decision graph. Use in Chat or Cowork (where there is no /compact to record them automatically) when the user asks to save, record or keep the decisions from this chat, or types /decision-graph:save.
---

# Save this conversation's decisions

Chat and Cowork have no `/compact`, so the decision graph never records their conversations by itself. This skill
does it on request: you pull the decisions out of this conversation and write them into the project's graph
with `dg add`, which applies the same checks as automatic recording.

## 1. Work out where you are

- **You can run commands and the project folder is available** (Cowork with the project folder attached, or
  Claude Code): use the *write* route below. The graph lives in `<project folder>/.claude/decisions/`. If more
  than one folder is attached, ask which project the decisions belong to.
- **Cowork without the project folder attached:** before anything else, tell the user plainly:
  *"To save decisions from Cowork I need access to the project's folder. Attach the folder (for example
  `Threadfall`) to this conversation, then ask me again."* Then offer the *copy* route as the alternative for now.
- **Plain Chat** (you can't reach the user's files): use the *copy* route. Say in one line that Chat can't write
  to their project, so they'll run one command themselves.

## 2. See what's already recorded (write route)

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/dg.py" --project "<project folder>" active
```

Use `python3` if `python` isn't found. If `${CLAUDE_PLUGIN_ROOT}` is empty, the scripts folder is two levels up
from this skill file (`../../scripts/dg.py`). Use the ids it lists (D-n) when a decision in this conversation
replaces, rests on, or adds a reason to one of them. In the copy route you can't run this; ask the user to paste
the output of `dg active` if the conversation changes earlier decisions, otherwise leave links to existing ids out.

## 3. Pull out the decisions

A decision is a choice that constrains future work (a technology, architecture, design, scope, naming, process
or requirement) that was **agreed**: by the user, or proposed by you and accepted by the user. Follow these rules,
the same ones the automatic recorder uses:

- **Not decisions:** options only discussed, your suggestions the user didn't accept, brainstorming, debugging
  steps, facts about the code, and the order work is done in ("do X first"). Choosing *what* is in scope is a decision.
- **One choice per record.** Split "Vitest for unit tests and Playwright for e2e" into two. Several changes of
  the same kind made together (three verb mappings) are one record.
- **Reversals:** a change of an earlier decision is its own record whose `supersedes` names the decision it
  replaces (an existing `D-n`, or the `key` of one from this list). Widening an earlier decision's scope is one
  record with the final scope that supersedes it. If it brings back something replaced before, also list that in `restores`.
- **Rests on (`depends_on`):** it was chosen because of that decision, or only makes sense while that one holds
  (it configures, tunes or extends what that decision set up). Never link to a work plan or task list just
  because something was built under it.
- **Added reasons only:** if the user only gives a new reason for an existing decision, set `amends` to its id,
  put the reason in `rationale`, and restate the decision.
- **Reasons:** only reasons someone actually gave, paraphrased briefly. If none was given, use `[]`. Never
  invent a motive or write where it came from ("stated by the user").
- **Who decided:** `"user"` if the user proposed it, `"assistant_accepted"` if you proposed it and the user agreed
  (including "sounds good", "do that").
- **Area (`topic`):** 1 to 3 lowercase words for the part of the project (narrator, storage, frontend style).
  Reuse areas already in the graph when they fit.
- Prefer fewer, clearly agreed records. If this conversation was saved before, only take decisions made after
  that save (you can see the earlier save above).

Format: a JSON array, one object per decision:

```json
[{"key": "n1", "decision": "One self-contained sentence, no ids in it.", "rationale": ["..."],
  "alternatives_rejected": ["..."], "supersedes": "D-12", "depends_on": ["D-3", "n2"], "restores": [],
  "amends": null, "topic": "storage", "decided_by": "user", "confidence": 0.9}]
```

## 4a. Write route

Write the array to a temporary file, then:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/dg.py" --project "<project folder>" add --file "<temp file>" --session "<3 to 6 words on what this conversation was about>"
```

**Make sure it lands on the user's computer.** If you ran `dg add` on a copy of the project inside your own
workspace (a sandbox path, not the folder the user attached), the decisions exist only in that copy until you
write them back. Copy every file in the copy's `.claude/decisions/` folder to the same place in the user's own
folder, using your tools for writing to their files, and check `decisions.jsonl` arrived (same size, or same line count).
Only then say they're saved, and name the folder they're in. If you can't write back, say so plainly: the save
isn't kept, and the copy route below is the way to get them into the project.

Tell the user in two or three lines what was added (ids and what they say), anything it now replaces or makes
stale (the command prints both), and that `dg reject <id>` undoes a wrong one.

If the decision graph was opened earlier in this conversation as a snapshot (its header says "Snapshot from"),
it won't show what you just saved. Refresh it so it does (from the user's folder, or your copy after writing back):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/dg.py" --project "<project folder>" view --static
```

and open the page again the same way as before. A live page (header "Live") updates by itself; leave it alone.

## 4b. Copy route

Give the array in one ```json code block, then say: copy it, and in a terminal in the project folder run

```
dg add --clipboard --session "<3 to 6 words on what this conversation was about>"
```

`dg add --clipboard` works in PowerShell, macOS and Linux. If the clipboard can't be read there, saving the block
as `decisions.json` and running `dg add --file decisions.json` does the same.

If nothing in the conversation counts as a decision, say so in one line instead of saving an empty list.
