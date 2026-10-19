# Email vault — setup guide

An Obsidian vault that turns saved Outlook emails into a searchable, cross-linked wiki,
maintained by Claude Code. Modelled on Karpathy's **LLM wiki**: raw sources stay immutable,
the LLM "compiles" them once into thread, person, org and project pages, and every later
question reads the compiled wiki instead of re-reading your inbox.

```
Outlook ──save .msg──▶ 00-inbox/ ──msg_to_md.py──▶ raw/emails, raw/attachments   (deterministic, no LLM)
                                                        │
                                          /email-ingest │ Claude reads, triages, links
                                                        ▼
                         wiki/threads · people · orgs · projects · index.md · log.md
                                                        │
                              /email-ask  ◀─────────────┤──────────▶  Obsidian: Bases, graph, search
                              /email-lint (health check) · /email-today (morning brief) · /email-draft (reply draft)
```

Why two stages: conversion is cheap, exact and repeatable (headers, threading, attachments,
dedup), so a script does it. Judgement — what matters, who owes what, how things connect — is
the LLM's job, and its output is plain markdown you can read, edit and version.

## 1. Install (≈15 minutes)

1. **Obsidian 1.12+** (desktop). Open this folder as a vault (*Open folder as vault*).
   Settings → General → enable **Command line interface** (the Obsidian CLI).
2. **Python environment with uv.** Install uv (`winget install astral-sh.uv`, or
   `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`), then from the vault root:
   ```powershell
   uv sync            # creates .venv/ from pyproject.toml + uv.lock (installs pywin32 on Windows only)
   ```
   uv downloads a suitable Python if you don't have one. Alternative without the lock file:
   `uv venv` then `uv pip install -r requirements.txt`. Run scripts with `uv run --frozen python …` (after `uv sync`).
   Keep it in a venv: `extract-msg`'s `red-black-tree-mod` dependency fails to build against
   some system Pythons, but builds fine in uv's isolated build.
3. **Claude Code** installed and signed in. Then run `claude` in the vault root.
   kepano's Obsidian skills (obsidian-markdown, obsidian-bases, json-canvas, obsidian-cli) are
   vendored in `.claude/skills/` (see `OBSIDIAN-SKILLS.md`), so nothing extra to install. They and
   the email skills load automatically because Claude Code reads project skills from the
   folder it starts in.
4. **Replace the placeholders in the Owner section at the top of `CLAUDE.md`** with your name and
   your own email address(es) (and any senders to ignore). Claude relies on this to know which
   mailbox is yours, so it never creates a person page for you and treats mail you sent correctly.
   The scripts read your addresses from that line too, so emails you sent are tagged
   `direction: sent`; `--me` or `EMAIL_VAULT_ME` can add more.
5. *Optional, team mail.* To also track what a team owes on a distribution list such as
   `dreamteam@coolco.uk`, fill in `Team addresses` and `Team: <name>; members: …` in the Owner section.
   Do **not** add the list to Owner's addresses: that would file every team request under "I owe".
   Team threads get `owner: team` until a teammate takes them on (CLAUDE.md → Team mail), and they appear in
   the **Team owes** view and in the brief's **Team, unassigned** section. A distribution list's copies already
   arrive in your Inbox, so the exporter needs nothing extra (add `--folder` if an Outlook rule files them
   elsewhere). Ask the team to keep the list on cc when replying; a reply sent only to the requester
   never reaches the vault, so that thread stays unassigned. Leave both lines as `none` and nothing changes.

## 2. Daily use

