#!/usr/bin/env python3
"""List raw emails waiting to be compiled into the wiki, grouped by thread; or mark them done.

    python pending.py                      # pending emails grouped by thread (oldest thread first)
    python pending.py --limit 10           # only the first 10 threads (one compile batch)
    python pending.py --json
    python pending.py mark compiled "raw/emails/2026/10/....md" [...]
    python pending.py mark skipped  "raw/emails/....md" --reason newsletter
    python pending.py stats                # counts by wiki_status
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vaultlib import find_vault, iter_notes, parse_frontmatter, set_frontmatter_field, utf8_stdout  # noqa: E402

VALID = {"pending", "compiled", "skipped"}
MAX_REASON_LEN = 40


def load(vault: Path):
    for p in iter_notes(vault / "raw" / "emails"):
        fm = parse_frontmatter(p.read_text(encoding="utf-8"))
        if fm.get("type") == "email":
            yield p, fm


def cmd_list(vault: Path, limit: int | None, as_json: bool) -> None:
    threads: dict[str, list] = defaultdict(list)
    for p, fm in load(vault):
        if fm.get("wiki_status", "pending") == "pending":
            threads[fm.get("thread_id") or fm.get("thread_subject", "?")].append((p, fm))
    groups = sorted(threads.values(), key=lambda g: min(str(fm.get("date")) for _, fm in g))
    total = sum(len(g) for g in groups)
    if limit:
        groups = groups[:limit]
    out = []
    for g in groups:
        g.sort(key=lambda x: str(x[1].get("date")))
        out.append({
            "thread_id": g[0][1].get("thread_id"),
            "thread_subject": g[0][1].get("thread_subject"),
            "emails": [{
                "path": p.relative_to(vault).as_posix(),
                "date": fm.get("date"),
                "from": fm.get("from"),
                "direction": fm.get("direction"),
                "chars": p.stat().st_size,
            } for p, fm in g],
        })
    if as_json:
        print(json.dumps({"pending_total": total, "threads_shown": len(out), "threads": out}, ensure_ascii=False, indent=1))
        return
    print(f"Pending emails: {total} in {len(threads)} threads" + (f" (showing {len(out)})" if limit else ""))
    for t in out:
        print(f"\n## {t['thread_subject']}  [{t['thread_id']}]")
        for e in t["emails"]:
            print(f"  - {e['date']}  {e['from']}  ({e['direction']}, {e['chars'] // 1000} KB)  {e['path']}")


def cmd_mark(vault: Path, status: str, paths: list[str], reason: str | None) -> int:
    if status not in VALID:
        print(f"status must be one of {sorted(VALID)}")
        return 2
    if reason is not None and len(reason) > MAX_REASON_LEN:
        print(f"--reason must be at most {MAX_REASON_LEN} characters")
        return 2
    bad = 0
    emails_root = (vault / "raw" / "emails").resolve()
    for raw in paths:
        p = (vault / raw) if not Path(raw).is_absolute() else Path(raw)
        if not p.suffix:
            p = p.with_suffix(".md")
        p = p.resolve()
        if not p.is_relative_to(emails_root) or p.suffix.lower() != ".md":
            print(f"  ! refused (not under raw/emails/): {raw}")
            bad += 1
            continue
        if p.exists() and parse_frontmatter(p.read_text(encoding="utf-8")).get("type") != "email":
            print(f"  ! refused (not an email note): {raw}")
            bad += 1
            continue
        if not p.exists() or not set_frontmatter_field(p, "wiki_status", status):
            print(f"  ! not found or no frontmatter: {raw}")
            bad += 1
            continue
        if reason:
            set_frontmatter_field(p, "wiki_skip_reason", json.dumps(reason))
        print(f"  {status}: {p.relative_to(vault).as_posix()}")
    return 1 if bad else 0


def cmd_stats(vault: Path) -> None:
    counts: dict[str, int] = defaultdict(int)
    for _, fm in load(vault):
        counts[fm.get("wiki_status", "pending")] += 1
    print(json.dumps(dict(counts)))


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault")
    sub = ap.add_subparsers(dest="cmd")
    ls = sub.add_parser("list")
    for parser in (ap, ls):
        parser.add_argument("--limit", type=int, help="max threads to show")
        parser.add_argument("--json", action="store_true")
    mk = sub.add_parser("mark")
    mk.add_argument("status")
    mk.add_argument("paths", nargs="+")
    mk.add_argument("--reason")
    sub.add_parser("stats")
    a = ap.parse_args()
    vault = find_vault(a.vault)
    if a.cmd == "mark":
        return cmd_mark(vault, a.status, a.paths, a.reason)
    if a.cmd == "stats":
        cmd_stats(vault)
        return 0
    cmd_list(vault, a.limit, a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
