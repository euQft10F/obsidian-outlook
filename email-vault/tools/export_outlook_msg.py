#!/usr/bin/env python3
"""Export Outlook emails as .msg files, by default into the vault's 00-inbox/ folder.

Keeps incremental state so it can run on a timer. Uses the classic Outlook desktop app
through COM (Windows + pywin32); the "new Outlook" has no COM API. Nothing is exported
unless you pick an action: --selection, --days, --since or --list-folders.

    uv run python tools/export_outlook_msg.py --selection          # emails selected in Outlook
    uv run python tools/export_outlook_msg.py --days 7             # Inbox, last 7 days
    uv run python tools/export_outlook_msg.py --days 30 --folder "Inbox/Projects/Q3" --folder "Sent Items"
    uv run python tools/export_outlook_msg.py --since 2026-09-01 --folder "Other Mailbox/Inbox"
    uv run python tools/export_outlook_msg.py --days 7 --output-dir D:/scratch/msgs --state-file D:/scratch/state.json
    uv run python tools/export_outlook_msg.py --days 1 --convert   # export, then run msg_to_md.py
    uv run python tools/export_outlook_msg.py --list-folders       # show folder paths you can use

Already-exported emails are remembered by Outlook EntryID in the state file
(default raw/.outlook-export-state.json), so repeated runs only save new mail.
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

VAULT_ROOT = Path(__file__).resolve().parent.parent
STATE_RELATIVE_PATH = Path("raw") / ".outlook-export-state.json"
IGNORE_FROM_PATH = Path(__file__).resolve().parent / "ignore.from.txt"
IGNORE_SUBJECT_PATH = Path(__file__).resolve().parent / "ignore.subject.txt"
CONVERTER_RELATIVE_PATH = Path(".claude/skills/email-ingest/scripts/msg_to_md.py")
CONVERTER_BATCH_SIZE = 50  # keeps the converter's command line under Windows' length limit

sys.path.insert(0, str(VAULT_ROOT / CONVERTER_RELATIVE_PATH.parent))  # shared helpers live beside the converter
from vaultlib import INBOX_DIRNAME, find_vault, sanitize_filename, utf8_stdout  # noqa: E402

OL_MSG_UNICODE = 9   # OlSaveAsType.olMSGUnicode
OL_MAIL_ITEM = 43    # OlObjectClass.olMail (skips meeting requests, NDRs, tasks)
OL_DEFAULT_FOLDERS = {  # OlDefaultFolders
    "inbox": 6,
    "sent items": 5,
    "sent": 5,
    "deleted items": 3,
    "drafts": 16,
    "junk email": 23,
    "junk": 23,
    "outbox": 4,
}


class ExportError(Exception):
    """A problem the user can act on (Outlook missing, unknown folder, ...)."""


# --------------------------------------------------------------------------- Outlook helpers

def item_received_time(item: Any) -> dt.datetime:
    """Wall-clock time of an Outlook item (pywin32 tags local times as UTC; drop the tag)."""
    time = item.ReceivedTime or item.SentOn
    return dt.datetime(time.year, time.month, time.day, time.hour, time.minute, time.second)


def connect_to_outlook() -> tuple[Any, Any]:
    """Return the Outlook Application object and its MAPI namespace."""
    try:
        import win32com.client  # type: ignore[import-not-found]
        application = win32com.client.Dispatch("Outlook.Application")
        return application, application.GetNamespace("MAPI")
    except ImportError:
        raise ExportError("pywin32 is not installed. On Windows run `uv sync` "
                          "(it installs pywin32), or `uv pip install pywin32`.") from None
    except Exception as error:  # pywintypes.com_error; not importable without pywin32
        raise ExportError(f"Could not start classic Outlook via COM ({error}). "
                          "The 'new Outlook' is not supported.") from error


def find_child_folder(parent: Any, name: str) -> Any | None:
    return next((child for child in parent.Folders if child.Name.lower() == name.lower()), None)


def resolve_folder(namespace: Any, path: str) -> Any:
    """Resolve 'Inbox', 'Inbox/Sub/Sub', 'Sent Items' or '<mailbox or PST name>/Folder/Sub'."""
    root_name, *subfolder_names = [s.strip() for s in re.split(r"[\\/]", path) if s.strip()] or [""]
    if not root_name:
        raise ExportError("empty folder path")

    default_folder_id = OL_DEFAULT_FOLDERS.get(root_name.lower())
    if default_folder_id is not None:
        folder = namespace.GetDefaultFolder(default_folder_id)
    else:
        folder = find_child_folder(namespace, root_name)
        if folder is None:
            mailboxes = ", ".join(root.Name for root in namespace.Folders)
            raise ExportError(f"'{root_name}' is neither a default folder nor a mailbox ({mailboxes})")

    for name in subfolder_names:
        child = find_child_folder(folder, name)
        if child is None:
            raise ExportError(f"no subfolder '{name}' under '{folder.Name}'")
        folder = child
    return folder


def print_folder_tree(folder: Any, depth: int = 0, max_depth: int = 3) -> None:
    try:
        item_count: int | str = folder.Items.Count
    except Exception:  # some special folders refuse item access
        item_count = "?"
    print(f"{'    ' * depth}{folder.Name}  ({item_count} items)")
    if depth < max_depth:
        for subfolder in folder.Folders:
            print_folder_tree(subfolder, depth + 1, max_depth)


# --------------------------------------------------------------------------- exporting

def load_state(path: Path) -> dict[str, Any]:
    """EntryID -> {"file", "received"} for every email exported so far."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("exported", {})