| Step | How |
|---|---|
| Save emails | Drag from Outlook into `00-inbox/`, or use an exporter (below) |
| Compile | In Claude Code: `/email-ingest` (15 threads per run; `/email-ingest all` for a backlog); it ends with a lint pass that fixes its own index/citation gaps |
| Ask | `/email-ask what is waiting on me this week?` · `/email-ask how is Tom connected to the Q3 budget?` |
| Brief | `/email-today` — overdue, due today/soon, who to nudge (`/email-today 3` for a 3-day nudge threshold) |
| Draft | `/email-draft Q3 budget agree to the 14 Oct deadline` — writes a reply to `wiki/drafts/` plus a linked `.msg` (classic Outlook) that opens as an unsent reply under the original; nothing is sent |
| Browse | Obsidian: `bases/Threads.base` (I owe / Team owes / Waiting / Overdue / Due this week / Nudge / By project), `bases/People.base`, graph view, backlinks |
| Maintain | `/email-lint` weekly (its report starts with a Setup section while the CLAUDE.md Owner section is unfilled) |

Keep Claude Code and Obsidian side by side, as Karpathy does: Claude edits, Obsidian shows the
pages updating live. Good answers get filed into `wiki/answers/`, so your questions compound
in the vault too.

For a first backlog of hundreds of emails, export a month at a time and run
`/email-ingest all` per batch; compiling is where the tokens go, conversion is free.

### Exporting from Outlook

The exporter drives the **classic Outlook desktop app** through COM (Windows only; the
"new Outlook" has no COM interface). Outlook may show a "program is trying to access…" prompt
if your antivirus status isn't reported to Windows; allow it or ask IT.

**`tools/export_outlook_msg.py`** remembers what it exported (by Outlook
EntryID, in `raw/.outlook-export-state.json`), so it suits a timer:
```powershell
uv run python tools/export_outlook_msg.py --list-folders                 # see folder paths
uv run python tools/export_outlook_msg.py --selection                    # what's selected in Outlook
uv run python tools/export_outlook_msg.py --days 7                       # Inbox, last 7 days
uv run python tools/export_outlook_msg.py --days 30 --folder "Inbox/Projects/Q3" --folder "Sent Items"
uv run python tools/export_outlook_msg.py --since 2026-09-01 --folder "Shared Mailbox/Inbox"
uv run python tools/export_outlook_msg.py --days 1 --convert             # export, then convert to raw/
```
Running it with no arguments just prints the help; you must choose `--selection`, `--days`,
`--since` or `--list-folders`. Other flags: `--unread-only`, `--limit N` (newly saved emails per
folder), `--dry-run`, `--force` (re-export a selection), `--output-dir DIR` (default
`00-inbox/`) and `--state-file FILE` (default `raw/.outlook-export-state.json`).
Exporting Sent Items too gives Claude your side of each thread.

To skip mail before it is exported, list patterns in `tools/ignore.from.txt` (sender name or
address) and `tools/ignore.subject.txt` (subject), one per line, case-insensitive partial match,
`#` comments allowed; `--ignore-from-file` / `--ignore-subject-file` point elsewhere. Ignored mail
isn't remembered as exported, so removing a pattern lets it export next time. Both files are
git-ignored (they may name correspondents).

## 3. Folder structure

```
00-inbox/                 drop .msg/.eml here; _failed/ holds files that wouldn't parse
raw/emails/YYYY/MM/       one note per email (frontmatter + new text + folded quoted history)
raw/attachments/YYYY/MM/  attachment files + .extracted.md text (pdf, docx, xlsx, txt, csv)
raw/msg/YYYY/MM/          archived originals, same name as the note
wiki/threads/             one page per conversation: summary, timeline, decisions, open items, status
wiki/people/  wiki/orgs/  correspondents and their organisations
wiki/projects/            topics spanning several threads
wiki/answers/  digests/   filed query results
wiki/drafts/              reply drafts (.md) and unsent Outlook replies (.msg); never sent
wiki/index.md  log.md     catalogue Claude reads first; append-only history
bases/                    Obsidian Bases dashboards
tools/                    Outlook exporter (export_outlook_msg.py)
pyproject.toml  uv.lock   Python environment for uv (requirements.txt for uv pip / pip)
CLAUDE.md                 the schema: rules, page templates, triage — edit it as you go
.claude/skills/           email-ingest (+ scripts/), email-ask, email-lint, email-today, email-draft
```

