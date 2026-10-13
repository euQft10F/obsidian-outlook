#!/usr/bin/env python3
"""Export Outlook emails as .msg files into the vault's 00-inbox/ folder.

Python counterpart of Export-OutlookMsg.ps1, with incremental state so it can run
on a timer. Uses the classic Outlook desktop app through COM (Windows + pywin32);
the "new Outlook" has no COM API.

    uv run python tools/export_outlook_msg.py --selection          # emails selected in Outlook
    uv run python tools/export_outlook_msg.py --days 7             # Inbox, last 7 days
    uv run python tools/export_outlook_msg.py --days 30 --folder "Inbox/Projects/Q3" --folder "Sent Items"
    uv run python tools/export_outlook_msg.py --since 2026-09-01 --folder "Other Mailbox/Inbox"
    uv run python tools/export_outlook_msg.py --days 1 --convert   # export, then run msg_to_md.py
    uv run python tools/export_outlook_msg.py --list-folders       # show folder paths you can use

Already-exported emails are remembered by Outlook EntryID in raw/.outlook-export-state.json,
so repeated runs only save new mail. (The converter also de-duplicates by Message-ID.)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

OL_MSG_UNICODE = 9          # OlSaveAsType.olMSGUnicode
OL_MAIL_ITEM = 43           # OlObjectClass.olMail (skips meeting requests, NDRs, tasks)
DEFAULT_FOLDERS = {         # OlDefaultFolders
    "inbox": 6,
    "sent items": 5,
    "sent": 5,
    "deleted items": 3,
    "drafts": 16,
    "junk email": 23,
    "junk": 23,
    "outbox": 4,
}
STATE_FILE = "raw/.outlook-export-state.json"
BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]+')
MAX_NAME = 100              # keep full paths under Windows' 260-char limit


def utf8_stdout() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def safe_name(s: str) -> str:
    s = re.sub(r"\s+", " ", BAD_CHARS.sub(" ", s or "")).strip(" .")
    return s[:MAX_NAME].rstrip(" .") or "untitled"


def naive(t) -> dt.datetime:
    """pywin32 returns Outlook local times tagged as UTC; keep the wall-clock values."""
    return dt.datetime(t.year, t.month, t.day, t.hour, t.minute, t.second)


def connect():
    try:
        import win32com.client  # type: ignore
    except ImportError:
        sys.exit("pywin32 is not installed. On Windows run `uv sync` (it installs pywin32), "
                 "or `uv pip install pywin32`.")
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        return app, app.GetNamespace("MAPI")
    except Exception as e:
        sys.exit(f"Could not start classic Outlook via COM ({e}). The 'new Outlook' is not supported.")


def resolve_folder(ns, path: str):
    """'Inbox', 'Inbox/Sub/Sub', 'Sent Items', or '<mailbox or PST name>/Folder/Sub'."""
    parts = [p for p in re.split(r"[\\/]", path) if p.strip()]
    if not parts:
        raise ValueError("empty folder path")
    head = parts[0].strip().lower()
    if head in DEFAULT_FOLDERS:
        folder = ns.GetDefaultFolder(DEFAULT_FOLDERS[head])
    else:
        folder = None
        for store_root in ns.Folders:
            if store_root.Name.lower() == head:
                folder = store_root
                break
        if folder is None:
            stores = ", ".join(f.Name for f in ns.Folders)
            raise ValueError(f"'{parts[0]}' is neither a default folder nor a mailbox ({stores})")
    for p in parts[1:]:
        match = None
        for sub in folder.Folders:
            if sub.Name.lower() == p.strip().lower():
                match = sub
                break
        if match is None:
            raise ValueError(f"no subfolder '{p}' under '{folder.Name}'")
        folder = match
    return folder


def list_folders(ns, depth: int = 3) -> None:
    def walk(f, prefix: str, d: int):
        if d > depth:
            return
        try:
            count = f.Items.Count
        except Exception:
            count = "?"
        print(f"{prefix}{f.Name}  ({count} items)")
        for sub in f.Folders:
            walk(sub, prefix + "    ", d + 1)
    for root in ns.Folders:
        walk(root, "", 0)


class Exporter:
    def __init__(self, vault: Path, dry_run: bool):
        self.vault, self.dry = vault, dry_run
        self.out = vault / "00-inbox"
        self.state_path = vault / STATE_FILE
        self.state: dict = {"exported": {}}
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.state.setdefault("exported", {})
        self.saved = self.skipped = self.failed = 0

    def save(self, item, force: bool = False) -> None:
        try:
            if item.Class != OL_MAIL_ITEM:
                self.skipped += 1
                return
            entry_id = item.EntryID
            if entry_id in self.state["exported"] and not force:
                self.skipped += 1
                return
            when = naive(item.ReceivedTime if item.ReceivedTime else item.SentOn)
            name = safe_name(f"{when:%Y-%m-%d %H%M} {item.SenderName} - {item.Subject}")
            target = self.out / f"{name}.msg"
            n = 2
            while target.exists():
                target = self.out / f"{name} ({n}).msg"
                n += 1
            if self.dry:
                print(f"  would save: {target.name}")
            else:
                self.out.mkdir(parents=True, exist_ok=True)
                item.SaveAs(str(target), OL_MSG_UNICODE)
                self.state["exported"][entry_id] = {"file": target.name, "received": when.isoformat()}
            self.saved += 1
        except Exception as e:  # IRM-protected mail, items not downloaded from an online archive, …
            self.failed += 1
            subject = getattr(item, "Subject", "?")
            print(f"  ! could not save '{subject}': {e}", file=sys.stderr)

    def export_folder(self, folder, since: dt.datetime, limit: int | None, unread_only: bool) -> None:
        items = folder.Items
        items.Sort("[ReceivedTime]", True)  # newest first, so we can stop at the cutoff
        count = 0
        for item in items:
            try:
                received = naive(item.ReceivedTime)
            except Exception:
                continue  # items without ReceivedTime (some drafts/reports)
            if received < since:
                break
            if unread_only and not getattr(item, "UnRead", False):
                continue
            self.save(item)
            count += 1
            if limit and count >= limit:
                break

    def write_state(self) -> None:
        if self.dry:
            return
        self.state["last_run"] = dt.datetime.now().isoformat(timespec="seconds")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=1, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--selection", action="store_true", help="export emails selected in the Outlook window")
    mode.add_argument("--days", type=float, help="export emails received in the last N days (default 1)")
    mode.add_argument("--since", help="export emails received on/after YYYY-MM-DD")
    mode.add_argument("--list-folders", action="store_true", help="print mailbox folder tree and exit")
    ap.add_argument("--folder", action="append", help="folder path, repeatable (default: Inbox)")
    ap.add_argument("--unread-only", action="store_true")
    ap.add_argument("--limit", type=int, help="max emails per folder")
    ap.add_argument("--force", action="store_true", help="re-export selected emails even if exported before")
    ap.add_argument("--vault", default=str(Path(__file__).resolve().parent.parent), help="vault root")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--convert", action="store_true", help="run msg_to_md.py on the inbox afterwards")
    a = ap.parse_args()

    vault = Path(a.vault).resolve()
    app, ns = connect()
    if a.list_folders:
        list_folders(ns)
        return 0

    ex = Exporter(vault, a.dry_run)
    if a.selection:
        selection = app.ActiveExplorer().Selection
        print(f"Selected items: {selection.Count}")
        for item in selection:
            ex.save(item, force=a.force)
    else:
        since = (dt.datetime.fromisoformat(a.since) if a.since
                 else dt.datetime.now() - dt.timedelta(days=a.days if a.days is not None else 1))
        for path in a.folder or ["Inbox"]:
            try:
                folder = resolve_folder(ns, path)
            except Exception as e:
                print(f"! {e}", file=sys.stderr)
                ex.failed += 1
                continue
            print(f"{path}: since {since:%Y-%m-%d %H:%M}")
            ex.export_folder(folder, since, a.limit, a.unread_only)
    ex.write_state()
    verb = "would save" if a.dry_run else "saved"
    print(f"Done: {verb} {ex.saved}, skipped {ex.skipped} (already exported or not an email), failed {ex.failed}"
          f" -> {ex.out}")

    if a.convert and not a.dry_run and ex.saved:
        conv = vault / ".claude" / "skills" / "email-ingest" / "scripts" / "msg_to_md.py"
        return subprocess.call([sys.executable, str(conv), "--vault", str(vault)])
    return 1 if ex.failed and not ex.saved else 0


if __name__ == "__main__":
    sys.exit(main())
