#!/usr/bin/env python3
"""Delete emails from the vault, or only their heavy parts, to free space. Preview first, then apply.

    python prune.py preview --before 2025-01-01 --status skipped     # what would go and the space freed
    python prune.py apply   --before 2025-01-01 --status skipped     # delete it (permanent, no trash)
    python prune.py preview "raw/emails/2026/10/....md" [...]        # named emails
    python prune.py preview --thread "Q3 budget (2026-10)"           # every email a thread page cites
    python prune.py preview --reason newsletter --level attachments  # keep the notes, drop the files
    python prune.py preview --from @news.example.com                 # one sender, or a whole domain
    python prune.py preview --duplicates                             # empty raw/msg/_duplicates/

Levels (--level, default all):
    originals    the archived .msg/.eml in raw/msg/; the note stays (`source_file` cleared, `pruned: originals`)
    attachments  originals + attachment files and their extracted text; the note stays and lists them as removed
    all          everything, the note included. Its entries leave raw/.email-index.json, so dropping the same
                 file into 00-inbox/ converts it again.
Emails forwarded as attachments (embedded notes) go with the email that contains them. Links on wiki pages
(log.md and drafts included) to a deleted file become plain text (`[[note|5 Oct, Priya]]` ->
`5 Oct, Priya (deleted)`), and frontmatter list items holding only such a link (thread `sources`) are dropped.
Selectors combine with AND; at least one is required. `apply` appends what it deleted to raw/.prune-log.jsonl;
a file it cannot delete (open in Outlook, read-only) is reported, and everything else still completes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from msg_to_md import INDEX_FILE, ConversionIndex
from vaultlib import (
    CODE_SPAN_RE,
    FRONTMATTER_RE,
    WIKILINK_RE,
    EmailNoteRefusedError,
    Frontmatter,
    file_link_keys,
    find_vault,
    iter_email_frontmatter,
    iter_files,
    iter_notes,
    link_key,
    parse_frontmatter,
    read_frontmatter,
    resolve_email_note,
    set_frontmatter_field,
    source_stems,
    utf8_stdout,
    wikilinks,
)

LEVELS = ("originals", "attachments", "all")  # each removes what the one before it does, and more
WIKI_STATUSES = ("pending", "compiled", "skipped")
PRUNE_LOG_FILE = "raw/.prune-log.jsonl"
DUPLICATES_DIR = "raw/msg/_duplicates"
RAW_FOLDERS = ("raw/emails", "raw/attachments", "raw/msg")  # never removed, even when emptied
BYTES_PER_KB = 1000
BYTES_PER_MB = 1_000_000

LINK_ONLY_LIST_ITEM_RE = re.compile(r"^\s*-\s*[\"']?(\[\[[^\]]+\]\])[\"']?\s*$")
# Bullets msg_to_md writes under a raw note's `## Attachments`: a stored attachment and an embedded email.
ATTACHMENT_BULLET_RE = re.compile(r"^- \[\[(raw/attachments/[^\]|]+)\|([^\]]+)\]\] \((\d+ KB)\).*$", re.M)
EMBEDDED_BULLET_RE = re.compile(r"^- Embedded email: \[\[([^\]|]+)\]\]$", re.M)


class SelectionError(Exception):
    """The selectors given cannot be used (none at all, an unknown thread page, a refused path)."""


@dataclass(slots=True)
class NotePlan:
    note: Path
    files: dict[Path, int]  # file -> bytes; what this level deletes for this email (the note itself only at all)
    embedded: bool  # selected because the email containing it was

    @property
    def size(self) -> int:
        return sum(self.files.values())


@dataclass(slots=True)
class PrunePlan:
    level: str
    notes: list[NotePlan] = field(default_factory=list)
    duplicates: dict[Path, int] = field(default_factory=dict)
    parents: dict[Path, set[str]] = field(default_factory=dict)  # kept raw note -> link_keys of embedded emails
    wiki: dict[Path, tuple[str, int]] = field(default_factory=dict)  # page -> (new text, links rewritten)
    emptied_threads: list[Path] = field(default_factory=list)  # thread pages left with no `sources`
    index_entries: int = 0  # raw/.email-index.json entries that point at deleted notes
    failed: list[str] = field(default_factory=list)  # "path: error" for what apply could not delete or write

    @property
    def files(self) -> list[Path]:
        return [file for plan in self.notes for file in plan.files] + list(self.duplicates)

    @property
    def size(self) -> int:
        return sum(plan.size for plan in self.notes) + sum(self.duplicates.values())


def sized(files: list[Path]) -> dict[Path, int]:
    return {file: file.stat().st_size for file in files}


def relative_to(vault: Path, path: Path) -> str:
    return path.relative_to(vault).as_posix()


# ---------------------------------------------------------------- selecting

def thread_source_stems(vault: Path, name: str) -> set[str]:
    """Lowercased stems of the emails a thread page lists in `sources`."""
    page = vault / "wiki" / "threads" / (Path(name).name.removesuffix(".md") + ".md")
    if not page.is_file():
        raise SelectionError(f"no thread page wiki/threads/{page.name}")
    return set(source_stems(read_frontmatter(page)))


def dated_before(frontmatter: Frontmatter, before: dt.date) -> bool:
    """True only for a readable `date` before `before`: an undated note is never old enough to delete."""
    try:
        return dt.date.fromisoformat(str(frontmatter.get("date") or "")[:10]) < before
    except ValueError:
        return False


def matches_filters(frontmatter: Frontmatter, args: argparse.Namespace) -> bool:
    if args.before and not dated_before(frontmatter, args.before):
        return False
    if args.status and str(frontmatter.get("wiki_status", "pending")) != args.status:
        return False
    if args.reason and str(frontmatter.get("wiki_skip_reason", "")).lower() != args.reason.lower():
        return False
    if args.sender:
        sender, wanted = str(frontmatter.get("from_email", "")).lower(), args.sender.lower()
        return sender.endswith(wanted) if wanted.startswith("@") else sender == wanted
    return True


def select_notes(vault: Path, args: argparse.Namespace, notes: dict[Path, Frontmatter]) -> list[tuple[Path, bool]]:
    """(note, embedded) for every email the selectors pick, plus the emails embedded in them."""
    if args.paths:
        candidates = []
        for raw_path in args.paths:
            try:
                path = resolve_email_note(vault, raw_path)
            except EmailNoteRefusedError as refusal:
                raise SelectionError(str(refusal)) from refusal
            if path not in notes:
                raise SelectionError(f"no email note: {raw_path}")
            candidates.append(path)
    else:
        candidates = list(notes)
    if args.thread:
        stems = thread_source_stems(vault, args.thread)
        candidates = [path for path in candidates if path.stem.lower() in stems]
    selected = {path: False for path in candidates if matches_filters(notes[path], args)}

    children: dict[str, list[Path]] = {}  # parent note's link_key -> embedded notes
    for path, frontmatter in notes.items():
        for parent in wikilinks(str(frontmatter.get("embedded_in") or "")):
            children.setdefault(link_key(parent), []).append(path)
    queue = list(selected)
    while queue:
        for child in children.get(link_key(relative_to(vault, queue.pop())), []):
            if child not in selected:
                selected[child] = True
                queue.append(child)
    return sorted(selected.items())


# ---------------------------------------------------------------- planning

def original_file(vault: Path, frontmatter: Frontmatter) -> Path | None:
    """The archived original a note's `source_file` names, if it exists inside raw/msg/."""
    source = str(frontmatter.get("source_file") or "")
    source = next(iter(wikilinks(source)), source)  # newer notes: "[[raw/msg/…]]", older: plain path
    if not source:
        return None
    path = (vault / source).resolve()
    return path if path.is_file() and path.is_relative_to(vault / "raw" / "msg") else None


