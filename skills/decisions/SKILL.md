---
name: decisions
description: Look up this project's recorded decisions — what was decided, why, what depends on it, and what replaced it. Use when the user asks why something was chosen, refers to an earlier agreement you can't see in context, proposes reversing or changing an earlier choice, or before making a change that might contradict a past decision.
---

# Decision graph

Decisions from earlier conversations in this project are extracted automatically when a conversation is compacted, and stored in `.claude/decisions/decisions.jsonl` at the project root. Each decision has an id (`D-12`), its reasons, the options it rejected, the decisions it **rests on** (`depends_on`), the decision it **replaced** (`supersedes`), and any earlier replaced decision it **brings back** (`restores`). Records are never edited: a reversal is a new decision that replaces the old one, and a reason added later is an **amendment** (`A-3`) attached to the decision it explains.

The decisions currently in force are usually already in your context from session start. Use this skill when you need more than that list.

## Querying

Run the query script with Python (`python` on Windows; use `python3` if `python` is not found):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/dg.py" <command> [args]
```

| Command | Use it when |
|---|---|
| `active` | You need the full list of decisions in force |
| `search <words>` | You need to find the id for a topic ("database", "auth") |
| `show <id>` | You need one decision's reasons, rejected options and source |
| `why <id>` | The user asks why something was chosen: follows the chain of decisions it rests on |
| `impact <id>` | Someone wants to change or undo a decision: lists everything that rests on it, directly or indirectly |
| `history <id>` | You need the earlier versions of a decision |
| `stale` | Finds decisions still in force whose justification was replaced: directly, or further up the chain. Also lists ones whose reason was replaced and later restored (these hold) |
| `view` | The user wants to see the graph: opens an interactive page in their browser |
| `mermaid` | You need the graph as Mermaid text, e.g. to put in a document |
| `confirm <id>` / `reject <id>` / `edit <id> "wording"` | Only when the user tells you a recorded decision is right, isn't a decision, or is worded wrong |
| `doctor` | Something seems broken: extraction failed, no decisions appear, or a notice says the last extraction failed |

## How to use the answers

- **Before reversing a decision**, run `impact` on it and tell the user what else would lose its justification. This is the main reason the graph exists.
- **When asked "why did we…"**, answer from `why`/`show`, quote the recorded reasons, and say which decision ids you used. If nothing is recorded, say so rather than reconstructing a reason.
- **Treat records as evidence, not ground truth.** They were extracted by a model; `confidence` and `decided_by` (`user` vs `assistant_accepted`) tell you how firm each one is. If a record contradicts what the user says now, the user's current statement wins. Point out the conflict once.
- **`stale` results** are worth raising at natural moments: a decision resting on a replaced decision may no longer hold.
- **Corrections are the user's call.** When the user says a record is wrong ("that wasn't a decision", "we never agreed to that", "it should say X"), apply it with `reject`, `edit` or `confirm`, adding `--note` with their reason, and tell them in one line what changed. Don't correct records on your own initiative: point out what looks wrong and let them decide. A rejection can be undone with `confirm`.
- **Decisions marked "[confirmed]" or "(confirmed by the user)" were checked by the user.** Trust them over unconfirmed ones when they conflict.
- **Reasons can come from amendments.** `show` marks them "added later"; cite them like any other reason.
- If no decisions are recorded yet, they appear after the first compaction (`/compact`). Don't create or edit records by hand: if the user wants something recorded, have them state it in the conversation and it will be captured at the next compaction.
- If the user asks to rebuild or repair the graph (for example after updating the plugin), give them `dg rebuild` (or `python "${CLAUDE_PLUGIN_ROOT}/scripts/dg.py" rebuild`) to run in their own terminal: it asks for confirmation and moves the old graph to a backup folder.
- If session context says the last extraction failed, tell the user once, briefly, and suggest `dg doctor`. Run `dg.py doctor --offline` yourself if they want you to look into it.
- **Decision-graph warnings** ("This uses X, which D-n decided against") come from the plugin, not the user. If you get one, stop and check: either pick an option that fits the decision, or tell the user the conflict in one line and ask whether the decision has changed. If they say it has, carry on; the new choice is recorded at the next compaction and replaces the old one.
