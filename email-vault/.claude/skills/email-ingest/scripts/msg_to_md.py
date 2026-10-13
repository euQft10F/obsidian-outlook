#!/usr/bin/env python3
"""Convert Outlook .msg (and .eml) files from 00-inbox/ into immutable markdown notes in raw/.

Deterministic, no LLM. Safe to run repeatedly: duplicates (same Message-ID or same bytes)
are detected and skipped.

    python msg_to_md.py                 # convert everything in <vault>/00-inbox
    python msg_to_md.py --dry-run       # show what would happen
    python msg_to_md.py --keep          # leave originals in the inbox (default: archive to raw/msg/)
    python msg_to_md.py --me me@work.com --me me@home.com   # mark emails you sent

Output per email:
    raw/emails/YYYY/MM/<YYYY-MM-DD HHMM> <Sender> - <Subject>.md
    raw/attachments/YYYY/MM/<same stem>/<files>   (+ <file>.extracted.md text for pdf/docx/xlsx/txt/csv)
    raw/msg/YYYY/MM/<same stem>.msg               (archived original)

Requires: extract-msg (for .msg). Optional: markdownify (HTML-only emails), pypdf,
python-docx, openpyxl (attachment text extraction).
"""
from __future__ import annotations

import argparse
import datetime as dt
import email
import email.policy
import email.utils
import hashlib
import html
import json
import os
import re
import shutil
import sys
import traceback
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vaultlib import find_vault, utf8_stdout  # noqa: E402

INDEX_FILE = "raw/.email-index.json"
LOG_FILE = "raw/.ingest-log.jsonl"
TEXT_EXTRACT_LIMIT = 200_000  # chars kept from an attachment's extracted text
SMALL_INLINE_IMAGE = 20_000  # bytes; smaller inline images are usually signature logos
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".emz", ".wmz", ".tif", ".tiff", ".webp"}

SUBJECT_PREFIX_RE = re.compile(
    r"^\s*((re|fw|fwd|aw|wg|tr|sv|vs|rv|r|antw|odp|réf|ref)\s*(\[\d+\])?\s*:\s*|\[external\]\s*)+",
    re.I,
)
# Where quoted history starts (Outlook, Gmail, Apple Mail; EN/FR/DE/NL/IT/ES headers).
QUOTE_START_PATTERNS = [
    re.compile(r"^-{2,}\s*(original message|ursprüngliche nachricht|message d'origine|oorspronkelijk bericht|messaggio originale|mensaje original)\s*-{2,}\s*$", re.I),
    re.compile(r"^_{10,}\s*$"),
    re.compile(r"^(on|le|am|op|il|el)\s.{4,250}(wrote|a écrit|schrieb|schreef|ha scritto|escribió)\s*:?\s*$", re.I),
]
HEADER_FROM_RE = re.compile(r"^\**\s*(from|de|von|van|da|od)\s*:\**\s*\S", re.I)
HEADER_SENT_RE = re.compile(r"^\**\s*(sent|date|envoyé|gesendet|verzonden|inviato|enviado|datum)\s*:\**\s*\S", re.I)
BAD_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]+')


# ---------------------------------------------------------------- helpers

def clean(s) -> str:
    if s is None:
        return ""
    if isinstance(s, bytes):
        s = s.decode("utf-8", errors="replace")
    s = str(s).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    return s.strip()


def safe_get(obj, attr, default=None):
    try:
        v = getattr(obj, attr)
        return default if v is None else v
    except Exception:
        return default


def sanitize_filename(s: str, max_len: int = 110) -> str:
    s = BAD_FILENAME_CHARS.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:max_len].rstrip(" .") or "untitled")


def normalize_subject(subject: str) -> str:
    s = SUBJECT_PREFIX_RE.sub("", subject or "").strip()
    return s or "(no subject)"


def slug(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w]+", "-", s.lower(), flags=re.U).strip("-")
    return s[:max_len].strip("-") or "untitled"


BARE_ADDR_RE = re.compile(r"[\w.+'&-]+@[\w-]+(?:\.[\w-]+)+")