Design choices worth knowing:
- **Organise by entity, not by mailbox folder.** Outlook folders are one-dimensional; a thread
  belongs to a project, several people and an org at once, so it's linked to each.
- **Thread page = unit of work.** Each has `status` (open = I or my team owe, waiting = they owe, fyi,
  closed), `next_action`, `due`, `owner`, so Bases can show your open loops.
- **Quoted history is folded, not deleted.** Replies quote the whole chain; the converter
  moves it into a collapsed callout so Claude reads only the new text, while search still finds it.
- **Newsletters and notifications** are marked `skipped` — searchable, but no wiki pages.

## 4. Obsidian plugins

Core (built in, enable in Settings → Core plugins):
- **Bases** — database views over frontmatter; powers the dashboards. Use it rather than
  Dataview for new dashboards; Dataview still works but is barely maintained.
- **Properties**, **Backlinks**, **Outgoing links**, **Graph view**, **Search** — on by default; keep them.
- **Command line interface** (Settings → General) — lets Claude search, read backlinks and
  query Bases through `obsidian` commands instead of grepping.

Community (Settings → Community plugins → Browse):
- **Omnisearch** + **Text Extractor** — ranked, typo-tolerant full-text search for you, including
  text inside PDFs and images in `raw/attachments`.
- **Smart Connections** — local embeddings; shows semantically related notes in a side pane.
  Good for spotting connections you didn't link ("other emails like this one").
- **Obsidian Git** — commits the vault on a timer, so every Claude edit is diffable and reversible.
  Karpathy's tip: the wiki is just a git repo of markdown.
- Optional: **Claudian** (runs Claude Code inside an Obsidian side pane) if you prefer one window;
  **Dataview** only if you already rely on it.

When the wiki outgrows `index.md` (a few thousand emails), add **qmd** (Tobi Lütke's local
BM25 + vector search with an MCP server, the search tool Karpathy recommends):
`claude plugin marketplace add tobi/qmd`, then `claude plugin install qmd@qmd`, then index the
vault folder as a qmd collection (see the qmd README). The skills already prefer it when present.

## 5. Plugging into Claude Code

Everything Claude needs is in the vault:
- `CLAUDE.md` loads at every session start — it's the schema that makes Claude a disciplined
  wiki maintainer.
- `.claude/skills/*/SKILL.md` — each is a slash command and is also used automatically when
  your request matches its description. Arguments go after the name (`/email-ingest 30`).
- `.claude/settings.json` pre-approves only the read-only script calls in every session:
  `lint_vault.py`, and `pending.py` (list), `pending.py stats` and `pending.py --json`. Each one is
  run as `uv run --frozen python …` or with the `.venv` interpreter, so nothing is resolved or
  installed at run time. Settings grant no wiki writes.
- Each skill's `allowed-tools` grants the rest, and only while that skill runs:
  - `/email-ingest` converts and marks emails and writes `wiki/`.
  - `/email-lint` writes `wiki/`.
  - `/email-ask` writes answers and digests, and edits `wiki/`.
  - `/email-draft` writes `wiki/drafts/` and the log, and runs `draft_to_msg.py` to save the .msg
    next to the draft (Outlook `SaveAs` to a file only; it never calls `Send` or saves to the mailbox).
  - `/email-today` writes nothing.

  Skill rules only add permissions, so keeping settings this narrow is what makes the skill
  scopes real. Wiki edits outside a skill ask first, and so does anything outside `wiki/`, so text
  in an email can't steer Claude into silently editing a script, a skill or these settings.
