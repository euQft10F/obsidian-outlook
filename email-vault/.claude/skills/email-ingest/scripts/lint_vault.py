#!/usr/bin/env python3
"""Deterministic health check of the email wiki. Prints a markdown report.

Checks the things a script can count, so the LLM only has to judge the rest:
broken links, orphans, pages missing from index.md, bad frontmatter, duplicate people,
overdue / stale threads, compiled emails no wiki page cites, inbox backlog and failures,
plus owner setup (template placeholders left in CLAUDE.md, no owner address configured).

    python lint_vault.py [--stale-days 7] [--json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from vaultlib import (
    EMAIL_SUFFIXES,
    FAILED_DIRNAME,
    INBOX_DIRNAME,
    Frontmatter,
    check_setup,
    find_vault,
    iter_files,
    iter_notes,
    owner_addresses,
    parse_frontmatter,
    utf8_stdout,
    wikilinks,
)

WIKI_PAGE_TYPES = {"person", "org", "thread", "project", "answer", "digest", "index", "log", "overview"}
UNINDEXED_PAGE_STEMS = {"index", "log"}
AUTOMATED_SENDER_RE = re.compile(r"no-?reply|do-?not-?reply|notification|mailer-daemon|postmaster|calendar", re.I)
MAX_ITEMS_PER_SECTION = 50
MAX_FAILED_NAMES_SHOWN = 5

# Report section headings (shown to the user and used as JSON keys).
BAD_TYPE = "Missing or unknown `type` in frontmatter"
ORPHAN = "Orphan pages (no links from other wiki pages)"
NOT_INDEXED = "Pages not listed in wiki/index.md"
PERSON_NO_EMAILS = "People pages with no `emails`"
OVERDUE = "Overdue threads (status open/waiting, due date passed)"
THREAD_NO_SOURCES = "Thread pages with no `sources`"
DUPLICATE_PEOPLE = "Same address on several people pages (merge?)"
BROKEN_LINKS = "Broken wikilinks"
UNCITED_EMAILS = "Compiled emails that no wiki page cites"


@dataclass(slots=True)
class WikiPage:
    path: Path
    text: str
    frontmatter: Frontmatter


def parse_iso_date(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def build_link_targets(vault: Path) -> set[str]:
    """Every name an Obsidian wikilink could resolve to (lowercased path and bare filename, with and without .md)."""
    targets: set[str] = set()
    for file in iter_files(vault):
        posix_path = file.relative_to(vault).as_posix().lower()
        targets.update({posix_path, file.name.lower()})
        if file.suffix == ".md":
            targets.update({posix_path[:-3], file.stem.lower()})
    return targets


def load_wiki_pages(wiki_dir: Path) -> list[WikiPage]:
    pages = []
    for path in iter_notes(wiki_dir):
        text = path.read_text(encoding="utf-8")
        pages.append(WikiPage(path, text, parse_frontmatter(text)))
    return pages


@dataclass(slots=True)
class LinkAnalysis:
    inbound_counts: dict[str, int]
    broken: list[tuple[str, str]]  # (page relative path, link text)
    cited_email_stems: set[str]


def analyse_links(vault: Path, pages: list[WikiPage], targets: set[str]) -> LinkAnalysis:
    inbound_counts: dict[str, int] = defaultdict(int)
    broken: list[tuple[str, str]] = []
    cited_email_stems: set[str] = set()
    for page in pages:
        for link in wikilinks(page.text):
            key = link.lower().rstrip("/")
            key = key.removesuffix(".md")
            if key not in targets:
                broken.append((page.path.relative_to(vault).as_posix(), link))
                continue
            target_name = key.rsplit("/", 1)[-1]
            if page.path.stem.lower() != target_name:
                inbound_counts[target_name] += 1
            if key.startswith("raw/emails/") or "/" not in key:
                cited_email_stems.add(target_name)
    return LinkAnalysis(inbound_counts, broken, cited_email_stems)


class PageChecker:
    """Per-page checks. Findings accumulate in `report`; person-page addresses in `address_owners`."""

    def __init__(self, vault: Path, index_text: str, inbound_counts: dict[str, int], today: dt.date, stale_days: int):
        self.vault = vault
        self.index_text = index_text
        self.inbound_counts = inbound_counts
        self.today = today
        self.stale_days = stale_days
        self.report: dict[str, list[str]] = defaultdict(list)
        self.address_owners: dict[str, list[str]] = defaultdict(list)

    def check(self, page: WikiPage) -> None:
        relative = page.path.relative_to(self.vault).as_posix()
        stem = page.path.stem.lower()
        page_type = page.frontmatter.get("type")
        if page_type not in WIKI_PAGE_TYPES:
            self.report[BAD_TYPE].append(f"{relative} (type={page_type!r})")
        if stem in UNINDEXED_PAGE_STEMS:
            return
        if self.inbound_counts.get(stem, 0) == 0:
            self.report[ORPHAN].append(relative)
        index_text = self.index_text
        if f"[[{stem}" not in index_text and f"/{stem}]]" not in index_text and f"/{stem}|" not in index_text:
            self.report[NOT_INDEXED].append(relative)
        if page_type == "person":
            self._check_person(page, relative)
        elif page_type == "thread":
            self._check_thread(page, relative)

    def _check_person(self, page: WikiPage, relative: str) -> None:
        addresses = page.frontmatter.get("emails") or []
        if not addresses:
            self.report[PERSON_NO_EMAILS].append(relative)
        for address in addresses if isinstance(addresses, list) else [addresses]:
            self.address_owners[str(address).lower()].append(relative)

    def _check_thread(self, page: WikiPage, relative: str) -> None:
        status = str(page.frontmatter.get("status", "")).lower()
        due = parse_iso_date(page.frontmatter.get("due"))
        last_activity = parse_iso_date(page.frontmatter.get("last_activity"))
        if status in ("open", "waiting") and due and due < self.today:
            self.report[OVERDUE].append(f"{relative} (due {due}, {status})")
        if status == "waiting" and last_activity and (self.today - last_activity).days > self.stale_days:
            heading = f"Waiting > {self.stale_days} days with no new email"
            self.report[heading].append(f"{relative} (last {last_activity})")
        if not page.frontmatter.get("sources"):
            self.report[THREAD_NO_SOURCES].append(relative)


def scan_raw_emails(vault: Path, cited_email_stems: set[str], owner: set[str], report: dict[str, list[str]]
                    ) -> tuple[dict[str, int], dict[str, int]]:
    """Returns (emails per wiki_status, emails per sender worth a person page)."""
    status_counts: dict[str, int] = defaultdict(int)
    sender_counts: dict[str, int] = defaultdict(int)
    for path in iter_notes(vault / "raw" / "emails"):
        frontmatter = parse_frontmatter(path.read_text(encoding="utf-8"))
        status = frontmatter.get("wiki_status", "pending")
        status_counts[status] += 1
        sender = str(frontmatter.get("from_email", "")).lower()
        if sender and status != "skipped" and sender not in owner and not AUTOMATED_SENDER_RE.search(sender):
            sender_counts[sender] += 1
        if status == "compiled" and path.stem.lower() not in cited_email_stems:
            report[UNCITED_EMAILS].append(path.relative_to(vault).as_posix())
    return status_counts, sender_counts


def email_file_names(folder: Path) -> list[str]:
    if not folder.exists():
        return []
    return [file.name for file in folder.glob("*") if file.suffix.lower() in EMAIL_SUFFIXES]


def render_markdown(today: dt.date, page_count: int, status_counts: dict[str, int], unconverted: list[str],
                    failed: list[str], setup: list[str], report: dict[str, list[str]]) -> None:
    print(f"# Vault lint — {today}\n")
    print(f"- Wiki pages: {page_count}")
    if status_counts:
        print("- Raw emails: " + ", ".join(f"{status} {count}" for status, count in sorted(status_counts.items())))
    else:
        print("- Raw emails: 0")
    print(f"- Unconverted files in 00-inbox: {len(unconverted)}")
    if failed:
        shown = ", ".join(failed[:MAX_FAILED_NAMES_SHOWN])
        print(f"- Failed conversions in 00-inbox/_failed: {len(failed)} ({shown})")
    if setup:
        print(f"\n## Setup ({len(setup)})")
        for warning in setup:
            print(f"- {warning}")
    if not report:
        print("\nNo structural issues found.")
    for title, items in report.items():
        print(f"\n## {title} ({len(items)})")
        for item in items[:MAX_ITEMS_PER_SECTION]:
            print(f"- {item}")
        if len(items) > MAX_ITEMS_PER_SECTION:
            print(f"- … and {len(items) - MAX_ITEMS_PER_SECTION} more")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault")
    parser.add_argument("--stale-days", type=int, default=7)
    parser.add_argument("--min-emails", type=int, default=3, help="threshold for 'frequent correspondent'")
    parser.add_argument("--me", action="append", default=[], help="owner address(es); also env EMAIL_VAULT_ME")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    args = build_parser().parse_args(argv)
    vault = find_vault(args.vault)
    today = dt.date.today()
    owner = owner_addresses(args.me)

    wiki_dir = vault / "wiki"
    pages = load_wiki_pages(wiki_dir)
    links = analyse_links(vault, pages, build_link_targets(vault))
    index_page = next((page for page in pages if page.path == wiki_dir / "index.md"), None)
    index_text = index_page.text.lower() if index_page else ""

    checker = PageChecker(vault, index_text, links.inbound_counts, today, args.stale_days)
    for page in pages:
        checker.check(page)
    report, address_owners = checker.report, checker.address_owners
    for address, owners in address_owners.items():
        if len(owners) > 1:
            report[DUPLICATE_PEOPLE].append(f"{address}: {', '.join(owners)}")
    for source, link in links.broken:
        report[BROKEN_LINKS].append(f"{source} -> [[{link}]]")

    status_counts, sender_counts = scan_raw_emails(vault, links.cited_email_stems, owner, report)
    for address, count in sorted(sender_counts.items(), key=lambda item: -item[1]):
        if count >= args.min_emails and address not in address_owners:
            report[f"Frequent correspondents (>= {args.min_emails} emails) with no person page"].append(
                f"{address} ({count})")

    inbox = vault / INBOX_DIRNAME
    unconverted = email_file_names(inbox)
    failed = email_file_names(inbox / FAILED_DIRNAME)
    setup = check_setup(vault, owner)

    if args.json:
        summary = {
            "wiki_pages": len(pages),
            "raw_emails": dict(status_counts),
            "inbox_unconverted": len(unconverted),
            "inbox_failed": failed,
            "setup": setup,
            "issues": dict(report),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    else:
        render_markdown(today, len(pages), status_counts, unconverted, failed, setup, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
