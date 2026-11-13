# Decision Graph: a Claude Code plugin

Long Claude Code sessions get compacted, and new sessions start blank. The
decisions you made along the way, such as "SQLite, not Postgres" or "offline mode is a
hard requirement", slowly fall out of context. This plugin records those
decisions as a small graph and gives them back to Claude.

It's like a project's logbook combined with a dependency map. The logbook records what was
decided and when. The map records *why*: which decision rests on which. With
both, the plugin can answer "what breaks if we undo this?" and "which decisions
lost their reason?", which a plain summary can't.

## What it does

| Piece | When it runs | What it does |
|---|---|---|
| `PreCompact` and `SessionEnd` hooks | Right before a conversation is compacted (`/compact` or automatic), and when a session ends | Reads the part of the transcript it hasn't seen yet, asks Haiku to extract decisions, appends them to `.claude/decisions/decisions.jsonl` |
| `SessionStart` hook | New session, resume, `/clear`, and right after compaction | Puts the decisions currently in force into Claude's context, and shows you one status line: how many are in force, which are stale, what's new |
| `UserPromptSubmit` / `PreToolUse` hooks | Every message, and before Bash or file edits | Warns when something contradicts a decision, such as `npm install jest` after you chose Vitest over Jest. The first time per session, the command waits for your OK |
| `decisions` skill | When Claude needs more than that list | Claude queries the graph: `why`, `impact`, `history`, `stale`, `search` |
| `graph` skill | "Show me the decision graph" | Opens the interactive graph page |
| `dg` command | Whenever you want | The same queries, the live graph page, `demo`, `report`, `rebuild` and `doctor` from your terminal |

Everything is stored per project in `<project>/.claude/decisions/`.

## Install

You need **Claude Code** (logged in) and **Python 3.9+**. On Windows, also **Git for Windows**, which Claude Code
normally comes with: the plugin's hooks run through its Git Bash. No Python packages, no API key.

**In the Claude desktop app** (Code tab, Cowork): open the plugin browser, choose *Add marketplace* →
*from GitHub*, enter `DEMONKINGKAI/neuro-graph`, and install **decision-graph**. Updates come from the repository,
so there's nothing to re-upload.

**In Claude Code in a terminal:**

```
/plugin marketplace add DEMONKINGKAI/neuro-graph
/plugin install decision-graph@neuro-graph
```

That's all. At your first Claude Code session afterwards the plugin finishes setting itself up and tells you so in
one line: it installs the `dg` command (on Windows it also adds it to your PATH; open a new terminal to use it)
and starts the live graph in the background, which then starts at login (`dg service remove` turns it off;
`DECISION_GRAPH_NO_SERVICE=1` skips it). If Python is missing, the session tells you what to install instead of
failing on every hook.

**Or with the one-line installer**, which also checks and offers to install Claude Code, Python and Git for you:

```powershell
irm https://raw.githubusercontent.com/DEMONKINGKAI/neuro-graph/main/install.ps1 | iex      # Windows (PowerShell)
```

```bash
curl -fsSL https://raw.githubusercontent.com/DEMONKINGKAI/neuro-graph/main/install.sh | sh  # macOS / Linux
```

**From a local copy** of this folder: `powershell -ExecutionPolicy Bypass -File install.ps1 -Source C:\path\to\neuro-graph`
or `sh install.sh /path/to/neuro-graph`.

Then try it without waiting for a real session:

```bash
dg demo
```

opens an example project's graph (a notes app with 17 decisions, one stale), so you can click around. If anything
seems off, `dg doctor` checks every piece and says how to fix what isn't working; `dg setup` redoes the setup
interactively.

How the hooks find Python: they run `scripts/run-hook.sh`, which tries `python3`, `python` and `py -3` (or
`DECISION_GRAPH_PYTHON` if you set it), skips the Windows Store placeholder and macOS's stand-in that only offers
to install developer tools, and remembers what it found. Extraction calls `claude -p` with your login; when
`claude` isn't on PATH (the desktop app ships its own), the hooks use the one Claude is running from.

### Updating

Installed from GitHub, the plugin updates with the marketplace (the desktop app syncs it; in a terminal,
`claude plugin marketplace update neuro-graph` then `claude plugin update decision-graph@neuro-graph`). When
working on a local copy, bump `version` in `.claude-plugin/plugin.json` first, and restart Claude Code after
updating. `dg doctor` tells you when the running copy is out of date. The `dg` command always runs the newest copy
it finds, so it needs no reinstalling.

## Using it

