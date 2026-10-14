---
name: email-draft
description: Draft a reply for an email thread from the wiki and the cited emails, saved to wiki/drafts/ for the user to paste into Outlook - never sends anything. Use when the user asks to "draft a reply", "write a response to X", "help me answer" an email, thread or person.
argument-hint: "[thread or person] [what I want to say]"
allowed-tools:
  - Read
  - Glob
  - Grep
  - Write(wiki/drafts/**)
  - Edit(wiki/drafts/**)
  - Edit(wiki/log.md)
---

# Draft a reply

Request: $ARGUMENTS

Follow `CLAUDE.md` (hard rules, draft page type). Write only the draft and the log line; change
no other page (thread status, index, people).

## Find

1. Grep `wiki/threads/` (subjects, `participants`) and `wiki/people/` (aliases) for the thread or
   person named. Several matches: take the most recent actionable one and say which. None: ask.
2. Read the thread page (frontmatter, `## Summary`, `## Decisions`, `## Open items`), then the
   latest email in its `sources`; verify every number, date and ask against it. Reply to its
   sender (plus `cc` when reply-all fits), subject `Re: <its subject>`.

## Draft

- Intent: the user's words after the thread/person. If none, resolve the thread's `next_action`
  and say that was assumed.
- Plain prose to paste into Outlook: greeting, short body, sign-off; no wikilinks or markdown.
- Never invent commitments, amounts or dates: leave `[CONFIRM: …]` where the user must decide.
- No links, phone numbers or payment details copied from emails (CLAUDE.md hard rule 7).

## Write

Overwrite `wiki/drafts/Draft - <Thread page name>.md` with the draft frontmatter from CLAUDE.md,
the draft text, then `## Notes`: facts used (cited `[[<basename>|short label]]`), assumptions and
each `[CONFIRM]` point. Append `## [YYYY-MM-DD] edit | draft for <Thread page name>` to
`wiki/log.md` with Edit, never a whole-file Write (create it first if missing, per CLAUDE.md).

## Report

Show the draft and its notes, give the file path, and end with: nothing was sent.