def save_state(path: Path, exported: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch_path = path.with_name(path.name + ".tmp")  # replace atomically: no truncated state on a crash
    scratch_path.write_text(json.dumps({"exported": exported}, indent=1, ensure_ascii=False), encoding="utf-8")
    scratch_path.replace(path)


def load_ignore_list(path: Path) -> list[str]:
    """Lower-cased patterns from a text file: one per line, blank lines and '#' comments skipped."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return [line.strip().lower() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def sender_addresses(item: Any) -> list[str]:
    """Lower-cased strings identifying the sender: display name, address and SMTP address if resolvable."""
    values = [getattr(item, "SenderName", ""), getattr(item, "SenderEmailAddress", "")]
    try:  # Exchange senders carry an X.500 address; ask for the SMTP one
        values.append(item.Sender.GetExchangeUser().PrimarySmtpAddress)
    except Exception:
        pass
    return [str(value).lower() for value in values if value]


class MailExporter:
    def __init__(self, output_dir: Path, exported: dict[str, Any], *, dry_run: bool,
                 ignore_from: list[str] | None = None, ignore_subject: list[str] | None = None):
        self.output_dir = output_dir
        self.exported = exported  # shared with the caller, which persists it
        self.dry_run = dry_run
        self.ignore_from = ignore_from or []
        self.ignore_subject = ignore_subject or []
        self.saved_paths: set[Path] = set()
        self.skipped = 0
        self.ignored = 0
        self.failed = 0

    def is_ignored(self, item: Any) -> bool:
        """True when the sender or subject contains any ignore pattern (case-insensitive)."""
        subject = str(item.Subject or "").lower()
        if any(pattern in subject for pattern in self.ignore_subject):
            return True
        if self.ignore_from:
            senders = sender_addresses(item)
            return any(pattern in sender for pattern in self.ignore_from for sender in senders)
        return False

    def export_item(self, item: Any, *, force: bool = False, received: dt.datetime | None = None) -> bool:
        """Save one Outlook item as .msg; return True if saved. `force` re-exports known items."""
        try:
            if item.Class != OL_MAIL_ITEM:
                self.skipped += 1
                return False
            entry_id = item.EntryID
            if entry_id in self.exported and not force:
                self.skipped += 1
                return False
            if self.is_ignored(item):  # not recorded in state, so editing the lists later can export it
                self.ignored += 1
                return False

            received = received or item_received_time(item)
            stem = sanitize_filename(f"{received:%Y-%m-%d %H%M} {item.SenderName} - {item.Subject}")
            target = self.output_dir / f"{stem}.msg"
            copy_number = 2
            while target.exists() or target in self.saved_paths:  # saved_paths: dry runs write nothing
                target = self.output_dir / f"{stem} ({copy_number}).msg"
                copy_number += 1

            if self.dry_run:
                print(f"  would save: {target.name}")
            else:
                self.output_dir.mkdir(parents=True, exist_ok=True)
                item.SaveAs(str(target), OL_MSG_UNICODE)
                self.exported[entry_id] = {"file": target.name, "received": received.isoformat()}
            self.saved_paths.add(target)
            return True
        except Exception as error:  # IRM-protected mail, items not downloaded from an online archive, ...
            print(f"  ! could not save '{getattr(item, 'Subject', '?')}': {error}", file=sys.stderr)
            self.failed += 1
            return False

    def export_folder(self, folder: Any, since: dt.datetime, *, limit: int | None, unread_only: bool) -> None:
        """Export mail received on/after `since`, newest first, stopping after `limit` new saves."""
        items = folder.Items
        items.Sort("[ReceivedTime]", True)  # newest first, so we can stop at the cutoff
        saved_in_folder = 0
        for item in items:
            try:
                received = item_received_time(item)
            except Exception:
                continue  # items without a usable time (some drafts/reports)
            if received < since:
                break
            if unread_only and not getattr(item, "UnRead", False):
                continue
            if self.export_item(item, received=received):
                saved_in_folder += 1
                if saved_in_folder == limit:
                    break


# --------------------------------------------------------------------------- command line

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--selection", action="store_true",
                        help="export the emails currently selected in the Outlook window")
    action.add_argument("--days", type=float, metavar="N", help="export emails received in the last N days")
    action.add_argument("--since", type=dt.datetime.fromisoformat, metavar="YYYY-MM-DD",
                        help="export emails received on or after this date")
    action.add_argument("--list-folders", action="store_true", help="print the mailbox folder tree and exit")

    parser.add_argument("--folder", action="append", dest="folders", metavar="PATH",
                        help="with --days/--since: folder to export, e.g. 'Inbox/Projects' or "
                             "'Shared Mailbox/Inbox'; repeatable (default: Inbox)")
    parser.add_argument("--unread-only", action="store_true", help="with --days/--since: only unread emails")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="with --days/--since: stop after N newly saved emails per folder")
    parser.add_argument("--force", action="store_true",
                        help="with --selection: re-export emails even if they were exported before")

    parser.add_argument("--vault", type=Path, metavar="DIR",
                        help="vault root, used for the default --output-dir, --state-file and --convert "
                             "(default: $CLAUDE_PROJECT_DIR, the vault containing the current folder, "
                             "or the folder above tools/)")
    parser.add_argument("--output-dir", type=Path, metavar="DIR",
                        help=f"where to write .msg files (default: <vault>/{INBOX_DIRNAME})")
    parser.add_argument("--state-file", type=Path, metavar="FILE",
                        help=f"JSON file remembering exported emails "
                             f"(default: <vault>/{STATE_RELATIVE_PATH.as_posix()})")
    parser.add_argument("--ignore-from-file", type=Path, metavar="FILE",
                        help="text file of sender patterns to skip: one per line, case-insensitive partial match "
                             f"on name or address, '#' comments (default: {IGNORE_FROM_PATH})")
    parser.add_argument("--ignore-subject-file", type=Path, metavar="FILE",
                        help="text file of subject patterns to skip, same format "
                             f"(default: {IGNORE_SUBJECT_PATH})")
    parser.add_argument("--dry-run", action="store_true", help="show what would be saved without writing anything")
    parser.add_argument("--convert", action="store_true", help="run msg_to_md.py on the exported files afterwards")
    return parser


def parse_arguments(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace:
    args = parser.parse_args(argv)
    exports_folders = args.days is not None or args.since is not None
    if not exports_folders:
        for flag, used in (("--folder", args.folders), ("--unread-only", args.unread_only), ("--limit", args.limit)):
            if used:
                parser.error(f"{flag} only applies together with --days or --since")
    if args.force and not args.selection:
        parser.error("--force only applies together with --selection")
    if args.days is not None and args.days <= 0:
        parser.error("--days must be greater than 0")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


# --------------------------------------------------------------------------- commands

def convert_to_markdown(vault: Path, files: list[Path]) -> int:
    """Run msg_to_md.py on the exported files, in batches; return the last non-zero exit code."""
    command = [sys.executable, str(vault / CONVERTER_RELATIVE_PATH), "--vault", str(vault)]
    exit_code = 0
    for start in range(0, len(files), CONVERTER_BATCH_SIZE):
        batch = files[start:start + CONVERTER_BATCH_SIZE]
        exit_code = subprocess.call(command + [str(path) for path in batch]) or exit_code
    return exit_code


def run(args: argparse.Namespace) -> int:
    vault = find_vault(args.vault, default=VAULT_ROOT)
    output_dir = (args.output_dir or vault / INBOX_DIRNAME).resolve()
    state_path = (args.state_file or vault / STATE_RELATIVE_PATH).resolve()

    exported = load_state(state_path)  # before connecting: a corrupt state file fails fast
    application, namespace = connect_to_outlook()
    if args.list_folders:
        for mailbox in namespace.Folders:
            print_folder_tree(mailbox)
        return 0

    ignore_from_path = args.ignore_from_file or IGNORE_FROM_PATH
    ignore_subject_path = args.ignore_subject_file or IGNORE_SUBJECT_PATH
    for explicit, path in ((args.ignore_from_file, ignore_from_path), (args.ignore_subject_file, ignore_subject_path)):
        if explicit and not path.exists():
            raise ExportError(f"ignore file not found: {path}")
    exporter = MailExporter(output_dir, exported, dry_run=args.dry_run,
                            ignore_from=load_ignore_list(ignore_from_path),
                            ignore_subject=load_ignore_list(ignore_subject_path))
    try:
        if args.selection:
            selection = application.ActiveExplorer().Selection
            print(f"Selected items: {selection.Count}")
            for item in selection:
                exporter.export_item(item, force=args.force)
        else:
            since = args.since or dt.datetime.now() - dt.timedelta(days=args.days)
            for path in args.folders or ["Inbox"]:
                try:
                    folder = resolve_folder(namespace, path)
                except ExportError as error:
                    print(f"! {error}", file=sys.stderr)
                    exporter.failed += 1
                    continue
                print(f"{path}: since {since:%Y-%m-%d %H:%M}")
                exporter.export_folder(folder, since, limit=args.limit, unread_only=args.unread_only)
    finally:
        if not args.dry_run:  # also runs after Ctrl-C, so files already saved stay remembered
            save_state(state_path, exported)

    saved = len(exporter.saved_paths)
    print(f"Done: {'would save' if args.dry_run else 'saved'} {saved}, skipped {exporter.skipped} "
          f"(already exported or not an email), ignored {exporter.ignored}, "
          f"failed {exporter.failed} -> {output_dir}")

    convert_exit_code = 0
    if args.convert and saved and not args.dry_run:
        convert_exit_code = convert_to_markdown(vault, sorted(exporter.saved_paths))
    return 1 if exporter.failed else convert_exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        parser.print_help()
        return 0
    try:
        return run(parse_arguments(parser, argv))
    except ExportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    utf8_stdout()  # subjects are arbitrary Unicode; don't crash legacy consoles
    sys.exit(main())