Work normally. When a session is compacted you'll see *Recording decisions before compacting*
for a few seconds; after that, and in every new session, Claude starts with the decisions in force.
Ask things like *"why did we pick SQLite?"* or *"what would break if we drop offline mode?"*.

You'll see it working without opening anything:

- **A status line at the start of each session:** *Decision graph: 12 decisions in force, 1 stale (D-7). Run
  `dg view` to explore.* After a compaction it says how many decisions that conversation added.
- **Warnings when something contradicts a decision.** If you chose Vitest over Jest and Claude goes to run
  `npm install jest`, the command pauses with *This uses "jest", which D-9 decided against*. You approve
  or refuse it. The first time per session it asks; after that it only reminds Claude. Your own messages get a
  note to Claude too, plus the decisions related to what you asked about. Set `DECISION_GRAPH_GUARD` to `warn` (never
  pause, only remind Claude) or `off`.

From a terminal in the project folder:

| Command | What it shows |
|---|---|
| `dg` | Decisions in force, with warnings |
| `dg view` | The interactive graph in your browser, updating live |
| `dg demo` | An example graph to try things on |
| `dg report` | Two-week trial numbers: warnings fired, wrong records, how often you looked |
| `dg service install` | Keep the live graph running in the background for all projects, starting at login (`status`, `remove`) |
| `dg add --clipboard` | Record decisions saved from a Chat or Cowork conversation with `/decision-graph:save` (see below) |
| `dg record` | Record decisions now, without waiting for a compaction (also `/decision-graph:record` inside Claude Code) |
| `dg topics` | Group decisions into areas (narrator, storage, ...). New decisions get one when recorded; this tags older ones in a single call |
| `dg export` | Write the decision log as Markdown (`DECISIONS.md`): each decision with why, what was rejected, what it rests on and what it replaced |
| `dg stale` | Decisions whose reasons were replaced: directly, further up the chain, or replaced and later restored |
| `dg show D-3` | One decision: reasons (including ones added later), rejected options, links, source |
| `dg why D-3` / `dg impact D-1` | What it rests on / what depends on it |
| `dg history D-1` | Every version of a decision |
| `dg search sqlite` | Find decisions and reasons |
| `dg confirm D-3` | Mark a decision as right (confidence becomes 100%) |
| `dg reject D-9` | "That's not a decision": hide it and drop its links. Undo with `dg confirm D-9` |
| `dg edit D-3 "wording"` | Fix how a decision is worded; the original is kept |
| `dg corrections` | List your corrections |
| `dg rebuild` | Re-extract the project's whole history with the current prompt (your corrections are carried over) |
| `dg doctor` | Check setup and graph health, with a fix for each problem |
| `dg setup` | Fix Python for the hooks, install `dg`, run the health check |

Any command takes `--project DIR` to look at another project.

### The graph page

`dg view` opens the graph in your browser. Run from a terminal, it serves the page from your own machine
(127.0.0.1 only, behind a random link token) until you press Ctrl+C, and the page gets **✓ Correct**,
**✎ Edit wording** and **✗ Not a decision** buttons that save as you click. Every rejection can be undone.
`dg view --static` writes `.claude/decisions/graph.html` instead: no server, read-only. Asking Claude to "show the
decision graph" starts the live page in the background (`dg view --live`), reusing one that's already running.

- **Live:** leave the page open while you work. Decisions recorded at the next compaction appear on their own,
  with a note at the bottom and a **New** mark. Decisions added since your last visit are marked too, and the
  *new since your last visit* chip filters to them.
- **Empty projects** show what to expect and when, instead of a blank page.
- **Two layouts:** *Flow* reads left to right in the order decisions rest on each other. *Families* boxes each
  foundation decision together with everything that rests on it, as a tree; fold a family to its foundation with
  the arrow in its title. Links between families are drawn faintly.
- **Shapes:** foundations (others rest on them) are hexagons that say how many rest on them, sub-decisions are cards
  with ports where their links connect, standalone decisions are capsules, and replaced ones are notched tickets.
- **Areas:** each decision belongs to an area such as *narrator* or *storage*. Pick one in the header to see only
  that part of the project; unlinked decisions are grouped by area. Run `dg topics` once for decisions recorded before areas existed.
- **Go to a decision:** `Ctrl K` (`Cmd K` on a Mac), type a few words, press Enter, and the view glides to it.
  With a decision selected, `←` goes to what it rests on and `→` to what rests on it; `↑` `↓` move to the nearest card.
- **List view:** the *List* button shows the same decisions as a sortable table, filtered by the search, chips and
  area. It works with a keyboard and a screen reader.
