# Email Vault: Folder Structure and Setup Guide

Oct 13, 2026 · @Maxim

## How the folders are organised

The vault keeps the mail you collect untouched and lets Claude write summaries on top of it, following Karpathy's LLM-wiki split. Each folder has one owner, and most mistakes come from writing into a folder you don't own.

| Folder | Owner | Holds |
| --- | --- | --- |
| `00-inbox/` | You | Drop zone for `.msg` / `.eml` files |
| `raw/` | Scripts only | One note per email, attachments, archived originals |
| `wiki/` | Claude | Thread, person, org and project pages, index, log |
| `bases/` | You or Claude | Dashboards over frontmatter; no data of their own |

### 00-inbox: the drop zone

- Put `.msg` or `.eml` files here, by dragging from Outlook or with the exporter. Don't organise anything here.
- The converter empties it on every run. Each file becomes a note in `raw/`, and the original is archived to `raw/msg/`.
- Files that won't parse go to `00-inbox/_failed/` with an `.error.txt` explaining why. Encrypted or rights-protected mail ends up here.
- Duplicates are safe. A repeat is detected by Message-ID and file hash and moved to `raw/msg/_duplicates/`.

### raw: the source of truth (read-only)

- **`raw/emails/YYYY/MM/`** has one note per email, named like `2026-10-05 0814 Shah, Priya - Q3 hedging budget.md`. Each note has:
  - frontmatter: `from`, `participants`, `date`, `thread_id`, `has_attachments`, `wiki_status`
  - `## Message` with only the new text
  - `## Attachments` with links
  - `## Quoted history`, the earlier reply chain, folded into a collapsed block
- **`raw/attachments/YYYY/MM/<email name>/`** holds attachment files. PDF, Word, Excel and text attachments also get a `*.extracted.md` with their text, so search and Claude can read them.
- **`raw/msg/`** holds the archived originals, with the same names as their notes.
- **Hidden state files** keep duplicate detection and incremental export working, so don't delete them:
  - `.email-index.json` remembers which emails were converted.
  - `.ingest-log.jsonl` logs conversion runs.
  - `.outlook-export-state.json` remembers what the Python exporter saved.

What to know:

- Don't rename, move or edit raw notes. Wiki pages link to them by filename.
- The only field that changes is `wiki_status`: `pending` → `compiled`, or `skipped` for newsletters and notifications. Claude changes it through a script; `.claude/settings.json` blocks other edits to `raw/`.
- Skipped emails stay fully searchable; they just get no wiki pages.
- If you delete a raw note, the index still remembers it, so re-exporting that email is treated as a duplicate.

### wiki: the compiled knowledge (written by Claude)

- **`threads/`** — one page per conversation, named `<Subject> (YYYY-MM).md`. The pages you'll use most. Each has:
  - a short summary of where things stand
  - a dated timeline linking each source email
  - decisions and open items
  - `status`: `open` (you owe something), `waiting` (they do), `fyi`, `closed`
  - `next_action`, `due` and `owner`
- **`people/`** — one page per correspondent, listing their addresses and name variants (e.g. "Shah, Priya") so Claude matches them reliably.
- **`orgs/`** — one page per company, matched by email domain. Gmail and Outlook.com addresses get none.
- **`projects/`** — topics spanning several threads. Created when two or more threads share a topic, or when you name one.
- **`answers/`** and **`digests/`** — answers and summaries you chose to keep, so your questions make the wiki more useful over time.
- **`index.md`** is the catalogue Claude reads first, one line per page. **`log.md`** is an append-only record of every ingest, saved answer and lint run.

What to know: you can edit wiki pages, but Claude rewrites summaries on later runs. For corrections, tell Claude ("Tom isn't the owner, Priya is") or write in a person page's `## Notes` section. Renaming pages inside Obsidian is fine; Obsidian updates the links.

### bases: dashboards, not data

| Base | Views |
| --- | --- |
| `Threads.base` | I owe · Waiting on others · Overdue · All threads (your to-do view) |
| `Emails.base` | Last 14 days · Not yet compiled · High importance · With attachments |
| `People.base` | People table with days since last email · Cards grouped by organisation |