def attachment_files(vault: Path, note: Path) -> list[Path]:
    """Files in the note's attachment folder, raw/attachments/YYYY/MM/<note stem>/ (as msg_to_md names it)."""
    folder = vault / "raw" / "attachments" / note.relative_to(vault / "raw" / "emails").parent / note.stem
    if not folder.is_dir() or not folder.resolve().is_relative_to(vault / "raw" / "attachments"):
        return []
    return sorted(file for file in folder.rglob("*") if file.is_file())


def files_to_delete(vault: Path, note: Path, frontmatter: Frontmatter, level: str) -> list[Path]:
    files = [original] if (original := original_file(vault, frontmatter)) else []
    if level != "originals":
        files += attachment_files(vault, note)
    if level == "all":
        files.append(note)
    return files


def rewrite_dead_links(text: str, dead: set[str]) -> tuple[str, int]:
    """`text` with frontmatter list items that only hold a dead link dropped and other dead links turned into
    plain text `alias (deleted)`; code spans are left as they are. Returns (text, links rewritten)."""
    count = 0
    frontmatter = FRONTMATTER_RE.match(text)
    if frontmatter:
        kept = []
        for line in frontmatter.group(1).split("\n"):
            item = LINK_ONLY_LIST_ITEM_RE.match(line)
            if item and any(link_key(target) in dead for target in wikilinks(item.group(1))):
                count += 1
            else:
                kept.append(line)
        start, end = frontmatter.span(1)
        text = text[:start] + "\n".join(kept) + text[end:]

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        target = match.group(1).strip().rstrip("\\")  # `[[x\|alias]]` inside a table
        if link_key(target) not in dead:
            return match.group(0)
        count += 1
        label = (match.group(2) or "").strip() or target.rsplit("/", 1)[-1].removesuffix(".md")
        return f"{label} (deleted)"

    pieces, last = [], 0
    for code in CODE_SPAN_RE.finditer(text):
        pieces += [WIKILINK_RE.sub(replace, text[last:code.start()]), code.group(0)]
        last = code.end()
    pieces.append(WIKILINK_RE.sub(replace, text[last:]))
    return "".join(pieces), count


