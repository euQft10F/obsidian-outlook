"""Small shared helpers for the email vault scripts (stdlib only)."""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

Frontmatter = dict[str, str | bool | list[str]]

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
WIKILINK_RE = re.compile(r"!?\[\[([^\]\|#\^]+)(?:[#\^][^\]\|]*)?(?:\|[^\]]*)?\]\]")
INBOX_DIRNAME = "00-inbox"
FAILED_DIRNAME = "_failed"  # inside the inbox: files that would not parse
EMAIL_SUFFIXES = (".msg", ".eml")  # what the inbox accepts
CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
OWNER_PLACEHOLDER_RE = re.compile(r"<you(?:r\b|@)[^>\n]*>")  # CLAUDE.md template values: <your name>, <you@work.com>
# Characters Windows forbids in file names, plus ones that break Obsidian [[links]] and tags.
BAD_FILENAME_CHARS_RE = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]+')
WINDOWS_RESERVED_RE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)
MAX_FILENAME_LENGTH = 180  # leaves room under Windows' 260-character path limit for the folders above


def utf8_stdout() -> None:
    """Make printing safe on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # not a real console stream (captured by a test or a pipe wrapper)


def find_vault(explicit_root: str | Path | None = None, default: Path | None = None) -> Path:
    """Vault root = explicit arg, else $CLAUDE_PROJECT_DIR, else the nearest dir with CLAUDE.md,
    else `default` (or the current directory)."""
    if explicit_root:
        return Path(explicit_root).resolve()
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        return Path(project_dir).resolve()
    cwd = Path.cwd().resolve()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "CLAUDE.md").exists() and (candidate / "raw").is_dir():
            return candidate
    return default.resolve() if default else cwd


def sanitize_filename(name: str, max_length: int = MAX_FILENAME_LENGTH) -> str:
    """A string that is safe as a Windows file name and as an Obsidian link target."""
    name = BAD_FILENAME_CHARS_RE.sub(" ", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:max_length].rstrip(" .") or "untitled"
    return f"_{name}" if WINDOWS_RESERVED_RE.match(name) else name


def owner_addresses(cli_values: Iterable[str]) -> set[str]:
    """The owner's own addresses: `--me` values plus the comma-separated $EMAIL_VAULT_ME, lowercased."""
    from_env = os.environ.get("EMAIL_VAULT_ME", "").split(",")
    return {address.strip().lower() for address in [*cli_values, *from_env] if address.strip()}


def check_setup(vault: Path, owner: set[str]) -> list[str]:
    """Warnings about owner configuration the CLAUDE.md template leaves for the user to fill in."""
    warnings = []
    schema = vault / "CLAUDE.md"
    placeholders = OWNER_PLACEHOLDER_RE.findall(schema.read_text(encoding="utf-8")) if schema.exists() else []
    if placeholders:
        warnings.append(f"CLAUDE.md Owner section still has template placeholders: {', '.join(placeholders)}")
    if not owner:
        warnings.append("No owner address known: pass --me or set env EMAIL_VAULT_ME")
    return warnings


def parse_frontmatter(text: str) -> Frontmatter:
    """Parse the simple YAML subset the vault uses: scalars, JSON-quoted strings, block lists.

    Good enough for files written by msg_to_md.py and by Claude following CLAUDE.md;
    it is not a general YAML parser.
    """
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    data: Frontmatter = {}
    current_key = None
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith(("  - ", "- ")) and current_key is not None:
            if not isinstance(data.get(current_key), list):
                data[current_key] = []
            data[current_key].append(_parse_scalar(line.split("- ", 1)[1]))
        elif ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            current_key, value = key.strip(), value.strip()
            if value == "":
                data[current_key] = []
            elif value.startswith("[") and value.endswith("]"):
                inline_items = value[1:-1].strip()
                items = _split_inline_list(inline_items) if inline_items else []
                data[current_key] = [_parse_scalar(item) for item in items]
            else:
                data[current_key] = _parse_scalar(value)
    return data


def _split_inline_list(text: str) -> list[str]:
    """Split `a, "b, c", d` on commas that are outside quotes."""
    items: list[str] = []
    current = ""
    open_quote = None
    for char in text:
        if open_quote:
            current += char
            if char == open_quote:
                open_quote = None
        elif char in "\"'":
            open_quote = char
            current += char
        elif char == ",":
            items.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        items.append(current.strip())
    return items


def _parse_scalar(value: str) -> str | bool:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value in ("true", "false"):
        return value == "true"
    return value


def set_frontmatter_field(path: Path, key: str, value: str) -> bool:
    """Replace (or add) one top-level scalar field in a note's frontmatter. False if it has none."""
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return False
    block = match.group(1)
    new_line = f"{key}: {value}"
    existing_line = re.compile(rf"^{re.escape(key)}:.*$", re.M)
    if existing_line.search(block):
        new_block = existing_line.sub(new_line, block, count=1)
    else:
        new_block = f"{block}\n{new_line}"
    start, end = match.span(1)
    path.write_text(text[:start] + new_block + text[end:], encoding="utf-8")
    return True


def wikilinks(text: str) -> list[str]:
    """Wikilink targets in a note, ignoring examples inside code spans/blocks."""
    return [target.strip() for target in WIKILINK_RE.findall(CODE_SPAN_RE.sub("", text))]


def iter_files(root: Path) -> Iterator[Path]:
    """Every file under `root` outside dot-folders (.git, .venv, .obsidian...), which are never entered."""
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if not name.startswith(".")]
        for filename in filenames:
            if not filename.startswith("."):
                yield Path(directory) / filename


def iter_notes(root: Path) -> Iterator[Path]:
    """Markdown files under `root`, sorted, skipping anything inside a dot-folder."""
    yield from sorted(path for path in iter_files(root) if path.suffix == ".md")
