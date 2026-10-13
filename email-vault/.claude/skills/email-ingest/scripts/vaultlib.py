"""Small shared helpers for the email vault scripts (stdlib only)."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
WIKILINK_RE = re.compile(r"!?\[\[([^\]\|#\^]+)(?:[#\^][^\]\|]*)?(?:\|[^\]]*)?\]\]")


def utf8_stdout() -> None:
    """Make printing safe on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def find_vault(start: str | None = None) -> Path:
    """Vault root = explicit arg, else $CLAUDE_PROJECT_DIR, else the nearest dir with CLAUDE.md."""
    if start:
        return Path(start).resolve()
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    here = Path.cwd().resolve()
    for p in [here, *here.parents]:
        if (p / "CLAUDE.md").exists() and (p / "raw").is_dir():
            return p
    return here


def parse_frontmatter(text: str) -> dict:
    """Parse the simple YAML subset the vault uses: scalars, JSON-quoted strings, block lists.

    Good enough for files written by msg_to_md.py and by Claude following CLAUDE.md;
    it is not a general YAML parser.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    data: dict = {}
    key = None
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith(("  - ", "- ")) and key is not None:
            data.setdefault(key, [])
            if not isinstance(data[key], list):
                data[key] = []
            data[key].append(_scalar(line.split("- ", 1)[1]))
            continue
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val == "":
                data[key] = []
            elif val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                data[key] = [_scalar(v) for v in _split_inline(inner)] if inner else []
            else:
                data[key] = _scalar(val)
    return data


def _split_inline(s: str) -> list[str]:
    out, buf, q = [], "", None
    for ch in s:
        if q:
            buf += ch
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            buf += ch
        elif ch == ",":
            out.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out


def _scalar(v: str):
    v = v.strip()
    if v.startswith('"') and v.endswith('"'):
        try:
            return json.loads(v)
        except Exception:
            return v[1:-1]
    if v.startswith("'") and v.endswith("'"):
        return v[1:-1].replace("''", "'")
    if v in ("true", "false"):
        return v == "true"
    return v


def set_frontmatter_field(path: Path, key: str, value: str) -> bool:
    """Replace (or add) one top-level scalar field in a note's frontmatter."""
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return False
    block = m.group(1)
    line = f"{key}: {value}"
    pat = re.compile(rf"^{re.escape(key)}:.*$", re.M)
    new_block = pat.sub(line, block, count=1) if pat.search(block) else block + "\n" + line
    path.write_text(text.replace(block, new_block, 1), encoding="utf-8")
    return True


CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def wikilinks(text: str) -> list[str]:
    """Wikilink targets in a note, ignoring examples inside code spans/blocks."""
    return [t.strip() for t in WIKILINK_RE.findall(CODE_RE.sub("", text))]


def iter_notes(root: Path):
    for p in sorted(root.rglob("*.md")):
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        yield p