def parse_addresses(raw: str) -> list[dict]:
    """Parse Outlook-style recipient strings.

    Handles 'Last, First <a@b>; Other <c@d>', unquoted names containing commas or '@',
    and bare address lists. (email.utils.getaddresses mangles the Outlook forms.)
    """
    raw = clean(raw)
    pairs: list[tuple[str, str]] = []
    for seg in re.split(r"(?<=>)\s*[,;]\s*|\s*;\s*", raw):
        seg = seg.strip(" ,")
        if not seg:
            continue
        m = re.search(r"<([^<>]+)>\s*$", seg)
        if m:
            name = seg[:m.start()].strip()
            # peel bare addresses that precede the name: "a@b.com, Smith, John <j@x.com>"
            while True:
                lead = re.match(r"^\s*(" + BARE_ADDR_RE.pattern + r")\s*,\s*", name)
                if not lead:
                    break
                pairs.append(("", lead.group(1)))
                name = name[lead.end():]
            pairs.append((name, m.group(1)))
        else:
            found = BARE_ADDR_RE.findall(seg)
            if found:
                pairs.extend(("", a) for a in found)
            else:
                pairs.append((seg, ""))  # Exchange display name with no address
    out, seen = [], set()
    for name, addr in pairs:
        name = clean(name).strip("'\" ,")
        addr = clean(addr).strip().lower()
        if addr.startswith("mailto:"):
            addr = addr[7:]
        if addr and "@" not in addr:  # Exchange X.500 path (/o=exchangelabs/...): keep the name only
            addr = ""
        if "@" in name and not addr:
            name, addr = "", name.lower()
        if name.lower() == addr:
            name = ""
        key = addr or name.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"name": name or (addr.split("@")[0] if addr else ""), "email": addr})
    return out


def fmt_addr(a: dict) -> str:
    if a["email"] and a["name"] and a["name"].lower() != a["email"]:
        return f"{a['name']} <{a['email']}>"
    return a["email"] or a["name"]


def yaml_str(s) -> str:
    return json.dumps("" if s is None else str(s), ensure_ascii=False)


def yaml_list(items) -> str:
    items = [i for i in items if i]
    if not items:
        return " []"
    return "\n" + "\n".join(f"  - {yaml_str(i)}" for i in items)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "table"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_markdown(h: str) -> str:
    try:
        from markdownify import markdownify  # type: ignore
        md = markdownify(h, heading_style="ATX", strip=["img", "style", "script"])
    except Exception:
        p = _TextExtractor()
        p.feed(h)
        md = html.unescape("".join(p.parts))
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def split_quoted(body: str) -> tuple[str, str]:
    """Split body into (new content, quoted history)."""
    lines = body.split("\n")
    n = len(lines)
    for i, line in enumerate(lines):
        s = line.strip()
        if i == 0 or not s:
            continue
        if any(p.match(s) for p in QUOTE_START_PATTERNS):
            return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:]).strip()
        if HEADER_FROM_RE.match(s) and any(HEADER_SENT_RE.match(l.strip()) for l in lines[i + 1:i + 6]):
            return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:]).strip()
        if s.startswith(">"):
            # a run of 3+ quoted lines that continues to the end-ish = reply quoting
            rest = [l for l in lines[i:] if l.strip()]
            quoted = sum(1 for l in rest if l.lstrip().startswith(">"))
            if quoted >= 3 and quoted >= 0.6 * len(rest):
                return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:]).strip()
    return body.strip(), ""


