#!/usr/bin/env python3
"""Turn a reply draft from wiki/drafts/ into an unsent Outlook reply saved as .msg next to it.

Uses the classic Outlook desktop app through COM (Windows + pywin32). The reply is built from the
archived original (raw/msg/...), so it threads under it and carries Outlook's quoted history and
your default signature. The .msg is only written to disk: nothing is sent and nothing is saved in
the mailbox. Double-click the file to open it in Outlook as an editable reply, check it, then send.

    uv run --frozen python .claude/skills/email-draft/scripts/draft_to_msg.py "wiki/drafts/Draft - X (2026-10).md"
    uv run --frozen python .claude/skills/email-draft/scripts/draft_to_msg.py "wiki/drafts/Draft - X.md" --json
"""
from __future__ import annotations

import argparse
import contextlib
import html
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "email-ingest" / "scripts"))  # shared helpers
from vaultlib import (  # noqa: E402
    EMAIL_ADDRESS_RE,
    FRONTMATTER_RE,
    find_vault,
    parse_frontmatter,
    utf8_stdout,
    wikilinks,
)

OL_MAIL_ITEM = 0             # OlItemType.olMailItem (for CreateItem)
OL_MSG_UNICODE = 9           # OlSaveAsType.olMSGUnicode
OL_DISCARD = 1               # OlInspectorClose.olDiscard
OL_FOLDER_INBOX = 6          # OlDefaultFolders
OL_FOLDER_SENT_ITEMS = 5
PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
NOTES_HEADING_RE = re.compile(r"^## Notes\s*$", re.M)
BODY_TAG_RE = re.compile(r"<body\b[^>]*>", re.I)
DATED_NAME_RE = re.compile(r"^(\d{4})-(\d{2})-\d{2} ")


class DraftError(Exception):
    """A problem the user can act on (bad draft path, missing source, Outlook unavailable, ...)."""


@dataclass
class Draft:
    path: Path
    to: list[str]
    cc: list[str]
    subject: str
    body: str
    source_meta: dict[str, Any]  # frontmatter of the latest raw email note among the draft's sources


@dataclass
class Result:
    msg: Path
    anchor: str  # how the original was found: archived-msg | mailbox | none
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"msg": str(self.msg), "uri": self.msg.as_uri(), "anchor": self.anchor, "warnings": self.warnings}


# --------------------------------------------------------------------------- reading the draft

def as_list(value: Any) -> list[str]:
    """Frontmatter value (block list, or one scalar) -> list of non-empty strings."""
    items = value if isinstance(value, list) else [value] if isinstance(value, str) else []
    return [str(item).strip() for item in items if str(item).strip()]


def inside(path: Path, folder: Path) -> bool:
    return path.resolve().is_relative_to(folder.resolve())


