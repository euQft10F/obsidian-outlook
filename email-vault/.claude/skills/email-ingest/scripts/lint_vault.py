#!/usr/bin/env python3
"""Deterministic health check of the email wiki. Prints a markdown report.

Checks the things a script can count, so the LLM only has to judge the rest:
broken links, orphans, pages missing from index.md, bad frontmatter, duplicate people,
overdue / stale threads, compiled emails no wiki page cites, inbox backlog and failures.

    python lint_vault.py [--stale-days 7] [--json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vaultlib import find_vault, iter_notes, parse_frontmatter, utf8_stdout, wikilinks  # noqa: E402

WIKI_TYPES = {"person", "org", "thread", "project", "answer", "digest", "index", "log", "overview"}
SPECIAL = {"index", "log"}
NOREPLY_RE = re.compile(r"no-?reply|do-?not-?reply|notification|mailer-daemon|postmaster|calendar", re.I)


def parse_date(v) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault")
    ap.add_argument("--stale-days", type=int, default=7)
    ap.add_argument("--min-emails", type=int, default=3, help="threshold for 'frequent correspondent'")
    ap.add_argument("--me", action="append", default=[], help="owner address(es); also env EMAIL_VAULT_ME")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    me = {x.strip().lower() for x in a.me + os.environ.get("EMAIL_VAULT_ME", "").split(",") if x.strip()}
    vault = find_vault(a.vault)
    today = dt.date.today()

    # every file Obsidian could link to
    targets: set[str] = set()
    for f in vault.rglob("*"):
        rel = f.relative_to(vault)
        if f.is_file() and not any(p.startswith(".") for p in rel.parts):
            posix = rel.as_posix().lower()
            targets.update({posix, f.name.lower()})
            if f.suffix == ".md":
                targets.update({posix[:-3], f.stem.lower()})

    wiki = vault / "wiki"
    pages = {p: p.read_text(encoding="utf-8") for p in iter_notes(wiki)}
    fms = {p: parse_frontmatter(t) for p, t in pages.items()}
    inbound: dict[str, int] = defaultdict(int)
    broken: list[tuple[str, str]] = []
    cited_raw: set[str] = set()

    for p, text in pages.items():
        for link in wikilinks(text):
            key = link.lower().rstrip("/")
            if key.endswith(".md"):
                key = key[:-3]
            if key not in targets:
                broken.append((p.relative_to(vault).as_posix(), link))
                continue
            base = key.rsplit("/", 1)[-1]
            if p.stem.lower() != base:
                inbound[base] += 1
            if key.startswith("raw/emails/") or "/" not in key:
                cited_raw.add(base)

    index_text = pages.get(wiki / "index.md", "").lower()
    report: dict[str, list] = defaultdict(list)
    email_owner: dict[str, list[str]] = defaultdict(list)

    for p, fm in fms.items():
        rel = p.relative_to(vault).as_posix()
        stem = p.stem.lower()
        t = fm.get("type")
        if t not in WIKI_TYPES:
            report["Missing or unknown `type` in frontmatter"].append(f"{rel} (type={t!r})")
        if stem in SPECIAL:
            continue
        if inbound.get(stem, 0) == 0:
            report["Orphan pages (no links from other wiki pages)"].append(rel)
        if f"[[{stem}" not in index_text and f"/{stem}]]" not in index_text and f"/{stem}|" not in index_text:
            report["Pages not listed in wiki/index.md"].append(rel)
        if t == "person":
            emails = fm.get("emails") or []
            if not emails:
                report["People pages with no `emails`"].append(rel)
            for e in emails if isinstance(emails, list) else [emails]:
                email_owner[str(e).lower()].append(rel)
        if t == "thread":
            status = str(fm.get("status", "")).lower()
            due = parse_date(fm.get("due"))
            last = parse_date(fm.get("last_activity"))
            if status in ("open", "waiting") and due and due < today:
                report["Overdue threads (status open/waiting, due date passed)"].append(f"{rel} (due {due}, {status})")
            if status == "waiting" and last and (today - last).days > a.stale_days:
                report[f"Waiting > {a.stale_days} days with no new email"].append(f"{rel} (last {last})")
            if not fm.get("sources"):
                report["Thread pages with no `sources`"].append(rel)

    for e, owners in email_owner.items():
        if len(owners) > 1:
            report["Same address on several people pages (merge?)"].append(f"{e}: {', '.join(owners)}")
    for src, link in broken:
        report["Broken wikilinks"].append(f"{src} -> [[{link}]]")

    status_counts: dict[str, int] = defaultdict(int)
    sender_counts: dict[str, int] = defaultdict(int)
    for p in iter_notes(vault / "raw" / "emails"):
        fm = parse_frontmatter(p.read_text(encoding="utf-8"))
        st = fm.get("wiki_status", "pending")
        status_counts[st] += 1
        sender = str(fm.get("from_email", "")).lower()
        if sender and st != "skipped" and sender not in me and not NOREPLY_RE.search(sender):
            sender_counts[sender] += 1
        if st == "compiled" and p.stem.lower() not in cited_raw:
            report["Compiled emails that no wiki page cites"].append(p.relative_to(vault).as_posix())

    for addr, n in sorted(sender_counts.items(), key=lambda x: -x[1]):
        if n >= a.min_emails and addr not in email_owner:
            report[f"Frequent correspondents (>= {a.min_emails} emails) with no person page"].append(f"{addr} ({n})")

    inbox = vault / "00-inbox"
    waiting = [f.name for f in inbox.glob("*") if f.suffix.lower() in (".msg", ".eml")] if inbox.exists() else []
    failed = [f.name for f in (inbox / "_failed").glob("*") if f.suffix.lower() in (".msg", ".eml")] \
        if (inbox / "_failed").exists() else []

    summary = {
        "wiki_pages": len(pages),
        "raw_emails": dict(status_counts),
        "inbox_unconverted": len(waiting),
        "inbox_failed": failed,
        "issues": {k: v for k, v in report.items()},
    }
    if a.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0
    print(f"# Vault lint — {today}\n")
    print(f"- Wiki pages: {len(pages)}")
    print(f"- Raw emails: " + ", ".join(f"{k} {v}" for k, v in sorted(status_counts.items())) if status_counts else "- Raw emails: 0")
    print(f"- Unconverted files in 00-inbox: {len(waiting)}")
    if failed:
        print(f"- Failed conversions in 00-inbox/_failed: {len(failed)} ({', '.join(failed[:5])})")
    if not report:
        print("\nNo structural issues found.")
    for title, items in report.items():
        print(f"\n## {title} ({len(items)})")
        for i in items[:50]:
            print(f"- {i}")
        if len(items) > 50:
            print(f"- … and {len(items) - 50} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