def plan_wiki(vault: Path, deleted: list[Path], plan: PrunePlan) -> None:
    """Fill plan.wiki and plan.emptied_threads: links that only resolve to a deleted file (another file of the
    same name keeps a bare-name link alive). Works before deleting (preview) and after (apply)."""
    plan.wiki, plan.emptied_threads = {}, []
    gone = set(deleted)
    dead = set().union(*(file_link_keys(relative_to(vault, file)) for file in gone))
    for file in iter_files(vault):
        if file not in gone:
            dead -= file_link_keys(relative_to(vault, file))
    if not dead:
        return
    for page in iter_notes(vault / "wiki"):
        text = page.read_text(encoding="utf-8")
        new_text, count = rewrite_dead_links(text, dead)
        if not count:
            continue
        plan.wiki[page] = (new_text, count)
        before, after = parse_frontmatter(text), parse_frontmatter(new_text)
        if after.get("type") == "thread" and before.get("sources") and not after.get("sources"):
            plan.emptied_threads.append(page)


def count_index_entries(vault: Path, deleted_notes: set[str]) -> int:
    index = ConversionIndex(vault / INDEX_FILE)
    return sum(note in deleted_notes for table in index.data.values() for note in table.values())


def build_plan(vault: Path, args: argparse.Namespace) -> PrunePlan:
    selectors_given = any((args.paths, args.thread, args.before, args.status, args.reason, args.sender))
    if not (selectors_given or args.duplicates):
        raise SelectionError("refused: name emails or give at least one selector (see --help)")
    plan = PrunePlan(args.level)
    if selectors_given:
        notes = dict(iter_email_frontmatter(vault))
        for note, embedded in select_notes(vault, args, notes):
            if files := files_to_delete(vault, note, notes[note], args.level):
                plan.notes.append(NotePlan(note, sized(files), embedded))
        if plan.level == "all":  # an embedded email deleted without its parent: the parent's bullet names it
            deleted = {note_plan.note for note_plan in plan.notes}
            by_key = {link_key(relative_to(vault, path)): path for path in notes}
            for note in deleted:
                for parent_link in wikilinks(str(notes[note].get("embedded_in") or "")):
                    parent = by_key.get(link_key(parent_link))
                    if parent is not None and parent not in deleted:
                        plan.parents.setdefault(parent, set()).add(link_key(relative_to(vault, note)))
    if args.duplicates and (duplicates := vault / DUPLICATES_DIR).is_dir():
        plan.duplicates = sized(sorted(file for file in duplicates.rglob("*") if file.is_file()))
    plan.index_entries = count_index_entries(vault, notes_deleted(vault, plan))
    plan_wiki(vault, plan.files, plan)
    return plan


