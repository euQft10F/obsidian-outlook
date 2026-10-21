#!/usr/bin/env python3
"""List raw emails waiting to be compiled into the wiki, grouped by thread; or mark them done.

    python pending.py                      # pending emails grouped by thread (oldest thread first)
    python pending.py --limit 10           # only the first 10 threads (one compile batch)
    python pending.py --json
    python pending.py mark compiled "raw/emails/2026/10/....md" [...]
    python pending.py mark skipped  "raw/emails/....md" --reason newsletter
    python pending.py stats                # counts by wiki_status
    python pending.py link-threads         # point each email cited by a thread page back at it (wiki_thread)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from vaultlib import (
    Frontmatter,
    find_vault,
    iter_notes,
    parse_frontmatter,
    remove_frontmatter_field,
    set_frontmatter_field,
    utf8_stdout,
    wikilinks,
)

WIKI_STATUSES = ("pending", "compiled", "skipped")
MAX_REASON_LENGTH = 40
BYTES_PER_KB = 1000


class MarkRefusedError(Exception):
    """A path given to `mark` is not an email note we are willing to touch."""


@dataclass(slots=True)
class EmailNote:
    path: Path
    frontmatter: Frontmatter

    @property
    def date(self) -> str:
        return str(self.frontmatter.get("date"))

    @property
    def wiki_status(self) -> str:
        return str(self.frontmatter.get("wiki_status", "pending"))


@dataclass(slots=True)
class PendingThread:
    thread_id: str | None
    thread_subject: str | None
    emails: list[EmailNote]


def iter_email_notes(vault: Path) -> Iterator[EmailNote]:
    for path in iter_notes(vault / "raw" / "emails"):
        frontmatter = parse_frontmatter(path.read_text(encoding="utf-8"))
        if frontmatter.get("type") == "email":
            yield EmailNote(path, frontmatter)


def group_pending_by_thread(vault: Path) -> list[PendingThread]:
    """Pending emails grouped per thread, oldest thread first and oldest email first within a thread."""
    emails_by_thread: dict[str, list[EmailNote]] = defaultdict(list)
    for note in iter_email_notes(vault):
        if note.wiki_status == "pending":
            frontmatter = note.frontmatter
            thread_key = frontmatter.get("thread_id") or frontmatter.get("thread_subject", "?")
            emails_by_thread[thread_key].append(note)
    groups = sorted(emails_by_thread.values(), key=lambda emails: min(note.date for note in emails))
    threads = []
    for emails in groups:
        emails.sort(key=lambda note: note.date)
        first = emails[0].frontmatter
        threads.append(PendingThread(first.get("thread_id"), first.get("thread_subject"), emails))
    return threads


def list_pending(vault: Path, limit: int | None, as_json: bool) -> None:
    all_threads = group_pending_by_thread(vault)
    total_emails = sum(len(thread.emails) for thread in all_threads)
    shown = all_threads[:limit] if limit else all_threads
    listing = [{
        "thread_id": thread.thread_id,
        "thread_subject": thread.thread_subject,
        "emails": [{
            "path": note.path.relative_to(vault).as_posix(),
            "date": note.frontmatter.get("date"),
            "from": note.frontmatter.get("from"),
            "direction": note.frontmatter.get("direction"),
            "chars": note.path.stat().st_size,
        } for note in thread.emails],
    } for thread in shown]
    if as_json:
        summary = {"pending_total": total_emails, "threads_shown": len(listing), "threads": listing}
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return
    suffix = f" (showing {len(listing)})" if limit else ""
    print(f"Pending emails: {total_emails} in {len(all_threads)} threads{suffix}")
    for thread in listing:
        print(f"\n## {thread['thread_subject']}  [{thread['thread_id']}]")
        for email in thread["emails"]:
            size_kb = email["chars"] // BYTES_PER_KB
            print(f"  - {email['date']}  {email['from']}  ({email['direction']}, {size_kb} KB)  {email['path']}")


def resolve_email_note(vault: Path, raw_path: str) -> Path:
    """Absolute `.md` path under raw/emails/; refuses anything that is not an email note."""
    path = Path(raw_path) if Path(raw_path).is_absolute() else vault / raw_path
    if not path.suffix:
        path = path.with_suffix(".md")
    path = path.resolve()
    if not path.is_relative_to((vault / "raw" / "emails").resolve()) or path.suffix.lower() != ".md":
        raise MarkRefusedError(f"refused (not under raw/emails/): {raw_path}")
    if path.exists() and parse_frontmatter(path.read_text(encoding="utf-8")).get("type") != "email":
        raise MarkRefusedError(f"refused (not an email note): {raw_path}")
    return path


def mark_notes(vault: Path, status: str, raw_paths: list[str], reason: str | None) -> int:
    """Set `wiki_status` on each note; returns 1 if any path was refused or missing."""
    failures = 0
    for raw_path in raw_paths:
        try:
            path = resolve_email_note(vault, raw_path)
        except MarkRefusedError as refusal:
            print(f"  ! {refusal}")
            failures += 1
            continue
        if not path.exists() or not set_frontmatter_field(path, "wiki_status", status):
            print(f"  ! not found or no frontmatter: {raw_path}")
            failures += 1
            continue
        if reason:
            set_frontmatter_field(path, "wiki_skip_reason", json.dumps(reason))
        print(f"  {status}: {path.relative_to(vault).as_posix()}")
    return 1 if failures else 0


def thread_links_wanted(vault: Path, email_stems: set[str]) -> tuple[dict[str, str], list[str]]:
    """Email stem (lowercased) -> `[[thread page]]` for every email a thread page lists in `sources`,
    plus a warning per email cited by two thread pages (it goes to the newest `last_activity`)."""
    claims: dict[str, set[tuple[str, str]]] = defaultdict(set)  # email stem -> {(last_activity, thread stem)}
    for path in iter_notes(vault / "wiki" / "threads"):
        frontmatter = parse_frontmatter(path.read_text(encoding="utf-8"))
        if frontmatter.get("type") != "thread":
            continue
        sources = frontmatter.get("sources") or []
        claim = (str(frontmatter.get("last_activity", "")), path.stem)
        for source in sources if isinstance(sources, list) else [sources]:
            for target in wikilinks(str(source)):
                email_stem = target.rsplit("/", 1)[-1].removesuffix(".md").lower()
                if email_stem in email_stems:
                    claims[email_stem].add(claim)
    wanted, warnings = {}, []
    for email_stem, threads in claims.items():
        thread = max(threads)[1]
        wanted[email_stem] = f"[[{thread}]]"
        if len(threads) > 1:
            others = ", ".join(sorted(other for _, other in threads if other != thread))
            warnings.append(f"{email_stem} is cited by {thread} and {others}; linked to {thread}")
    return wanted, warnings


def link_threads(vault: Path, dry_run: bool, as_json: bool) -> int:
    """Set `wiki_thread` on emails cited by a thread page, clear it where no thread cites them any more.

    Only files whose value changes are rewritten, so a re-run touches nothing."""
    notes = {note.path.stem.lower(): note for note in iter_email_notes(vault)}
    wanted, warnings = thread_links_wanted(vault, set(notes))
    counts = {"set": 0, "cleared": 0, "unchanged": 0}
    for stem, note in notes.items():
        current = note.frontmatter.get("wiki_thread") or None
        link = wanted.get(stem)
        if current == link:
            counts["unchanged"] += 1 if link else 0
        elif link:
            counts["set"] += 1
            if not dry_run:
                set_frontmatter_field(note.path, "wiki_thread", json.dumps(link, ensure_ascii=False))
        else:
            counts["cleared"] += 1
            if not dry_run:
                remove_frontmatter_field(note.path, "wiki_thread")
    if as_json:
        print(json.dumps({**counts, "dry_run": dry_run, "warnings": warnings}, ensure_ascii=False))
        return 0
    prefix = "Would link" if dry_run else "Linked"
    print(f"{prefix}: {counts['set']} set, {counts['cleared']} cleared, {counts['unchanged']} unchanged")
    for warning in warnings:
        print(f"  ! {warning}")
    return 0


def print_stats(vault: Path) -> None:
    counts: dict[str, int] = defaultdict(int)
    for note in iter_email_notes(vault):
        counts[note.wiki_status] += 1
    print(json.dumps(dict(counts)))


def reason_text(value: str) -> str:
    if len(value) > MAX_REASON_LENGTH:
        raise argparse.ArgumentTypeError(f"must be at most {MAX_REASON_LENGTH} characters")
    return value


def build_parser() -> argparse.ArgumentParser:
    list_options = argparse.ArgumentParser(add_help=False)
    list_options.add_argument("--limit", type=int, help="max threads to show")
    list_options.add_argument("--json", action="store_true")

    # allow_abbrev=False: settings.json denies the literal `--vault`, so `--vau` must not reach it
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     parents=[list_options], allow_abbrev=False)
    parser.add_argument("--vault")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("list", parents=[list_options])
    mark = subcommands.add_parser("mark")
    mark.add_argument("status", choices=WIKI_STATUSES)
    mark.add_argument("paths", nargs="+")
    mark.add_argument("--reason", type=reason_text)
    subcommands.add_parser("stats")
    link = subcommands.add_parser("link-threads")
    link.add_argument("--dry-run", action="store_true")
    link.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    args = build_parser().parse_args(argv)
    vault = find_vault(args.vault)
    if args.command == "mark":
        return mark_notes(vault, args.status, args.paths, args.reason)
    if args.command == "link-threads":
        return link_threads(vault, args.dry_run, args.json)
    if args.command == "stats":
        print_stats(vault)
        return 0
    list_pending(vault, args.limit, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