- **Timeline:** the slider at the bottom shows the project as it stood on any day: later decisions disappear,
  replaced ones come back, and stale marks are worked out as of that date. *Play* replays the history.
- **Download as an image:** `⤓` in the corner saves what's on screen as SVG (sharp, for docs) or PNG (for slides),
  including the current filter or timeline date.
- **Changed since your last visit:** decisions you or an amendment changed are marked *Changed*, alongside *New* ones.

- **Look:** an observatory for the project's reasoning. Decisions are ivory cards floating on a deep
  cobalt-gray field, and links are drawn in soft azure light. Each card's edge shows its status: azure is in force,
  brick is stale (a reason it rests on was replaced) and glows faintly, ochre is indirectly affected, deep azure is
  resolved (its reason was replaced, then brought back), and dashed translucent cards are replaced.
- **Motion:** cards emerge out of depth while the links draw themselves in light; cards tilt toward your cursor
  with a moving sheen, and a soft light follows the cursor across the field over a slowly drifting aurora.
  Selecting a card glides the camera to it, gives it a turning azure halo, lets the rest of the field recede,
  and sends comets along its links: into it from its reasons, out of it to what depends on it. Cards glide to new
  places when the layout changes, and a rejected decision folds away.
- **Minimap:** the corner map shows the whole graph and where you are; click or drag it to move around.
- Follows your system's dark mode, and switches off all motion if your system asks for reduced motion.
- **Links:** linked decisions are laid out left to right in the order they rest on each other.
  Decisions with no links are grouped separately below.
- **Hidden replacements:** when replaced decisions are hidden (the default), a link to one is drawn
  from whatever replaced it. It's red if the reason is gone, blue if it was brought back.
- **Selecting a decision:** click one to see its details. What it rests on is highlighted in blue,
  what depends on it in orange.
- **Navigation:** drag to pan, scroll to zoom, `F` to fit, `/` to search, `Esc` to clear. The chips
  at the top filter by status.

Add `.claude/decisions/` to `.gitignore` unless you want decisions in the repo (committing them can be useful for a team).

## Live graph in the background

`dg service install` keeps the live graph running for every project on your computer: it starts when you log in,
`dg view` then opens instantly, and Claude can open it anywhere it has a browser on your computer, including Cowork.
The one-line installers and `dg setup` turn it on (skip with `dg setup --no-service`).

- One small Python process serves all projects, each at `http://127.0.0.1:<port>/p/<project>/`, by default on port
  47474 (`DECISION_GRAPH_PORT` changes it). Only this computer can reach it, each project's page needs that project's
  own access key, and requests from other addresses or web pages are refused.
- Projects are added when Claude Code starts a session in them, when you use `dg` in them, and by a scan of your
  home folder every few minutes, which finds projects you only use through Cowork or Chat (it looks up to five
  folders deep and skips hidden and dependency folders). `dg service scan` runs it now;
  `DECISION_GRAPH_SCAN_ROOTS` (folders separated by `;` on Windows, `:` elsewhere) sets where it looks. Each project's link is kept
  in `.claude/decisions/live.json`, which is listed in that folder's `.gitignore` because it holds the access key.
- If the service stops (a crash, an update), the next Claude Code session starts it again.
- `dg service status` shows whether it's running and which projects it serves; `dg service remove` stops it and
  takes it out of your login items. How it starts at login: a hidden script in your Windows Startup folder, a Launch
  Agent on macOS, an autostart entry on Linux.

## Chat and Cowork

Recording is built on Claude Code: it reads Claude Code's conversation files when a conversation is compacted,
when a session ends, or when you run `dg record`. Chat and Cowork have no `/compact`, and their conversations
aren't stored where the plugin can read them, so nothing from them is recorded automatically. A plugin can't add
`/compact` to them either; it's part of Claude Code.

To keep decisions you make there, type **`/decision-graph:save`** in the conversation. Claude pulls the decisions out
of that conversation, using the same rules as the automatic recorder, and adds them with `dg add`. That command runs
the same checks as compaction: no circular links, replacements resolved against what's already recorded, and
references to unknown decisions dropped. It tells you what it added and what is stale as a result.

- **Cowork needs folder access.** Attach the project's folder (for example `Threadfall`) to the Cowork
  conversation before saving; the graph is written to `<folder>/.claude/decisions/`. Without it, Claude tells you
  so and offers the Chat route below instead.
- **Chat can't reach your files.** Claude gives you the decisions as a block to copy; then run
  `dg add --clipboard` in a terminal in the project folder (or save the block and run `dg add --file decisions.json`).
  In Chat, Claude also can't see what's already recorded unless you paste `dg active`, so links to earlier
  decisions are only made if you do.