- The settings also **deny**:
  - edits to `raw/`
  - `--vault` on the scripts: they always work on this vault, and argparse abbreviations are disabled
  - WebFetch and WebSearch
  - `curl`, `wget`, `Invoke-WebRequest` and `Invoke-RestMethod`, in both Bash and PowerShell
  - package installs
  - the Gmail, Google Drive and Chrome connectors
- Script rules name the file exactly (`pending.py` or `pending.py <args>`, not a prefix), so a
  newly written `pending.py_x.py` isn't pre-approved. Don't ingest in auto-accept or bypass
  permission modes, which skip those prompts.

### Use notes
- `raw/` and all of `wiki/` are git-ignored (only empty folder markers are committed), so
  Obsidian Git or any commit never captures email bodies, attachments, originals or the summaries
  compiled from them. A fresh clone starts with an empty wiki; the first ingest recreates
  `wiki/index.md` and `wiki/log.md`.
- `.githooks/pre-commit` refuses any commit that stages files under `raw/`, `00-inbox/` or `wiki/`
  (other than `.gitkeep`), so `git add -f` or a plugin can't push email content either. Git
  doesn't enable repo hooks automatically: run once per clone, from the git root,
  `git config core.hooksPath email-vault/.githooks` (or `.githooks` if the vault is the git root).
- `.ignore` re-includes `wiki/` and the `.md` files under `raw/` for search only: ripgrep (Claude
  Code's Grep) honours `.gitignore` and would otherwise skip them; git does not read `.ignore`.
  Originals and attachments stay hidden, so ruff never lints an emailed `.py` file.
- `.gitattributes` keeps LF line endings in the working tree whatever `core.autocrlf` says, since
  the scripts and Obsidian write LF.
- `pending.py mark` only touches `type: email` notes under `raw/emails/`; `msg_to_md.py` only
  accepts files inside `00-inbox/`.
- Attachments of executable/script types are stored with a `.blocked` suffix and never parsed;
  attachments over 25 MB are stored but not parsed. Attachments named `.md`, `.canvas` or `.base` get
  `.txt` appended, so an emailed file can't pose as a vault note or capture `[[links]]`.
- In raw notes and extracted text, `<` is written as `&lt;` and `![` as `!\[`. Opening a note
  therefore never loads remote images or HTML from an email (tracking pixels, iframes).
- A file that fails to convert is moved to `00-inbox/_failed/` and the rest of the batch still runs.

Automation options:
- **Capture on a timer** (no LLM): a Windows Task Scheduler task, hourly, running
  `uv run --directory <vault path> python tools/export_outlook_msg.py --days 1 --convert`.
  Run it as your own user while logged in, since it needs your Outlook session.
- **Compile on a schedule**: a scheduled task in the Claude desktop app with the prompt
  `/email-ingest`, or headless `claude -p "/email-ingest"` from Task Scheduler. Desktop
  scheduled tasks run on your machine, so they see the vault's project skills; cloud
  routines don't.
- Run Claude Code from this folder. The scripts find the vault from the project directory, and
  Claude may not pass `--vault`, so copying the skills elsewhere won't reach this vault. You and
  the exporter can still use `--vault` from a terminal.


## 6. Troubleshooting

- `pip install extract-msg` fails building `red-black-tree-mod` → use `uv sync` (step 1.2), which
  builds it in an isolated environment with current `setuptools`.
- Exporter says it can't start Outlook → it needs classic Outlook installed and your Windows user
  session; the "new Outlook" toggle must be off.
- A file lands in `00-inbox/_failed/` → read its `.error.txt`. Encrypted (S/MIME, IRM) messages
  can't be parsed; save them as plain text from Outlook instead.
- Wrong times → the converter writes local time of the machine it runs on; run it where your
  Outlook runs.
- Sender shows as a name with no address → internal Exchange sender with no SMTP address in the
  file; the person page is matched by name instead.
- `python pending.py stats` shows counts by status; `python lint_vault.py` the vault's health.
- `uv run pytest` runs the exporter's tests (fake Outlook objects; no Outlook needed).
