---
name: record
description: Record this project's decisions now, without waiting for the conversation to be compacted. Use when the user asks to record, save or capture the decisions so far, or wants a decision they just made to show up in the decision graph right away.
---

# Record decisions now

Run:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/dg.py" record
```

(Use `python3` if `python` is not found.) It reads every conversation in this project that has turns not yet
recorded, including this one, and adds the decisions it finds. It takes about 5 to 30 seconds per conversation.

Then tell the user in one or two lines what was recorded: the new decision ids and what each says, and which
earlier decisions they replace, if any. If a live decision graph page is open, it shows them by itself.

Decisions are also recorded automatically when a conversation is compacted and when a session ends, so this is
only needed when the user wants them right away.
