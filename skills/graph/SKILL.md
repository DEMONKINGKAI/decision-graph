---
name: graph
description: Show this project's decision graph as the plugin's interactive page. Use whenever the user asks to see, show, open, draw or visualize the decisions or the decision graph, in Claude Code, Cowork or Chat. Always use this instead of drawing a diagram of the decisions yourself.
---

# Show the decision graph

**Always show the plugin's own page**, live or as a snapshot, using the steps below. Never draw the decisions
yourself as an inline diagram, SVG, Mermaid chart or widget: a hand-drawn version leaves out what the page shows
(stale and replaced decisions, reasons, areas, the timeline) and is easy to get wrong.

Open the live page. It updates by itself as decisions are recorded, and the user can correct decisions with
buttons on it. It's served from the user's own machine (127.0.0.1 only, behind a random link token).

Run this **in the background** (your Bash tool's background option), because it keeps running to serve the page:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/dg.py" view --live
```

(Use `python3` if `python` is not found.) It opens the page in the user's browser and prints a line starting
with `Decision graph (live` followed by the link; read the background task's output to get it. If a live page
is already running for this project, it reuses that one, prints its link and exits; that's expected.

Give the user the link in case the browser didn't open. Mention once that the page stays live while this session
is open, and stops by itself after an hour without use.

## In Cowork (or anywhere your commands run in your own sandbox)

A live page you start in your sandbox can't be reached from the user's computer, so don't start one. Instead:

1. Look for `<project folder>/.claude/decisions/live.json` in the user's attached folder. It holds the link (`url`)
   to a live page running **on the user's computer**: from the background service (`"service": true`, always on
   once installed) or from a `dg view` they started.
2. If it's there and you have the built-in browser's tool for opening a page (`preview_start` or `navigate`), open
   that `url` in the browser pane: it runs on the user's computer, so it reaches the page. If the page loads
   (its header says "Live"), that's the live graph, with updates and correction buttons. Tell the user so.
3. If there's no `live.json`, or the page doesn't load, make the snapshot below and add one line: *"For the live
   graph here, open a terminal in this project's folder on your computer and run `dg service install` (once; it then
   starts at login and serves every project you've run it or `dg` in), and ask me again in a new conversation; I'll
   open it in this pane."* A project used only through Cowork isn't known to the service until a `dg` command has run
   in its folder on the user's computer, which is why the folder matters.

## In Chat (you can't reach the user's project folder)

You can't open their live page, but you can build the real page in your own workspace if you have the decisions:

1. Get them, in this order of preference:
   - the user attaches `<project>/.claude/decisions/decisions.jsonl` (ask for it: it's the whole graph);
   - decisions you wrote in this conversation with `/decision-graph:save` (the JSON block you gave them).
   If neither is available, ask for the file instead of drawing anything.
2. Make a temporary project named after the real one (the page shows that name as its title) and render it:

```bash
mkdir -p "/tmp/dg-show/<project name>/.claude/decisions"
cp "<attached decisions.jsonl>" "/tmp/dg-show/<project name>/.claude/decisions"/decisions.jsonl     # if they attached the file
python "${CLAUDE_PLUGIN_ROOT}/scripts/dg.py" --project "/tmp/dg-show/<project name>" add --file "<json block saved to a file>"   # if from a save block
python "${CLAUDE_PLUGIN_ROOT}/scripts/dg.py" --project "/tmp/dg-show/<project name>" view --static --no-open --out /tmp/dg-show/graph.html
```

   (Use `python3` if `python` isn't found; if `${CLAUDE_PLUGIN_ROOT}` is empty, the scripts are two levels up from
   this skill file.)
3. Give the user `graph.html` as a file (or publish it as a page if you can), and say in one line that it's a
   snapshot of what they gave you: the live page comes from `dg view` or the background service on their computer.

## Snapshot

Also use it if you can't run commands in the background, or the live command fails:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/dg.py" view --static
```

It writes `.claude/decisions/graph.html` and opens it. Tell the user in one line that this page is a snapshot that
won't update, and that running `dg view` in their own terminal opens the live version.

Then tell the user in a sentence or two what the graph shows right now: how many decisions are in force, and
which ones are stale or indirectly affected, if any (`dg.py stale` gives you this). Don't describe the page's
controls unless asked; the page explains itself.

If no decisions are recorded yet, explain that they're captured automatically at the next `/compact` in this
project; the live page will show them as they arrive.