def resolve_draft_path(vault: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    path = (path if path.is_absolute() else vault / path).resolve()
    if path.suffix != ".md" or not inside(path, vault / "wiki" / "drafts"):
        raise DraftError(f"not a draft under wiki/drafts/: {raw_path}")
    if not path.is_file():
        raise DraftError(f"draft not found: {raw_path}")
    return path


def find_email_note(vault: Path, link: str) -> Path | None:
    """The raw email note a `[[basename]]` (or `[[raw/emails/.../basename]]`) link points to."""
    emails_dir = vault / "raw" / "emails"
    name = Path(link).name
    if not name:
        return None
    dated = DATED_NAME_RE.match(name)  # notes live in raw/emails/YYYY/MM/; avoid walking the whole tree
    if dated and (candidate := emails_dir / dated[1] / dated[2] / f"{name}.md").is_file():
        return candidate
    return next(emails_dir.rglob(f"{name}.md"), None)


def read_draft(vault: Path, path: Path) -> Draft:
    text = path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    if meta.get("type") != "draft":
        raise DraftError(f"{path.name} has no `type: draft` frontmatter")

    match = FRONTMATTER_RE.match(text)
    body = text[match.end():] if match else text
    notes = NOTES_HEADING_RE.search(body)
    body = (body[:notes.start()] if notes else body).strip()
    if not body:
        raise DraftError(f"{path.name} has no reply text above `## Notes`")

    sources = [note for link in wikilinks("\n".join(as_list(meta.get("sources"))))
               if (note := find_email_note(vault, link))]
    if not sources:
        raise DraftError(f"{path.name}: none of its `sources` is a raw email note")
    latest = max(sources, key=lambda note: note.name)  # basenames start with "YYYY-MM-DD HHMM"

    to = as_list(meta.get("to"))
    if not to:
        raise DraftError(f"{path.name} has no `to` recipients")
    return Draft(path=path, to=to, cc=as_list(meta.get("cc")), subject=str(meta.get("subject") or ""),
                 body=body, source_meta=parse_frontmatter(latest.read_text(encoding="utf-8")))


def body_html(text: str) -> str:
    """Plain draft text -> escaped HTML paragraphs. Never passes markup through: the text is email-derived."""
    paragraphs = [part.strip("\n") for part in re.split(r"\n\s*\n", text) if part.strip()]
    rendered = ("<p>" + "<br>\n".join(html.escape(line) for line in part.splitlines()) + "</p>"
                for part in paragraphs)
    return '<div class="email-vault-draft">\n' + "\n".join(rendered) + "\n</div>\n"


def unknown_recipients(draft: Draft, source_meta: dict[str, Any]) -> list[str]:
    """Draft recipients whose address is not among the source email's participants."""
    known = {address.lower() for address in as_list(source_meta.get("participants"))}
    known |= {str(source_meta.get("from_email", "")).lower()}
    flagged = []
    for recipient in [*draft.to, *draft.cc]:
        addresses = EMAIL_ADDRESS_RE.findall(recipient)
        if not addresses or any(address.lower() not in known for address in addresses):
            flagged.append(recipient)
    return flagged


# --------------------------------------------------------------------------- Outlook

def connect_to_outlook() -> tuple[Any, Any]:
    """Return the Outlook Application object and its MAPI namespace."""
    try:
        import win32com.client  # type: ignore[import-not-found]
        application = win32com.client.Dispatch("Outlook.Application")
        return application, application.GetNamespace("MAPI")
    except ImportError:
        raise DraftError("pywin32 is not installed (Windows only); run `uv sync`") from None
    except Exception as error:  # pywintypes.com_error; not importable without pywin32
        raise DraftError(f"Could not start classic Outlook via COM ({error}). "
                         "The 'new Outlook' is not supported.") from error


def find_in_mailbox(namespace: Any, message_id: str) -> Any | None:
    """The Inbox or Sent Items item with this Internet Message-ID, if any."""
    if not message_id:
        return None
    query = f"@SQL=\"{PR_INTERNET_MESSAGE_ID}\" = '{message_id.replace(chr(39), chr(39) * 2)}'"
    for folder_id in (OL_FOLDER_INBOX, OL_FOLDER_SENT_ITEMS):
        try:
            for item in namespace.GetDefaultFolder(folder_id).Items.Restrict(query):
                return item
        except Exception:  # a folder that refuses the query; try the next
            continue
    return None


def open_original(vault: Path, namespace: Any, source_meta: dict[str, Any]) -> tuple[Any | None, str]:
    """The email being replied to as an Outlook item, and how it was found."""
    source_file = str(source_meta.get("source_file") or "")
    archived = (vault / source_file).resolve() if source_file else None
    if archived and archived.suffix.lower() == ".msg" and archived.is_file() and inside(archived, vault / "raw"):
        with contextlib.suppress(Exception):  # e.g. IRM-protected; try the mailbox
            return namespace.OpenSharedItem(str(archived)), "archived-msg"
    item = find_in_mailbox(namespace, str(source_meta.get("message_id") or ""))
    return (item, "mailbox") if item is not None else (None, "none")


def build_reply_msg(vault: Path, draft: Draft, application: Any, namespace: Any) -> Result:
    """Save an unsent reply for `draft` as `<draft stem>.msg`. Never sends, never saves to the mailbox."""
    original, anchor = open_original(vault, namespace, draft.source_meta)
    result = Result(msg=draft.path.with_suffix(".msg"), anchor=anchor)
    flagged = unknown_recipients(draft, draft.source_meta)
    if flagged:
        result.warnings.append("recipients not on the original email: " + "; ".join(flagged))

    if original is not None:
        reply = original.Reply()
    else:
        reply = application.CreateItem(OL_MAIL_ITEM)
        result.warnings.append("original not found (no archived .msg, not in Inbox/Sent Items): "
                               "new message without quoted history or threading")
    try:
        with contextlib.suppress(Exception):
            reply.GetInspector  # noqa: B018 - touching the inspector makes Outlook add the default signature
        reply.To = "; ".join(draft.to)
        reply.CC = "; ".join(draft.cc)
        if draft.subject:
            reply.Subject = draft.subject
        existing = reply.HTMLBody or ""
        body_tag = BODY_TAG_RE.search(existing)
        insert_at = body_tag.end() if body_tag else 0
        reply.HTMLBody = existing[:insert_at] + body_html(draft.body) + existing[insert_at:]

        result.msg.unlink(missing_ok=True)  # one .msg per draft, replaced on every run
        reply.SaveAs(str(result.msg), OL_MSG_UNICODE)
    finally:
        for item in (reply, original):
            if item is not None:
                with contextlib.suppress(Exception):
                    item.Close(OL_DISCARD)
    return result


# --------------------------------------------------------------------------- command line

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    parser.add_argument("draft", help="the draft note, e.g. 'wiki/drafts/Draft - X (2026-10).md'")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    vault = find_vault()
    try:
        draft = read_draft(vault, resolve_draft_path(vault, args.draft))
        application, namespace = connect_to_outlook()
        result = build_reply_msg(vault, draft, application, namespace)
    except DraftError as error:
        print(json.dumps({"error": str(error)}) if args.json else f"error: {error}",
              file=sys.stdout if args.json else sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        print(f"Saved unsent reply: {result.msg}\n  {result.msg.as_uri()}\n  original found via: {result.anchor}")
        for warning in result.warnings:
            print(f"  ! {warning}")
    return 0


if __name__ == "__main__":
    utf8_stdout()
    sys.exit(main())