- **Saving is manual.** Nothing is saved at the end of a Chat or Cowork conversation; run `/decision-graph:save` when
  decisions were made. Saving again later in the same conversation only adds what came after the last save.
- **The model you're chatting with does the extraction**, not the Haiku call that `eval/` measures, so its accuracy
  isn't covered by the eval scores. Check what it reports, and `dg reject <id>` anything wrong.
- **Only what's still in the conversation can be saved.** If a long Chat or Cowork conversation has dropped its
  early part, decisions from that part are gone.
- **The live graph in Cowork comes from your computer.** Cowork runs commands in Claude's own sandbox, and a page
  served from there can't be reached from your computer. With the background service on (see "Live graph in the
  background" below), every project's live page is always running on your computer and its link is in
  `<folder>/.claude/decisions/live.json`, so asking Claude in Cowork for the graph opens the live page in the browser
  pane. Without the service, run `dg view` in the project folder first; otherwise you get a read-only snapshot
  (header "Snapshot from"), which `/decision-graph:save` refreshes after saving.
- **Cowork may work on a copy of your folder.** When it does, the save skill writes the graph back to your folder
  and checks it arrived before saying it's saved. If you're unsure, run `dg active` in the folder on your computer.
- **The rest is Claude Code only.** The session-start status line and the warnings when something contradicts a
  decision are Claude Code hooks; they don't run in Chat or Cowork. The graph page (`dg view`) works for any project
  on your computer, whichever way its decisions were recorded.

## Is it worth keeping? A two-week trial

Use it normally for two weeks, then run:

```bash
dg report
```

It answers three questions from the project's own log (`.claude/decisions/events.jsonl`):

1. **Did warnings fire, and on what?** It lists recent pauses so you can judge whether each one caught a real mistake.
2. **How often was a recorded decision wrong?** Measured by your corrections, so skim `dg view` now and then and
   click **Not a decision** on anything wrong.
3. **Do you look at the graph?** How often `dg view` was opened, and on how many days.

Rule of thumb: keep it if warnings caught at least one real mistake and fewer than about 1 in 5 recorded decisions
needed rejecting. `--days N` changes the window.

## Record format

One JSON object per line, append-only:

```json
{
  "id": "D-4",
  "decision": "Use SQLite instead of Postgres.",
  "rationale": ["single user now"],
  "alternatives_rejected": ["Postgres"],
  "supersedes": "D-1",
  "depends_on": [],
  "restores": [],
  "decided_by": "user",
  "confidence": 0.95,
  "source": {"session_id": "...", "turn_uuids": ["..."], "decided_at": "..."},
  "extracted_at": "..."
}
```

- **`supersedes`** links a decision to the one it replaced. A record is never edited, and "in force" means nothing replaces it.
- **`depends_on`** links a decision to the decisions that justify it.
- **`restores`** marks a decision that brings back an earlier replaced one ("offline is a requirement again"). Anything resting on the earlier decision counts as justified again.
- **Amendments** (`A-1`, `{"kind": "amendment", "amends": "D-3", "rationale": [...]}`) add a reason to an existing decision without replacing it.
- **`source`** points back to the exact transcript turns, so a claim can be checked against what was actually said.
- **Corrections** (`C-1`, `{"kind": "correction", "target": "D-9", "action": "reject" | "confirm" | "edit", ...}`)
  record your fixes. They're replayed in order: the latest confirm or reject wins, and the latest edit wins.
  A rejected decision is ignored everywhere, including by the extractor, and anything it replaced is back in force.

## Behaviour and safeguards

- **Extraction never blocks compaction.** Errors go to `.claude/decisions/extract.log`. The next session tells Claude, and `dg doctor` shows the cause.
- **Failed turns are retried.** The per-session cursor only advances after a successful extraction.
- **The extractor won't create a broken graph.** It drops circular "rests on" links, replacement loops, references to unknown decisions, and restores of decisions still in force. When asked to replace a decision that's already been replaced, it replaces the version in force instead. Each correction is logged.
- **A crash during a write can't corrupt the next record.** New records always start on a fresh line.
- **Your login is used, not a stray API key.** Headless `claude -p` would otherwise prefer `ANTHROPIC_API_KEY`. Set `DECISION_GRAPH_AUTH=api-key` to bill extraction to a key on purpose.
- **Its own calls aren't extracted.** The extraction call never triggers these hooks, and `rebuild` skips transcripts left by older versions' extraction calls.

