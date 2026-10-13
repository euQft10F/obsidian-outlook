---
name: email-lint
description: Health-check and tidy the email wiki - broken links, orphans, index gaps, duplicate people, overdue or stale threads, contradictions and frequent correspondents without pages. Use when the user asks to lint, clean up, audit or check the vault, or weekly after ingests.
allowed-tools:
  - Read
  - Edit
  - Glob
  - Grep
  - Bash(uv run *)
  - Bash(python *)
  - Bash(python3 *)
  - Bash(.venv/bin/python *)
  - Bash(.venv/Scripts/python.exe *)
---

# Lint the email wiki

1. Run `<python> .claude/skills/email-ingest/scripts/lint_vault.py` (see CLAUDE.md → Tools).
   It counts what a script can count.
2. **Fix mechanical issues directly**: broken links (point them at the right page or remove),
   pages missing from `wiki/index.md`, missing `sources`, frontmatter `type` errors,
   compiled emails no page cites (add them to the right thread timeline).
3. **Judgement checks** (read the pages involved):
   - Contradictions between pages (dates, amounts, owners, decisions) — newest source wins,
     use the supersede format.
   - Thread `status` that no longer fits (a reply arrived → `waiting`→`open`; resolved → `closed`).
   - Summaries that no longer match their timelines.
   - People pages that are the same person (same address or obvious alias) — merge.
   - Frequent correspondents without a person page (the report lists them) — create pages.
   - Topics spanning several threads with no project page.
4. **Ask before** merging pages, deleting pages, or changing many thread statuses at once;
   list proposed changes and apply after the user agrees.
5. Append `## [YYYY-MM-DD] lint | <n> fixes` with bullets to `wiki/log.md`, then report:
   fixes made, proposals awaiting a yes, and overdue items for the owner.

Never modify `raw/`.
