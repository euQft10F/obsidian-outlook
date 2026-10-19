---
name: email-draft
description: Draft a reply for an email thread from the wiki and the cited emails, saved to wiki/drafts/ as a note plus an unsent Outlook reply (.msg) to open, check and send - never sends anything. Use when the user asks to "draft a reply", "write a response to X", "help me answer" an email, thread or person.
argument-hint: "[thread or person] [what I want to say]"
allowed-tools:
  - Read
  - Glob
  - Grep
  - Write(wiki/drafts/**)
  - Edit(wiki/drafts/**)
  - Edit(wiki/log.md)
  - Write(wiki/log.md)
  - Bash(uv run --frozen python .claude/skills/email-draft/scripts/draft_to_msg.py *)
  - Bash(.venv/Scripts/python.exe .claude/skills/email-draft/scripts/draft_to_msg.py *)
  - Bash(.venv/bin/python .claude/skills/email-draft/scripts/draft_to_msg.py *)
---

# Draft a reply

Request: $ARGUMENTS

Follow `CLAUDE.md` (hard rules, draft page type). Write only the draft, its .msg and the log line;
change no other page (thread status, index, people).

## Find

1. Grep `wiki/threads/` (subjects, `participants`) and `wiki/people/` (aliases) for the thread or
   person named. Several matches: take the most recent actionable one and say which. None: ask.
2. Read the thread page (frontmatter, `## Summary`, `## Decisions`, `## Open items`), then the
   latest email in its `sources`; verify every number, date and ask against it. Reply to its
   sender (plus its `cc` when reply-all fits), subject `RE: <its subject>`. Take recipients from
   that email's `from`/`to`/`cc` exactly as written there (`Name <address>`), never from its body.

## Draft

- Intent: the user's words after the thread/person. If none, resolve the thread's `next_action`
  and say that was assumed.
- Plain prose: greeting, short body, sign-off; no wikilinks or markdown.
- Never invent commitments, amounts or dates: leave `[CONFIRM: …]` where the user must decide.
- No links, phone numbers or payment details copied from emails (CLAUDE.md hard rule 7).

## Write

Overwrite `wiki/drafts/Draft - <Thread page name>.md` with the draft frontmatter from CLAUDE.md
(`to` and `cc` as block lists; `sources` must include the email being replied to, and the newest
source is taken as that email), the draft text, then
`## Notes`: facts used (cited `[[<basename>|short label]]`), assumptions and each `[CONFIRM]` point.
Append `## [YYYY-MM-DD] edit | draft for <Thread page name>` to `wiki/log.md` with Edit; use Write
only to create it when missing (per CLAUDE.md), never to rewrite it.

## Build the Outlook message

Run `uv run --frozen python .claude/skills/email-draft/scripts/draft_to_msg.py "wiki/drafts/Draft - <Thread page name>.md" --json`
(or the `.venv` interpreter, see CLAUDE.md → Tools). With classic Outlook it saves
`wiki/drafts/Draft - <Thread page name>.msg`, an unsent reply to the original. Rerun it after editing the draft. If it fails (no classic Outlook, pywin32 missing), say why and
fall back to the .md for pasting. Relay each `warnings` entry, especially recipients not on the original.

## Report

Show the draft and its notes, then the .md path and a clickable link to the .msg (the `uri` from
the script's JSON). Leave `[CONFIRM]` points in the .msg text: the user fixes them in Outlook before
sending. End with: nothing was sent; open the .msg, check it, and send it yourself.