def notes_deleted(vault: Path, plan: PrunePlan) -> set[str]:
    """Vault-relative paths of the email notes the plan deletes (level all), as the index stores them."""
    return {relative_to(vault, note_plan.note) for note_plan in plan.notes if note_plan.note in note_plan.files}


# ---------------------------------------------------------------- applying

def delete_files(files: dict[Path, int], failed: list[str]) -> dict[Path, int]:
    """Delete each file; returns the ones now gone, with their sizes. Failures are appended to `failed`."""
    removed = {}
    for file, size in files.items():
        try:
            file.unlink(missing_ok=True)
        except OSError as error:
            failed.append(f"{file}: {error.strerror or error}")
            continue
        removed[file] = size
    return removed


def mark_note_pruned(vault: Path, note_plan: NotePlan, removed: dict[Path, int], level: str,
                     today: dt.date) -> None:
    """Record on a kept note what is gone: `pruned` (never lowered), cleared `source_file`, attachment bullets.
    `level` counts only if every planned file went; with just the original gone it is `originals`."""
    removed_keys = {link_key(relative_to(vault, file)) for file in removed}
    text = note_plan.note.read_text(encoding="utf-8")
    new_text = ATTACHMENT_BULLET_RE.sub(
        lambda bullet: bullet.group(0) if link_key(bullet.group(1)) not in removed_keys
        else f"- {bullet.group(2)} ({bullet.group(3)}) · removed (pruned {today.isoformat()})", text)
    if new_text != text:
        note_plan.note.write_text(new_text, encoding="utf-8")
    original = next((file for file in note_plan.files if file.is_relative_to(vault / "raw" / "msg")), None)
    if original in removed:
        set_frontmatter_field(note_plan.note, "source_file", '""')
    reached = level if len(removed) == len(note_plan.files) else "originals" if original in removed else None
    previous = str(read_frontmatter(note_plan.note).get("pruned", ""))
    if reached and (previous not in LEVELS or LEVELS.index(reached) > LEVELS.index(previous)):
        set_frontmatter_field(note_plan.note, "pruned", reached)


def mark_embedded_removed(parent: Path, child_keys: set[str], today: dt.date) -> None:
    """Turn a kept note's `- Embedded email: [[…]]` bullets for deleted emails into plain text."""
    text = parent.read_text(encoding="utf-8")
    new_text = EMBEDDED_BULLET_RE.sub(
        lambda bullet: bullet.group(0) if link_key(bullet.group(1)) not in child_keys
        else f"- Embedded email: {bullet.group(1).rsplit('/', 1)[-1]} · removed (pruned {today.isoformat()})", text)
    if new_text != text:
        parent.write_text(new_text, encoding="utf-8")