Settings (environment variables): `DECISION_GRAPH_MODEL` (default `haiku`), `DECISION_GRAPH_AUTH`
(`auto`, `login`, `api-key`), `DECISION_GRAPH_MAX_CHARS`, `DECISION_GRAPH_INJECT_CHARS` (default 6000),
`DECISION_GRAPH_CLAUDE_BIN` (path to `claude` if it isn't on PATH), `DECISION_GRAPH_GUARD` (`ask`, `warn`, `off`).

## Known limits

- **Chat and Cowork are recorded only when you ask** (`/decision-graph:save`); see "Chat and Cowork" above for what
  that can and can't do.
- **Recording happens at compaction, at the end of a session, or when you ask** (`dg record`). A decision made
  mid-session appears in the graph at the first of those. Session-end recording runs in the background after
  Claude Code exits; if your system stops it, the next compaction or `dg record` picks up where it left off.
- **Extraction quality depends on the prompt.** That's `PROMPT` in `scripts/extract_decisions.py`, and it's the first thing to tune against real sessions.
- **Compaction pauses briefly.** Each compaction waits for the Haiku extraction call, usually 5–30 seconds.
- **Windows needs Git Bash for the hooks.** Claude Code can run without Git for Windows (it falls back to
  PowerShell), but this plugin's hooks can't; `dg doctor` and the installer check for it.
- **Tested on Windows and Linux.** The macOS parts (the login entry, the Python lookup) are written to the platform's
  conventions but haven't been run on a Mac yet.
- **Warnings only know what was recorded.** They match names of rejected options and replaced choices (tools,
  libraries, services), not ideas described in other words. A decision whose rejected options weren't said gives nothing to warn about.

## Measuring extraction quality

```bash
python eval/run_eval.py              # all cases, 3 runs each (about 2 minutes)
python eval/run_eval.py --cases 06   # one case
```

`eval/cases.json` holds conversations with their correct answers: decisions that should be found, traps that
must not become decisions (rejected options, brainstorming, debugging steps, "leaning towards"), links, amendments,
rejected alternatives, who decided, and which reasons were actually given. A recorded reason that was never said
counts as unsupported. The extractor runs on each case several times, because the model isn't
deterministic. The scores (recall, precision, traps triggered, links, attribution) go to
`eval/results/<timestamp>/report.md`, and `eval/results/latest.md` always holds the most recent report.
Run it before and after any change to the extraction prompt. `eval/oracle/` holds a perfect answer for every case;
`tests/test_eval.py` uses them to check the scorer itself.

## Tests

```bash
python tests/run_all.py
```

The tests use the standard library only and make no network calls. `test_auth` checks login handling with a fake `claude` and runs on macOS/Linux only.

## Layout

```
.claude-plugin/plugin.json        plugin manifest
.claude-plugin/marketplace.json   lets this folder be added as a local marketplace
hooks/hooks.json                  PreCompact, SessionEnd, SessionStart, UserPromptSubmit, PreToolUse hooks
scripts/dg.py                     the `dg` command
scripts/dg_store.py               storage, locking, graph logic (shared)
scripts/extract_decisions.py      PreCompact: transcript -> decision records
scripts/inject_decisions.py       SessionStart: decisions in force -> context, status line -> you
scripts/guard.py                  contradiction warnings on your messages and Claude's commands
scripts/query_decisions.py        text queries
scripts/dg_view.py                interactive graph page (data + local server)
scripts/graph_template.html       the page itself: layout, styles, animations
scripts/dg_doctor.py              setup and graph health checks
scripts/rebuild_decisions.py      re-extract a project's whole history
scripts/dg_correct.py             confirm / reject / edit
scripts/install_cli.py            installs the `dg` launcher (follows plugin updates)
scripts/dg_setup.py               `dg setup`: Python for hooks, launcher, health check
scripts/dg_report.py              `dg report`: two-week trial numbers
scripts/record_decisions.py       `dg record` and the SessionEnd hook
scripts/add_decisions.py          `dg add`: record decisions saved from Chat or Cowork
scripts/dg_service.py             `dg service`: the live graph in the background, for all projects
scripts/dg_export.py              `dg export`: Markdown decision log
scripts/dg_topics.py              `dg topics`: tag decisions with areas
install.ps1, install.sh           one-line installers
skills/decisions/SKILL.md         tells Claude when and how to query
skills/graph/SKILL.md             "show me the graph"
skills/setup/SKILL.md             /decision-graph:setup
skills/record/SKILL.md            /decision-graph:record
skills/save/SKILL.md              /decision-graph:save, for Chat and Cowork
eval/                             extraction quality benchmark (cases, runner, perfect answers)
tests/                            stdlib tests; fixtures/notes_app.jsonl is a real example graph
```