`.base` files store no content, so deleting or changing one loses nothing. They rely on `.obsidian/types.json`, which marks `date` and `due` as dates; without it, date filters fail. Add views or columns from the Bases UI.

### Other files

- `CLAUDE.md` is the rulebook Claude reads at the start of every session.
- `.claude/` holds the skills and permissions; `tools/` holds the exporters; `.venv/` is created by uv.
- Obsidian hides dot-folders, so `.claude/` and `.venv/` don't appear there.

## Running it, step by step

Setup takes about 15 minutes once; after that the daily loop is export, `/email-ingest`, then check *I owe* in `Threads.base`.

### One-time setup

1. **Unzip to a short path**, such as `H``:\\``P``r``o``j``e``c``t``\\``EmailVault`.
   - Attachment paths nest deeply, and Windows limits paths to 260 characters unless long paths are enabled.
   - Avoid OneDrive if the mail is confidential, and avoid syncing while Claude is writing.
2. **Set up Python.** In a terminal in the vault folder, run `uv sync`.
3. **Open it in Obsidian.** Choose *Open folder as vault*, pick the folder, then in Settings:
   - Core plugins: check **Bases**, **Backlinks**, **Graph view** and **Search** are on.
   - Files & links: turn on **Automatically update internal links**.
   - General: enable the **Command line interface** (needs Obsidian 1.12 or later).
4. **Optional community plugins:**
   - **Omnisearch** and **Text Extractor** for full-text search, including inside PDFs.
   - **Smart Connections** for related notes.
   - **Obsidian Git** so every Claude edit can be undone.
5. **Tell the vault who you are:**
   - Fill in the Owner section at the top of `CLAUDE.md`.
   - Run `setx EMAIL_VAULT_ME "you@work.com"` once, so emails you sent are tagged as sent. It takes effect in new terminals.
6. **Start Claude Code** in the vault folder:
   - Run `claude` and accept the "trust this folder" prompt.
   - The Obsidian skills (markdown, bases, canvas, CLI) are bundled in `.claude/skills/`; nothing to install.
   - Type `/` and check that `email-ingest`, `email-ask` and `email-lint` are listed.

### First run

1. Preview a week of mail without saving anything: `uv run python tools/export_outlook_msg.py --days 7 --folder Inbox --folder "Sent Items" --dry-run` If the list looks right, run it again without `--dry-run`. Including Sent Items gives Claude your side of each thread. The exporter does nothing until you choose `--selection`, `--days`, `--since` or `--list-folders` (no arguments prints the help), and `--output-dir` / `--state-file` let you redirect where `.msg` files and the export state are written.
2. In Claude Code, run `/email-ingest all`. Claude converts the files, then works through each thread: it creates thread, person and org pages, updates `index.md` and `log.md`, and reports what you owe.
3. In Obsidian, put the window beside the terminal and look at:
   - `wiki/index.md`
   - `bases/Threads.base`, especially the *I owe* tab
   - the graph view filtered with `path:wiki`, so raw email notes don't clutter it

### Routine

| When | What |
| --- | --- |
| Daily | Export (`--days 1`), run `/email-ingest`, check *I owe* in `Threads.base` |
| Anytime | `/email-ask what did Tom say about the deadline?` or `/email-ask who else is involved in Q3 hedging?` Say "save it" to keep the answer in `wiki/answers/` |
| Weekly | `/email-lint`: fixes broken links and gaps itself, asks before merging duplicate people or closing threads |
| Backlog | Export a month at a time, then `/email-ingest all` per batch |

### Optional automation

In Task Scheduler, run this hourly as your own logged-in user, because it needs your Outlook session:

`uv run --directory C:\EmailVault python tools/export_outlook_msg.py --days 1 --convert`

This keeps `raw/` up to date; run `/email-ingest` whenever you want the wiki refreshed.

### Cautions

- Don't run two ingests at the same time.
- Attachment paths are the most likely to hit Windows' length limit. If the vault path can't be short, the converter can be changed to use shorter file names.