def extract_attachment_text(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if ext in (".txt", ".csv", ".md", ".json", ".log"):
            return path.read_text(encoding="utf-8", errors="replace")
        if ext == ".pdf":
            from pypdf import PdfReader  # type: ignore
            return "\n\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
        if ext == ".docx":
            import docx  # type: ignore
            d = docx.Document(str(path))
            parts = [p.text for p in d.paragraphs]
            for t in d.tables:
                for row in t.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
            return "\n".join(parts)
        if ext in (".xlsx", ".xlsm"):
            import openpyxl  # type: ignore
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
            out = []
            for ws in wb.worksheets:
                out.append(f"## Sheet: {ws.title}")
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= 500:
                        out.append("… (truncated)")
                        break
                    if any(c is not None for c in row):
                        out.append(" | ".join("" if c is None else str(c) for c in row))
            return "\n".join(out)
    except ImportError:
        return ""
    except Exception as e:  # unreadable / encrypted attachment
        return f"(text extraction failed: {e})"
    return ""


# ---------------------------------------------------------------- parsing

class Parsed:
    """Normalised view of one email, independent of source format."""

    def __init__(self):
        self.subject = ""
        self.sender: list[dict] = []
        self.to: list[dict] = []
        self.cc: list[dict] = []
        self.date: dt.datetime | None = None
        self.date_source = "header"
        self.message_id = ""
        self.in_reply_to = ""
        self.references: list[str] = []
        self.conversation_root = ""
        self.body = ""
        self.attachments: list[tuple[str, bytes, bool]] = []  # (filename, data, inline)
        self.embedded: list["Parsed"] = []
        self.importance = ""
        self.categories: list[str] = []


def _msgid_list(s: str) -> list[str]:
    return re.findall(r"<[^<>\s]+>", clean(s))


def parse_msg_object(m) -> Parsed:
    p = Parsed()
    p.subject = clean(safe_get(m, "subject", ""))
    p.sender = parse_addresses(safe_get(m, "sender", ""))
    p.to = parse_addresses(safe_get(m, "to", ""))
    p.cc = parse_addresses(safe_get(m, "cc", ""))
    p.date = safe_get(m, "date")
    if not isinstance(p.date, dt.datetime):
        p.date = None
    p.message_id = (_msgid_list(safe_get(m, "messageId", "")) or [""])[0]
    p.in_reply_to = (_msgid_list(safe_get(m, "inReplyTo", "")) or [""])[0]
    hdr = safe_get(m, "header")
    if hdr is not None:
        try:
            p.references = _msgid_list(hdr.get("References", "") or "")
            if not p.in_reply_to:
                p.in_reply_to = (_msgid_list(hdr.get("In-Reply-To", "") or "") or [""])[0]
            if not p.date and hdr.get("Date"):
                p.date = email.utils.parsedate_to_datetime(hdr.get("Date"))
        except Exception:
            pass
    ci = safe_get(m, "conversationIndex")
    if isinstance(ci, (bytes, bytearray)) and len(ci) >= 22:
        p.conversation_root = ci[:22].hex()
    imp = safe_get(m, "importance")
    if imp is not None and "HIGH" in str(imp).upper():
        p.importance = "high"
    cats = safe_get(m, "categories")
    if cats:
        p.categories = [clean(c) for c in (cats if isinstance(cats, (list, tuple)) else [cats]) if clean(c)]

    body = clean(safe_get(m, "body", ""))
    if len(body) < 20:
        h = safe_get(m, "htmlBody")
        if h:
            body = html_to_markdown(clean(h)) or body
    p.body = body

    for i, att in enumerate(safe_get(m, "attachments", []) or []):
        try:
            data = att.data
            if not isinstance(data, (bytes, bytearray, str)) and hasattr(data, "attachments") and hasattr(data, "body"):
                p.embedded.append(parse_msg_object(data))
                continue
            if not isinstance(data, (bytes, bytearray)) or not data:
                continue
            name = clean(safe_get(att, "longFilename") or safe_get(att, "shortFilename") or f"attachment-{i + 1}")
            inline = bool(safe_get(att, "hidden", False)) or bool(clean(safe_get(att, "contentId", "")))
            p.attachments.append((name, bytes(data), inline))
        except Exception:
            continue
    return p


def parse_msg_file(path: Path) -> Parsed:
    import extract_msg  # type: ignore

    m = extract_msg.openMsg(str(path))
    try:
        return parse_msg_object(m)
    finally:
        try:
            m.close()
        except Exception:
            pass


def parse_eml_file(path: Path) -> Parsed:
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    p = Parsed()
    p.subject = clean(msg.get("Subject", ""))
    p.sender = parse_addresses(str(msg.get("From", "")))
    p.to = parse_addresses(str(msg.get("To", "")))
    p.cc = parse_addresses(str(msg.get("Cc", "")))
    try:
        p.date = email.utils.parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else None
    except Exception:
        p.date = None
    p.message_id = (_msgid_list(str(msg.get("Message-ID", ""))) or [""])[0]
    p.in_reply_to = (_msgid_list(str(msg.get("In-Reply-To", ""))) or [""])[0]
    p.references = _msgid_list(str(msg.get("References", "")))
    if "high" in str(msg.get("Importance", "")).lower() or str(msg.get("X-Priority", "")).startswith("1"):
        p.importance = "high"
    plain = msg.get_body(preferencelist=("plain",))
    body = clean(plain.get_content()) if plain else ""
    if len(body) < 20:
        h = msg.get_body(preferencelist=("html",))
        if h:
            body = html_to_markdown(h.get_content()) or body
    p.body = body
    for i, part in enumerate(msg.iter_attachments()):
        try:
            if part.get_content_type() == "message/rfc822":
                inner = part.get_payload(0) if part.is_multipart() else None
                if inner is not None:
                    tmp = path.with_suffix(f".embedded{i}.eml.tmp")
                    tmp.write_bytes(inner.as_bytes())
                    try:
                        p.embedded.append(parse_eml_file(tmp))
                    finally:
                        tmp.unlink(missing_ok=True)
                continue
            data = part.get_payload(decode=True)
            if not data:
                continue
            name = clean(part.get_filename() or f"attachment-{i + 1}")
            inline = (part.get_content_disposition() == "inline") or bool(part.get("Content-ID"))
            p.attachments.append((name, data, inline))
        except Exception:
            continue
    return p


# ---------------------------------------------------------------- writing

class Converter:
    def __init__(self, vault: Path, me: list[str], dry_run: bool, keep: bool):
        self.vault, self.dry, self.keep = vault, dry_run, keep
        self.me = {e.lower() for e in me}
        self.index_path = vault / INDEX_FILE
        self.index = {"by_msgid": {}, "by_sha": {}}
        if self.index_path.exists():
            self.index.update(json.loads(self.index_path.read_text(encoding="utf-8")))
        self.results: list[dict] = []

    # -- public
    def convert_file(self, src: Path) -> None:
        raw = src.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if sha in self.index["by_sha"]:
            self._record(src, "duplicate", note=self.index["by_sha"][sha])
            self._archive(src, None, duplicate=True)
            return
        try:
            parsed = parse_msg_file(src) if src.suffix.lower() == ".msg" else parse_eml_file(src)
        except Exception as e:
            self._record(src, "failed", error=f"{type(e).__name__}: {e}")
            if not self.dry:
                dest = self.vault / "00-inbox" / "_failed"
                dest.mkdir(parents=True, exist_ok=True)
                if src.parent != dest:
                    shutil.move(str(src), str(dest / src.name))
                (dest / (src.name + ".error.txt")).write_text(traceback.format_exc(), encoding="utf-8")
            return
        if not parsed.date:
            parsed.date = dt.datetime.fromtimestamp(src.stat().st_mtime).astimezone()
            parsed.date_source = "file-modified-time"
        if parsed.message_id and parsed.message_id in self.index["by_msgid"]:
            self._record(src, "duplicate", note=self.index["by_msgid"][parsed.message_id])
            self._archive(src, None, duplicate=True)
            return
        note = self._write(parsed, sha, source_ext=src.suffix.lower())
        stem_path = note.with_suffix("")
        archived = self._archive(src, stem_path)
        if archived and not self.dry:
            self._set_source(note, archived)
        self._record(src, "converted", note=self._rel(note))

    def save_index(self) -> None:
        if self.dry:
            return
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps(self.index, indent=1, ensure_ascii=False), encoding="utf-8")
        with (self.vault / LOG_FILE).open("a", encoding="utf-8") as f:
            stamp = dt.datetime.now().isoformat(timespec="seconds")
            for r in self.results:
                f.write(json.dumps({"at": stamp, **r}, ensure_ascii=False) + "\n")

    # -- internals
    def _rel(self, p: Path) -> str:
        return p.relative_to(self.vault).as_posix()

    def _record(self, src: Path, status: str, **kw) -> None:
        self.results.append({"file": src.name, "status": status, **kw})

    def _thread_id(self, p: Parsed) -> str:
        if p.conversation_root:
            return "conv-" + p.conversation_root[:24]
        # References[0] is the thread root; a root email's own Message-ID matches its replies' References[0].
        root = p.references[0] if p.references else (p.in_reply_to or p.message_id)
        if root:
            return "ref-" + hashlib.sha1(root.encode()).hexdigest()[:16]
        return "subj-" + slug(normalize_subject(p.subject), 48)

    def _unique(self, path: Path) -> Path:
        if not path.exists():
            return path
        for i in range(2, 1000):
            cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
            if not cand.exists():
                return cand
        raise RuntimeError(f"cannot find free name for {path}")

    def _write(self, p: Parsed, sha: str, source_ext: str, parent: str = "") -> Path:
        local = p.date.astimezone() if p.date.tzinfo else p.date
        ym = Path(f"{local:%Y}") / f"{local:%m}"
        sender = p.sender[0] if p.sender else {"name": "Unknown sender", "email": ""}
        stem = sanitize_filename(f"{local:%Y-%m-%d %H%M} {sender['name']} - {normalize_subject(p.subject)}")
        note = self._unique(self.vault / "raw" / "emails" / ym / f"{stem}.md")
        stem = note.stem

        # attachments
        att_lines = []
        att_dir = self.vault / "raw" / "attachments" / ym / stem
        kept = [(n, d, inl) for n, d, inl in p.attachments
                if not (inl and Path(n).suffix.lower() in IMAGE_EXT and len(d) < SMALL_INLINE_IMAGE)]
        for name, data, _inline in kept:
            fname = sanitize_filename(name, 120)
            target = self._unique(att_dir / fname)
            line = f"- [[{self._rel(target)}|{fname}]] ({len(data) / 1024:.0f} KB)"
            if not self.dry:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                text = clean(extract_attachment_text(target))
                if text:
                    ext_note = target.with_name(target.name + ".extracted.md")
                    clipped = text[:TEXT_EXTRACT_LIMIT] + ("\n\n… (truncated)" if len(text) > TEXT_EXTRACT_LIMIT else "")
                    ext_note.write_text(
                        "---\ntype: attachment-text\n"
                        f"attachment: {yaml_str('[[' + self._rel(target) + ']]')}\n"
                        f"email: {yaml_str('[[' + self._rel(note.with_suffix('')) + ']]')}\n"
                        "---\n\n" + clipped + "\n", encoding="utf-8")
                    line += f" · text: [[{self._rel(ext_note.with_suffix(''))}|extracted]]"
            att_lines.append(line)

        # embedded emails (forwarded as attachments) become their own notes
        for emb in p.embedded:
            if not emb.date:
                emb.date, emb.date_source = p.date, "parent-email"
            child = self._write(emb, sha, source_ext, parent=self._rel(note.with_suffix("")))
            att_lines.append(f"- Embedded email: [[{self._rel(child.with_suffix(''))}]]")

        new_body, quoted = split_quoted(p.body)
        participants = sorted({a["email"] for a in p.sender + p.to + p.cc if a["email"]})
        direction = "sent" if sender["email"] in self.me else "received"

        fm = [
            "---",
            "type: email",
            f"subject: {yaml_str(p.subject or '(no subject)')}",
            f"thread_subject: {yaml_str(normalize_subject(p.subject))}",
            f"thread_id: {yaml_str(self._thread_id(p))}",
            f"date: {local:%Y-%m-%dT%H:%M:%S}",
            f"date_source: {p.date_source}",
            f"from: {yaml_str(fmt_addr(sender))}",
            f"from_email: {yaml_str(sender['email'])}",
            f"to:{yaml_list(fmt_addr(a) for a in p.to)}",
            f"cc:{yaml_list(fmt_addr(a) for a in p.cc)}",
            f"participants:{yaml_list(participants)}",
            f"direction: {direction}",
            f"has_attachments: {'true' if att_lines else 'false'}",
        ]
        if p.importance:
            fm.append(f"importance: {p.importance}")
        if p.categories:
            fm.append(f"outlook_categories:{yaml_list(p.categories)}")
        if parent:
            fm.append(f"embedded_in: {yaml_str('[[' + parent + ']]')}")
        fm += [
            f"message_id: {yaml_str(p.message_id)}",
            f"in_reply_to: {yaml_str(p.in_reply_to)}",
            f"source_format: {source_ext.lstrip('.')}",
            "source_file: \"\"",
            f"sha256: {sha[:16]}",
            "wiki_status: pending",
            "tags:\n  - email",
            "---",
        ]
        lines = fm + [
            "",
            f"# {p.subject or '(no subject)'}",
            "",
            f"**From:** {fmt_addr(sender)}  ",
            f"**To:** {', '.join(fmt_addr(a) for a in p.to) or '—'}  ",
        ]
        if p.cc:
            lines.append(f"**Cc:** {', '.join(fmt_addr(a) for a in p.cc)}  ")
        lines += [f"**Date:** {local:%a %d %b %Y %H:%M}", "", "## Message", "", new_body or "_(empty body)_", ""]
        if att_lines:
            lines += ["## Attachments", "", *att_lines, ""]
        if quoted:
            lines += ["## Quoted history", "", "> [!quote]- Earlier messages quoted in this email (folded)"]
            lines += [("> " + l) if l.strip() else ">" for l in quoted.split("\n")]
            lines.append("")

        if not self.dry:
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text("\n".join(lines), encoding="utf-8")
            if p.message_id:
                self.index["by_msgid"][p.message_id] = self._rel(note)
            if not parent:
                self.index["by_sha"][sha] = self._rel(note)
        return note

    def _archive(self, src: Path, stem_path: Path | None, duplicate: bool = False) -> Path | None:
        if self.keep or self.dry:
            return None
        if duplicate:
            dest = self.vault / "raw" / "msg" / "_duplicates" / src.name
        else:
            rel = stem_path.relative_to(self.vault / "raw" / "emails")
            dest = self.vault / "raw" / "msg" / rel.parent / (rel.name + src.suffix.lower())
        dest = self._unique(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return dest

    def _set_source(self, note: Path, archived: Path) -> None:
        text = note.read_text(encoding="utf-8")
        note.write_text(text.replace('source_file: ""', f"source_file: {yaml_str(self._rel(archived))}", 1),
                        encoding="utf-8")


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="specific .msg/.eml files (default: everything in 00-inbox/)")
    ap.add_argument("--vault", help="vault root (default: $CLAUDE_PROJECT_DIR or nearest folder with CLAUDE.md)")
    ap.add_argument("--me", action="append", default=[], help="your own address(es); also env EMAIL_VAULT_ME")
    ap.add_argument("--keep", action="store_true", help="leave originals in the inbox instead of archiving")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", help="print results as JSON")
    a = ap.parse_args()

    vault = find_vault(a.vault)
    me = a.me + [x.strip() for x in os.environ.get("EMAIL_VAULT_ME", "").split(",") if x.strip()]
    inbox = vault / "00-inbox"
    if a.files:
        files = [Path(f).resolve() for f in a.files]
    else:
        files = sorted(f for f in inbox.rglob("*")
                       if f.suffix.lower() in (".msg", ".eml") and "_failed" not in f.parts)
    conv = Converter(vault, me, a.dry_run, a.keep)
    for f in files:
        conv.convert_file(f)
    conv.save_index()

    counts: dict[str, int] = {}
    for r in conv.results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if a.json:
        print(json.dumps({"vault": str(vault), "counts": counts, "results": conv.results}, ensure_ascii=False, indent=1))
    else:
        print(f"Vault: {vault}{'  (dry run)' if a.dry_run else ''}")
        print("Summary: " + (", ".join(f"{k} {v}" for k, v in counts.items()) or "inbox empty"))
        for r in conv.results:
            extra = r.get("note") or r.get("error") or ""
            print(f"  [{r['status']}] {r['file']}  ->  {extra}")
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
