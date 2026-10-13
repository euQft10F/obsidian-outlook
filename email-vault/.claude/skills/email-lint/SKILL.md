---
name: email-lint
description: Health-check and tidy the email wiki - broken links, orphans, index gaps, duplicate people, overdue or stale threads, contradictions and frequent correspondents without pages. Use when the user asks to lint, clean up, audit or check the vault, or weekly after ingests.
allowed-tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Bash(uv run --frozen python .claude/skills/email-ingest/scripts/lint_vault.py*)
  - Bash(.venv/Scripts/python.exe .claude/skills/email-ingest/scripts/lint_vault.py*)
  - Bash(.venv/bin/python .claude/skills/email-ingest/scripts/lint_vault.py*)
---

# Lint the email wiki

1. Run `<python> .claude/skills/email-ingest/scripts/lint_vault.py` (see CLAUDE.md → Tools).
   It counts what a script can count.
2. **Fix non-destructive mechanical issues directly**: pages missing from `wiki/index.md`,
   missing `sources`, frontmatter `type` errors, compiled emails no page cites (add them to the
   right thread timeline), broken links with one obvious target (re-point them). Anything that
   removes text (e.g. dropping a broken link with no target) goes to step 4 as a proposal.
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