def remove_empty_folders(vault: Path, files: list[Path]) -> None:
    """Remove folders the deletions emptied, up to (not including) raw/emails, raw/attachments and raw/msg."""
    keep = {vault / folder for folder in RAW_FOLDERS}
    for folder in sorted({file.parent for file in files}, key=lambda path: len(path.parts), reverse=True):
        while folder not in keep and folder.is_relative_to(vault) and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent


def apply_plan(vault: Path, plan: PrunePlan) -> PrunePlan:
    """Delete what `plan` lists, then bring the log, index, raw notes and wiki in line with what actually went.
    Returns that as a plan: a file that could not be deleted is left out of it and listed in `failed`."""
    today = dt.date.today()
    done = PrunePlan(plan.level)
    for note_plan in plan.notes:
        removed = delete_files(note_plan.files, done.failed)
        if removed:
            done.notes.append(NotePlan(note_plan.note, removed, note_plan.embedded))
    done.duplicates = delete_files(plan.duplicates, done.failed)
    remove_empty_folders(vault, done.files)

    timestamp = dt.datetime.now().isoformat(timespec="seconds")  # first, so the deletions are on record
    lines = [{"at": timestamp, "level": done.level, "note": relative_to(vault, note_plan.note),
              "files": len(note_plan.files), "bytes": note_plan.size} for note_plan in done.notes]
    if done.duplicates:
        lines.append({"at": timestamp, "level": "duplicates", "files": len(done.duplicates),
                      "bytes": sum(done.duplicates.values())})
    if lines:
        with (vault / PRUNE_LOG_FILE).open("a", encoding="utf-8") as log:
            log.writelines(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)

    deleted_notes = notes_deleted(vault, done)
    if deleted_notes:
        index = ConversionIndex(vault / INDEX_FILE)
        for table in index.data.values():
            for key in [key for key, note in table.items() if note in deleted_notes]:
                del table[key]
                done.index_entries += 1
        index.save()

    planned = {note_plan.note: note_plan for note_plan in plan.notes}
    for note_plan in done.notes:
        if note_plan.note not in note_plan.files:  # kept note: record what went
            mark_note_pruned(vault, planned[note_plan.note], note_plan.files, done.level, today)
    gone_keys = {link_key(path) for path in deleted_notes}
    for parent, child_keys in plan.parents.items():
        if parent.exists() and child_keys & gone_keys:
            mark_embedded_removed(parent, child_keys & gone_keys, today)
            done.parents[parent] = child_keys & gone_keys

    if done.failed:  # some files are still there: links to them must stay
        plan_wiki(vault, done.files, done)
    else:
        done.wiki, done.emptied_threads = plan.wiki, plan.emptied_threads
    for page, (new_text, _count) in list(done.wiki.items()):
        try:
            page.write_text(new_text, encoding="utf-8")
        except OSError as error:
            done.failed.append(f"{page}: {error.strerror or error}")
            del done.wiki[page]
    return done


# ---------------------------------------------------------------- reporting

def megabytes(size: int) -> str:
    """Size for people: KB below a megabyte (most notes), MB above."""
    return f"{size / BYTES_PER_MB:.1f} MB" if size >= BYTES_PER_MB else f"{size / BYTES_PER_KB:.0f} KB"


def counted(number: int, noun: str, plural: str = "") -> str:
    return f"{number} {noun if number == 1 else plural or noun + 's'}"


def summarize(vault: Path, plan: PrunePlan, *, applied: bool) -> dict:
    """The report, as JSON data."""
    return {
        "vault": str(vault), "applied": applied, "level": plan.level,
        "notes": [{"note": relative_to(vault, note_plan.note), "embedded": note_plan.embedded,
                   "files": len(note_plan.files), "bytes": note_plan.size} for note_plan in plan.notes],
        "duplicates": len(plan.duplicates), "files": len(plan.files), "bytes": plan.size,
        "index_entries": plan.index_entries,
        "raw_notes": sorted(relative_to(vault, parent) for parent in plan.parents),
        "wiki": {relative_to(vault, page): count for page, (_text, count) in plan.wiki.items()},
        "emptied_threads": [relative_to(vault, page) for page in plan.emptied_threads],
        "failed": plan.failed,
    }


def print_summary(summary: dict) -> None:
    applied = summary["applied"]
    print(f"Vault: {summary['vault']}{'' if applied else '  (preview: nothing deleted)'}")
    if not summary["files"] and not summary["failed"]:
        print("Nothing to delete: no emails match, or what this level removes is already gone.")
        return
    embedded = sum(note["embedded"] for note in summary["notes"])
    print(f"{'Deleted' if applied else 'Would delete'} ({summary['level']}): {counted(len(summary['notes']), 'email')}"
          + (f" ({embedded} embedded)" if embedded else "")
          + f", {counted(summary['files'], 'file')}, {megabytes(summary['bytes'])}")
    for note in summary["notes"]:
        mark = " (embedded)" if note["embedded"] else ""
        print(f"  - {note['note']}{mark}  ({counted(note['files'], 'file')}, {megabytes(note['bytes'])})")
    if summary["duplicates"]:
        print(f"  - {DUPLICATES_DIR}/: {counted(summary['duplicates'], 'file')}")
    if summary["index_entries"]:
        print(f"Index: {counted(summary['index_entries'], 'entry', 'entries')} "
              f"{'removed' if applied else 'to remove'} (the same file dropped into 00-inbox/ converts again)")
    if summary["raw_notes"]:
        print(f"Raw notes whose embedded-email bullet {'was' if applied else 'is'} marked removed:")
        for note in summary["raw_notes"]:
            print(f"  - {note}")
    if summary["wiki"]:
        links = sum(summary["wiki"].values())
        print(f"Wiki: {counted(links, 'link')} on {counted(len(summary['wiki']), 'page')} "
              f"{'became' if applied else 'become'} plain text marked (deleted)")
        for page, count in summary["wiki"].items():
            print(f"  - {page} ({count})")
    if summary["emptied_threads"]:
        print("Thread pages left with no sources:")
        for page in summary["emptied_threads"]:
            print(f"  - {page}")
    if summary["failed"]:
        print(f"Could not delete or update {counted(len(summary['failed']), 'file')} (left as they were):")
        for failure in summary["failed"]:
            print(f"  ! {failure}")
    if not applied:
        print("Preview only. `prune.py apply` with the same options deletes this permanently.")


# ---------------------------------------------------------------- command line

def iso_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a date like 2026-01-31") from error


def build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False: settings.json denies the literal `--vault`, so `--vau` must not reach it
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    parser.add_argument("--vault")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("preview", "show what would be deleted (writes nothing)"),
                            ("apply", "delete it, permanently")):
        command = subcommands.add_parser(name, help=help_text, allow_abbrev=False)
        command.add_argument("paths", nargs="*", help="email notes under raw/emails/")
        command.add_argument("--thread", help="the emails a wiki/threads/ page lists in `sources` (page name)")
        command.add_argument("--before", type=iso_date, help="emails dated before this day (YYYY-MM-DD)")
        command.add_argument("--status", choices=WIKI_STATUSES, help="emails with this wiki_status")
        command.add_argument("--reason", help="skipped emails with this wiki_skip_reason (e.g. newsletter)")
        command.add_argument("--from", dest="sender", help="sender address, or @domain for a whole domain")
        command.add_argument("--level", choices=LEVELS, default="all", help="how much to remove (default all)")
        command.add_argument("--duplicates", action="store_true", help="also empty raw/msg/_duplicates/")
        command.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    args = build_parser().parse_args(argv)
    vault = find_vault(args.vault)
    try:
        plan = build_plan(vault, args)
    except SelectionError as refusal:
        print(refusal)
        return 2
    applied = args.command == "apply"
    summary = summarize(vault, apply_plan(vault, plan) if applied else plan, applied=applied)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    else:
        print_summary(summary)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
