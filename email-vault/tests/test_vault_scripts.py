"""Characterization tests for the skill scripts: pin today's behaviour before refactoring them."""
import json
import re
import sys
from email.message import EmailMessage

import pytest


def run_cli(module, monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", [module.__name__ + ".py", *argv])
    return module.main()


def write_eml(path, *, subject="Re: Q3 budget", sender="Shah, Priya <priya@acme.co.uk>",
              body="Numbers attached.\n\nOn Mon, 5 Oct 2026, Tom wrote:\n> old text\n> more\n> more\n",
              attachments=(), message_id="<root-1@acme.co.uk>", embedded=None):
    message = EmailMessage()
    message["Subject"], message["From"] = subject, sender
    message["To"], message["Date"] = "me@work.com", "Mon, 05 Oct 2026 08:14:00 +0000"
    message["Message-ID"] = message_id
    message.set_content(body)
    for name, data in attachments:
        message.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    if embedded is not None:  # an email forwarded as an attachment (message/rfc822)
        message.add_attachment(embedded)
    path.write_bytes(message.as_bytes())
    return path


def write_note(vault, name, **fields):
    lines = ["---", "type: email", *(f'{key}: "{value}"' for key, value in fields.items()), "---", "", "# body"]
    path = vault / "raw" / "emails" / "2026" / "10" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------- vaultlib

def test_frontmatter_round_trip(vaultlib, tmp_path):
    text = '---\ntype: email\nsubject: "Hi: there"\nhas_attachments: true\nto:\n  - "a@b.com"\n  - "c@d.com"\ntags: [x, "y z"]\n---\nbody'
    assert vaultlib.parse_frontmatter(text) == {
        "type": "email", "subject": "Hi: there", "has_attachments": True,
        "to": ["a@b.com", "c@d.com"], "tags": ["x", "y z"],
    }
    note = tmp_path / "n.md"
    note.write_text(text, encoding="utf-8")
    assert vaultlib.set_frontmatter_field(note, "type", "person")
    assert vaultlib.set_frontmatter_field(note, "wiki_status", "compiled")
    parsed = vaultlib.parse_frontmatter(note.read_text(encoding="utf-8"))
    assert (parsed["type"], parsed["wiki_status"]) == ("person", "compiled")
    assert note.read_text(encoding="utf-8").endswith("---\nbody")


def test_remove_frontmatter_field(vaultlib, tmp_path):
    note = tmp_path / "n.md"
    note.write_text('---\ntype: email\nwiki_thread: "[[T]]"\nwiki_status: compiled\n---\n\nbody\n', encoding="utf-8")
    assert vaultlib.remove_frontmatter_field(note, "wiki_thread")
    assert vaultlib.parse_frontmatter(note.read_text(encoding="utf-8")) == {"type": "email", "wiki_status": "compiled"}
    assert not vaultlib.remove_frontmatter_field(note, "wiki_thread")
    assert vaultlib.remove_frontmatter_field(note, "wiki_status")  # last line of the block
    assert note.read_text(encoding="utf-8") == "---\ntype: email\n---\n\nbody\n"


def test_wikilinks_ignore_code_and_strip_aliases(vaultlib):
    text = "[[A|alias]] and [[B#head]] and `[[code]]` and ![[img.png]]\n```\n[[block]]\n```"
    assert vaultlib.wikilinks(text) == ["A", "B", "img.png"]


# ---------------------------------------------------------------- msg_to_md helpers

@pytest.mark.parametrize("raw, expected", [
    ("Shah, Priya <priya@acme.co.uk>; Tom Reid <tom@x.com>",
     [("Shah, Priya", "priya@acme.co.uk"), ("Tom Reid", "tom@x.com")]),
    ("a@b.com, c@d.com", [("a", "a@b.com"), ("c", "c@d.com")]),
    ("/o=exchangelabs/cn=Recipients/cn=jdoe", [("/o=exchangelabs/cn=Recipients/cn=jdoe", "")]),
    ("A <a@b.com>; A <a@b.com>", [("A", "a@b.com")]),
])
def test_parse_addresses(msg_to_md, raw, expected):
    assert [(a.name, a.email) for a in msg_to_md.parse_addresses(raw)] == expected


@pytest.mark.parametrize("body, expected_new", [
    ("Hi\n\nOn Mon, 5 Oct 2026, Tom Reid wrote:\nold", "Hi"),
    ("Hi\n\nFrom: Tom\nSent: Monday\nTo: me\nold", "Hi"),
    ("Hi\n\n> a\n> b\n> c\n", "Hi"),
    ("Just text\nmore text", "Just text\nmore text"),
])
def test_split_quoted(msg_to_md, body, expected_new):
    new, quoted = msg_to_md.split_quoted(body)
    assert new == expected_new
    assert bool(quoted) == (new != body.strip())


def test_subject_and_filename_helpers(msg_to_md):
    assert msg_to_md.normalize_subject("RE: Fwd: [External] Budget") == "Budget"
    assert msg_to_md.normalize_subject("RE:") == "(no subject)"


def test_thread_id_prefers_conversation_index_then_references_then_subject(msg_to_md):
    parsed = msg_to_md.ParsedEmail(subject="RE: Budget")
    assert msg_to_md.thread_id_for(parsed) == "subj-budget"
    parsed.references = ["<root@x>"]
    assert msg_to_md.thread_id_for(parsed).startswith("ref-")
    parsed.conversation_root = "ab" * 11
    assert msg_to_md.thread_id_for(parsed) == "conv-" + "ab" * 11


# ---------------------------------------------------------------- msg_to_md end to end

def test_convert_eml_archives_and_detects_duplicates(msg_to_md, vault, monkeypatch, capsys):
    eml = write_eml(vault / "00-inbox" / "a.eml", attachments=[("budget.txt", b"line one")])
    assert run_cli(msg_to_md, monkeypatch, "--vault", str(vault), "--me", "me@work.com") == 0
    note = next((vault / "raw" / "emails").rglob("*.md"))
    text = note.read_text(encoding="utf-8")
    assert re.fullmatch(r"raw/emails/2026/10/2026-10-05 \d{4} Shah, Priya - Q3 budget\.md", note.relative_to(vault).as_posix())
    for expected in ('type: email', 'thread_subject: "Q3 budget"', 'from_email: "priya@acme.co.uk"',
                     "direction: received", "has_attachments: true", "wiki_status: pending",
                     "## Message", "Numbers attached.", "## Attachments", "## Quoted history"):
        assert expected in text
    assert re.search(r'source_file: "\[\[raw/msg/2026/10/[^"]+\.eml\]\]"', text)
    assert not eml.exists() and (vault / "raw" / "msg" / "2026" / "10" / f"{note.stem}.eml").exists()
    extracted = list((vault / "raw" / "attachments").rglob("*.extracted.md"))
    assert len(extracted) == 1 and "line one" in extracted[0].read_text(encoding="utf-8")

    write_eml(vault / "00-inbox" / "again.eml", attachments=[("budget.txt", b"line one")])
    capsys.readouterr()
    run_cli(msg_to_md, monkeypatch, "--vault", str(vault))
    assert "Summary: duplicate 1" in capsys.readouterr().out
    assert (vault / "raw/msg/_duplicates/again.eml").exists()


def test_dry_run_writes_nothing(msg_to_md, vault, monkeypatch):
    eml = write_eml(vault / "00-inbox" / "a.eml")
    assert run_cli(msg_to_md, monkeypatch, "--vault", str(vault), "--dry-run") == 0
    assert eml.exists()
    assert not list((vault / "raw").rglob("*.md"))
    assert not (vault / "raw" / ".email-index.json").exists()


def test_unparseable_msg_goes_to_failed(msg_to_md, vault, monkeypatch):
    (vault / "00-inbox" / "bad.msg").write_bytes(b"not an ole file")
    assert run_cli(msg_to_md, monkeypatch, "--vault", str(vault)) == 1
    assert (vault / "00-inbox" / "_failed" / "bad.msg").exists()
    assert (vault / "00-inbox" / "_failed" / "bad.msg.error.txt").exists()


def test_executable_attachment_is_blocked_not_parsed(msg_to_md, vault, monkeypatch):
    write_eml(vault / "00-inbox" / "a.eml", attachments=[("run.exe", b"MZ")])
    run_cli(msg_to_md, monkeypatch, "--vault", str(vault))
    saved = [p.name for p in (vault / "raw" / "attachments").rglob("*") if p.is_file()]
    assert saved == ["run.exe.blocked"]
    assert "executable type, not opened" in next((vault / "raw" / "emails").rglob("*.md")).read_text(encoding="utf-8")


def test_note_like_attachment_cannot_pose_as_a_vault_note(msg_to_md, vault, monkeypatch):
    fake_page = b"---\ntype: person\n---\nSee ![](https://tracker.example/p.gif)"
    write_eml(vault / "00-inbox" / "a.eml", attachments=[("Priya Shah.md", fake_page)])
    run_cli(msg_to_md, monkeypatch, "--vault", str(vault))
    saved = sorted(p.name for p in (vault / "raw" / "attachments").rglob("*") if p.is_file())
    assert saved == ["Priya Shah.md.txt", "Priya Shah.md.txt.extracted.md"]
    extracted = next((vault / "raw" / "attachments").rglob("*.extracted.md")).read_text(encoding="utf-8")
    assert "![](" not in extracted and "!\\[](" in extracted


def test_email_body_cannot_embed_remote_content(msg_to_md, vault, monkeypatch):
    body = 'Hi ![](https://tracker.example/p.gif) and <iframe src="https://evil.example"></iframe>\n'
    write_eml(vault / "00-inbox" / "a.eml", sender='Eve <img src=x> <eve@evil.example>', body=body)
    run_cli(msg_to_md, monkeypatch, "--vault", str(vault))
    text = next((vault / "raw" / "emails").rglob("*.md")).read_text(encoding="utf-8")
    message = text.split("\n---\n", 1)[1]
    assert "![](" not in message and "<iframe" not in message and "<img" not in message
    assert "&lt;iframe" in message and "!\\[](https://tracker.example/p.gif)" in message


def test_one_failing_email_does_not_abort_the_batch(msg_to_md, vault, monkeypatch):
    write_eml(vault / "00-inbox" / "a.eml", subject="boom")
    write_eml(vault / "00-inbox" / "b.eml", subject="fine")
    original = msg_to_md.Converter._write_note

    def write_note_or_fail(self, parsed, *args, **kwargs):
        if parsed.subject == "boom":
            raise OSError("path too long")
        return original(self, parsed, *args, **kwargs)

    monkeypatch.setattr(msg_to_md.Converter, "_write_note", write_note_or_fail)
    assert run_cli(msg_to_md, monkeypatch, "--vault", str(vault)) == 1
    assert (vault / "00-inbox" / "_failed" / "a.eml").exists()
    assert [p.name for p in (vault / "raw" / "emails").rglob("*.md")][0].endswith("fine.md")
    assert (vault / "raw" / ".email-index.json").exists()


def stored_attachments(vault):
    return sorted(p.name for p in (vault / "raw" / "attachments").rglob("*") if p.is_file())


def test_attachments_skip_lists_but_does_not_store(msg_to_md, vault, monkeypatch, capsys):
    write_eml(vault / "00-inbox" / "a.eml", attachments=[("budget.txt", b"line one"), ("run.exe", b"MZ")])
    assert run_cli(msg_to_md, monkeypatch, "--vault", str(vault), "--attachments", "skip") == 0
    assert stored_attachments(vault) == []
    text = next((vault / "raw" / "emails").rglob("*.md")).read_text(encoding="utf-8")
    assert "has_attachments: true" in text and "[[raw/attachments" not in text
    assert "- budget.txt (0 KB) · not stored (attachments: skip)" in text
    assert "- run.exe.blocked (0 KB) · not stored (attachments: skip)" in text
    assert "Attachments: not stored (skip)" in capsys.readouterr().out


def test_attachments_mode_read_from_claude_md_and_cli_overrides(msg_to_md, vault, monkeypatch):
    (vault / "CLAUDE.md").write_text("## Converter settings\n\n- Attachments: Skip  (save | skip: `--attachments`)\n",
                                     encoding="utf-8")
    write_eml(vault / "00-inbox" / "a.eml", attachments=[("budget.txt", b"line one")])
    run_cli(msg_to_md, monkeypatch, "--vault", str(vault))
    assert stored_attachments(vault) == []
    write_eml(vault / "00-inbox" / "b.eml", subject="Other", message_id="<other@acme.co.uk>",
              attachments=[("budget.txt", b"line one")])
    run_cli(msg_to_md, monkeypatch, "--vault", str(vault), "--attachments", "save")
    assert stored_attachments(vault) == ["budget.txt", "budget.txt.extracted.md"]


def test_attachments_skip_still_converts_embedded_email(msg_to_md, vault, monkeypatch):
    inner = EmailMessage()
    inner["Subject"], inner["From"], inner["Message-ID"] = "Rate card", "Tom Reid <tom@x.com>", "<inner@x.com>"
    inner["Date"] = "Fri, 02 Oct 2026 10:00:00 +0000"
    inner.set_content("See the card.")
    inner.add_attachment(b"rates", maintype="application", subtype="octet-stream", filename="rates.csv")
    write_eml(vault / "00-inbox" / "a.eml", subject="FW: Rate card", embedded=inner)
    run_cli(msg_to_md, monkeypatch, "--vault", str(vault), "--attachments", "skip")
    notes = {p.stem: p.read_text(encoding="utf-8") for p in (vault / "raw" / "emails").rglob("*.md")}
    assert len(notes) == 2 and stored_attachments(vault) == []
    parent = next(text for stem, text in notes.items() if "Shah" in stem)
    child = next(text for stem, text in notes.items() if "Tom Reid" in stem)
    assert "- Embedded email: [[raw/emails/" in parent
    assert "rates.csv (0 KB) · not stored" in child


def test_files_outside_inbox_are_refused(msg_to_md, vault, monkeypatch, tmp_path_factory):
    outside = write_eml(tmp_path_factory.mktemp("elsewhere") / "x.eml")
    assert run_cli(msg_to_md, monkeypatch, "--vault", str(vault), str(outside)) == 2
    assert outside.exists()


# ---------------------------------------------------------------- pending

def test_pending_list_groups_by_thread_and_mark_updates(pending, vault, monkeypatch, capsys):
    first = write_note(vault, "a", thread_id="t1", thread_subject="Budget", date="2026-10-05T08:00:00", wiki_status="pending")
    write_note(vault, "b", thread_id="t1", thread_subject="Budget", date="2026-10-06T08:00:00", wiki_status="pending")
    write_note(vault, "c", thread_id="t2", thread_subject="Lunch", date="2026-10-07T08:00:00", wiki_status="compiled")
    run_cli(pending, monkeypatch, "--vault", str(vault), "--json")
    listing = json.loads(capsys.readouterr().out)
    assert listing["pending_total"] == 2 and [t["thread_id"] for t in listing["threads"]] == ["t1"]

    rel = first.relative_to(vault).as_posix()
    assert run_cli(pending, monkeypatch, "--vault", str(vault), "mark", "skipped", rel, "--reason", "newsletter") == 0
    text = first.read_text(encoding="utf-8")
    assert 'wiki_status: skipped' in text and 'wiki_skip_reason: "newsletter"' in text

    capsys.readouterr()
    run_cli(pending, monkeypatch, "--vault", str(vault), "stats")
    assert json.loads(capsys.readouterr().out) == {"skipped": 1, "pending": 1, "compiled": 1}


def test_pending_mark_refusals(pending, vault, monkeypatch):
    note = write_note(vault, "a", thread_id="t1", wiki_status="pending")
    rel = note.relative_to(vault).as_posix()
    wiki_page = vault / "wiki" / "people" / "Priya.md"
    wiki_page.write_text("---\ntype: person\n---\n", encoding="utf-8")
    base = ("--vault", str(vault), "mark", "compiled")
    assert run_cli(pending, monkeypatch, *base, "wiki/people/Priya.md") == 1
    assert run_cli(pending, monkeypatch, *base, "../outside.md") == 1
    for bad_argv in (("--vault", str(vault), "mark", "bogus", rel), (*base, rel, "--reason", "x" * 41)):
        with pytest.raises(SystemExit) as refused:  # rejected by argparse
            run_cli(pending, monkeypatch, *bad_argv)
        assert refused.value.code == 2
    assert 'wiki_status: "pending"' in note.read_text(encoding="utf-8")


def write_page(vault, folder, name, page_type, sources=(), last_activity="2026-10-06"):
    lines = ["---", f"type: {page_type}", f"last_activity: {last_activity}", "sources:",
             *(f'  - "[[{source}]]"' for source in sources), "---", ""]
    path = vault / "wiki" / folder / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def link_threads(pending, vault, monkeypatch, capsys):
    capsys.readouterr()
    assert run_cli(pending, monkeypatch, "--vault", str(vault), "link-threads", "--json") == 0
    return json.loads(capsys.readouterr().out)


def wiki_thread(note, vaultlib):
    return vaultlib.parse_frontmatter(note.read_text(encoding="utf-8")).get("wiki_thread")


def test_link_threads_sets_and_is_idempotent(pending, vaultlib, vault, monkeypatch, capsys):
    cited = write_note(vault, "2026-10-05 0814 Shah, Priya - Budget", wiki_status="compiled")
    other = write_note(vault, "2026-10-06 0900 Reid, Tom - Lunch", wiki_status="pending")
    write_page(vault, "threads", "Budget (2026-10)", "thread", [cited.stem])
    assert link_threads(pending, vault, monkeypatch, capsys)["set"] == 1
    assert wiki_thread(cited, vaultlib) == "[[Budget (2026-10)]]" and wiki_thread(other, vaultlib) is None
    assert 'wiki_status: "compiled"' in cited.read_text(encoding="utf-8")  # rest of the note untouched

    mtime = cited.stat().st_mtime_ns
    assert link_threads(pending, vault, monkeypatch, capsys) == {
        "set": 0, "cleared": 0, "unchanged": 1, "dry_run": False, "warnings": []}
    assert cited.stat().st_mtime_ns == mtime


def test_link_threads_follows_merges_and_clears(pending, vaultlib, vault, monkeypatch, capsys):
    first = write_note(vault, "a", wiki_status="compiled")
    second = write_note(vault, "b", wiki_status="compiled")
    old_page = write_page(vault, "threads", "Old", "thread", ["a", "raw/emails/2026/10/b.md"])
    link_threads(pending, vault, monkeypatch, capsys)
    assert wiki_thread(second, vaultlib) == "[[Old]]"  # path-style source links resolve too

    old_page.unlink()  # merged into "New", which keeps only one of the emails
    write_page(vault, "threads", "New", "thread", ["a"])
    result = link_threads(pending, vault, monkeypatch, capsys)
    assert (result["set"], result["cleared"]) == (1, 1)
    assert wiki_thread(first, vaultlib) == "[[New]]" and wiki_thread(second, vaultlib) is None
    assert "wiki_thread" not in second.read_text(encoding="utf-8")


def test_link_threads_ignores_non_thread_pages(pending, vaultlib, vault, monkeypatch, capsys):
    note = write_note(vault, "a", wiki_status="compiled")
    write_page(vault, "people", "Priya Shah", "person", ["a"])
    write_page(vault, "threads", "Stray", "project", ["a"])
    assert link_threads(pending, vault, monkeypatch, capsys)["set"] == 0
    assert wiki_thread(note, vaultlib) is None


def test_link_threads_double_citation_prefers_newest(pending, vaultlib, vault, monkeypatch, capsys):
    note = write_note(vault, "a", wiki_status="compiled")
    write_page(vault, "threads", "Older", "thread", ["a"], last_activity="2026-10-01")
    write_page(vault, "threads", "Newer", "thread", ["a"], last_activity="2026-10-09")
    result = link_threads(pending, vault, monkeypatch, capsys)
    assert wiki_thread(note, vaultlib) == "[[Newer]]"
    assert result["warnings"] == ["a is cited by Newer and Older; linked to Newer"]


def test_link_threads_dry_run_writes_nothing(pending, vault, monkeypatch, capsys):
    note = write_note(vault, "a", wiki_status="compiled")
    write_page(vault, "threads", "T", "thread", ["a"])
    capsys.readouterr()
    run_cli(pending, monkeypatch, "--vault", str(vault), "link-threads", "--dry-run")
    assert "Would link: 1 set" in capsys.readouterr().out
    assert "wiki_thread" not in note.read_text(encoding="utf-8")


def test_scripts_reject_abbreviated_vault(msg_to_md, pending, lint_vault, vault, monkeypatch):
    # settings.json denies the literal `--vault`; an abbreviation must not slip past it
    for module in (msg_to_md, pending, lint_vault):
        with pytest.raises(SystemExit) as refused:
            run_cli(module, monkeypatch, "--vau", str(vault))
        assert refused.value.code == 2


# ---------------------------------------------------------------- lint_vault

def test_lint_reports_broken_orphan_and_overdue(lint_vault, vault, monkeypatch, capsys):
    (vault / "wiki" / "index.md").write_text("---\ntype: index\n---\n[[Budget (2026-10)]]\n", encoding="utf-8")
    (vault / "wiki" / "threads" / "Budget (2026-10).md").write_text(
        "---\ntype: thread\nstatus: open\ndue: 2020-01-01\nsources:\n  - \"[[x]]\"\n---\n[[Nowhere]]\n", encoding="utf-8")
    (vault / "wiki" / "people" / "Lonely.md").write_text("---\ntype: person\nemails:\n  - \"l@x.com\"\n---\n", encoding="utf-8")
    run_cli(lint_vault, monkeypatch, "--vault", str(vault), "--json")
    issues = json.loads(capsys.readouterr().out)["issues"]
    assert "wiki/threads/Budget (2026-10).md -> [[Nowhere]]" in issues["Broken wikilinks"]
    assert issues["Orphan pages (no links from other wiki pages)"] == ["wiki/people/Lonely.md"]
    assert issues["Pages not listed in wiki/index.md"] == ["wiki/people/Lonely.md"]
    assert any("due 2020-01-01" in line for line in issues["Overdue threads (status open/waiting, due date passed)"])


def test_lint_accepts_unindexed_draft_pages(lint_vault, vault, monkeypatch, capsys):
    (vault / "wiki" / "index.md").write_text("# no frontmatter: still exempt by name\n", encoding="utf-8")
    (vault / "wiki" / "drafts").mkdir()
    (vault / "wiki" / "threads" / "Budget (2026-10).md").write_text("---\ntype: thread\n---\n", encoding="utf-8")
    (vault / "wiki" / "drafts" / "Draft - Budget (2026-10).md").write_text(
        "---\ntype: draft\n---\nHi Priya,\n\n## Notes\n- [[Budget (2026-10)]]\n- [[Typo]]\n", encoding="utf-8")
    run_cli(lint_vault, monkeypatch, "--vault", str(vault), "--json")
    issues = json.loads(capsys.readouterr().out)["issues"]
    flagged = [line for key in (lint_vault.ORPHAN, lint_vault.NOT_INDEXED) for line in issues.get(key, [])]
    assert not any("drafts/" in line or line == "wiki/index.md" for line in flagged)
    assert not any("drafts/" in line for line in issues.get(lint_vault.BAD_TYPE, []))
    assert "wiki/threads/Budget (2026-10).md" in issues[lint_vault.ORPHAN]  # a draft's link doesn't count
    assert "wiki/drafts/Draft - Budget (2026-10).md -> [[Typo]]" in issues[lint_vault.BROKEN_LINKS]


# ---------------------------------------------------------------- owner setup

TEMPLATE_OWNER = "## Owner (fill in)\n\n- Owner: <your name>\n- Owner's addresses: <you@work.com>\n"


@pytest.mark.parametrize("schema, owner, expected", [
    (TEMPLATE_OWNER, set(), ["<your name>, <you@work.com>", "EMAIL_VAULT_ME"]),
    (TEMPLATE_OWNER, {"max@work.com"}, ["<your name>, <you@work.com>"]),
    ("## Owner\n\n- Owner: Max\n- Owner's addresses: max@work.com\n", {"max@work.com"}, []),
])
def test_check_setup(vaultlib, vault, schema, owner, expected):
    (vault / "CLAUDE.md").write_text(schema, encoding="utf-8")
    warnings = vaultlib.check_setup(vault, owner)
    assert len(warnings) == len(expected)
    assert all(fragment in warning for fragment, warning in zip(expected, warnings, strict=True))


def test_owner_addresses_read_from_claude_md(vaultlib, vault, monkeypatch):
    monkeypatch.delenv("EMAIL_VAULT_ME", raising=False)
    (vault / "CLAUDE.md").write_text(TEMPLATE_OWNER, encoding="utf-8")
    assert vaultlib.owner_addresses([], vault) == set()
    schema = "## Owner\n\n- Owner's addresses: Max@Work.com, max@home.org  (also `--me`)\n\n## Next\n<your x>\n"
    (vault / "CLAUDE.md").write_text(schema, encoding="utf-8")
    assert vaultlib.owner_addresses(["cli@x.com"], vault) == {"max@work.com", "max@home.org", "cli@x.com"}
    assert vaultlib.check_setup(vault, {"max@work.com"}) == []  # placeholders outside the Owner section don't count


TEAM_LINE_UNFILLED = ("- Team addresses: none  (optional: distribution lists I also track, e.g. `team@work.com`; "
                      "the scripts read this line)\n")


def test_team_addresses_are_optional_and_separate_from_owner(vaultlib, vault, monkeypatch):
    monkeypatch.delenv("EMAIL_VAULT_ME", raising=False)
    owner = "## Owner\n\n- Owner: Max\n- Owner's addresses: max@work.com\n"
    (vault / "CLAUDE.md").write_text(owner, encoding="utf-8")
    assert vaultlib.team_addresses(vault) == set()  # no Team line at all
    (vault / "CLAUDE.md").write_text(owner + TEAM_LINE_UNFILLED, encoding="utf-8")
    assert vaultlib.team_addresses(vault) == set()  # the code-span example is not an address
    assert vaultlib.check_setup(vault, vaultlib.owner_addresses([], vault)) == []  # unfilled Team is not a warning
    (vault / "CLAUDE.md").write_text(owner + "- Team addresses: DreamTeam@coolco.uk, ops@coolco.uk\n", encoding="utf-8")
    assert vaultlib.team_addresses(vault) == {"dreamteam@coolco.uk", "ops@coolco.uk"}
    assert vaultlib.owner_addresses([], vault) == {"max@work.com"}  # team mail is not "me"


MEMBERS_LINE_UNFILLED = ("- Team members: none  (optional: teammates as `First Last <address>`, comma-separated; "
                         "the scripts read this line)\n")


def test_team_members_are_optional_and_separate_from_owner_and_aliases(vaultlib, vault, monkeypatch):
    monkeypatch.delenv("EMAIL_VAULT_ME", raising=False)
    owner = "## Owner\n\n- Owner: Max\n- Owner's addresses: max@work.com\n"
    (vault / "CLAUDE.md").write_text(owner, encoding="utf-8")
    assert vaultlib.team_member_addresses(vault) == set()  # no Team members line at all
    (vault / "CLAUDE.md").write_text(owner + MEMBERS_LINE_UNFILLED, encoding="utf-8")
    assert vaultlib.team_member_addresses(vault) == set()  # the code-span example is not an address
    assert vaultlib.check_setup(vault, vaultlib.owner_addresses([], vault)) == []  # unfilled is not a warning
    members = "- Team members: Tom Reid <Tom.Reid@coolco.uk>, Zoë Müller <zoe@coolco.uk>\n"
    (vault / "CLAUDE.md").write_text(owner + members, encoding="utf-8")
    assert vaultlib.team_member_addresses(vault) == {"tom.reid@coolco.uk", "zoe@coolco.uk"}
    assert vaultlib.owner_addresses([], vault) == {"max@work.com"}
    assert vaultlib.team_addresses(vault) == set()
    assert vaultlib.check_setup(vault, vaultlib.owner_addresses([], vault)) == []


@pytest.mark.parametrize("members_line, warning", [
    ("- Team members: Tom Reid, Zoë Müller\n", "Team members has no addresses"),
    ("- Team members: Tom Reid <tom@coolco.uk>, Max <MAX@work.com>\n", "Owner address listed as a team member"),
])
def test_check_setup_warns_about_team_members(vaultlib, vault, members_line, warning):
    (vault / "CLAUDE.md").write_text("## Owner\n\n- Owner's addresses: max@work.com\n" + members_line, encoding="utf-8")
    warnings = vaultlib.check_setup(vault, {"max@work.com"})
    assert len(warnings) == 1 and warning in warnings[0]


def test_lint_still_flags_teammate_without_person_page(lint_vault, vault, monkeypatch, capsys):
    monkeypatch.delenv("EMAIL_VAULT_ME", raising=False)
    (vault / "CLAUDE.md").write_text("## Owner\n\n- Owner's addresses: max@work.com\n"
                                     "- Team members: Tom Reid <tom@coolco.uk>\n", encoding="utf-8")
    for n in range(3):
        write_note(vault, f"tom {n}", from_email="tom@coolco.uk", wiki_status="pending")
    run_cli(lint_vault, monkeypatch, "--vault", str(vault), "--json")
    issues = json.loads(capsys.readouterr().out)["issues"]
    frequent = next(items for key, items in issues.items() if key.startswith("Frequent correspondents"))
    assert frequent == ["tom@coolco.uk (3)"]


@pytest.mark.parametrize("team_line, flagged", [
    (TEAM_LINE_UNFILLED, {"priya@acme.co.uk", "dreamteam@coolco.uk"}),
    ("- Team addresses: dreamteam@coolco.uk\n", {"priya@acme.co.uk"}),
])
def test_lint_never_flags_team_alias_as_correspondent(lint_vault, vault, monkeypatch, capsys, team_line, flagged):
    monkeypatch.delenv("EMAIL_VAULT_ME", raising=False)
    (vault / "CLAUDE.md").write_text("## Owner\n\n- Owner's addresses: max@work.com\n" + team_line, encoding="utf-8")
    for n in range(3):
        for sender in ("priya@acme.co.uk", "dreamteam@coolco.uk", "max@work.com"):
            write_note(vault, f"{sender} {n}", from_email=sender, wiki_status="pending")
    run_cli(lint_vault, monkeypatch, "--vault", str(vault), "--json")
    issues = json.loads(capsys.readouterr().out)["issues"]
    frequent = next(items for key, items in issues.items() if key.startswith("Frequent correspondents"))
    assert {line.split(" ")[0] for line in frequent} == flagged


@pytest.mark.parametrize("settings, mode, warned", [
    ("", "save", False),  # no Converter settings section
    ("## Converter settings\n\n- Attachments: skip\n", "skip", False),
    ("## Converter settings\n\n- Attachments: save  (save | skip)\n", "save", False),
    ("## Converter settings\n\n- Attachments: maybe\n", "save", True),
    ("## Other\n\n- Attachments: skip\n", "save", False),  # only the Converter settings section counts
])
def test_attachments_mode_from_claude_md(vaultlib, vault, settings, mode, warned):
    (vault / "CLAUDE.md").write_text("## Owner\n\n- Owner's addresses: max@work.com\n\n" + settings, encoding="utf-8")
    assert vaultlib.attachments_mode(vault) == mode
    warnings = vaultlib.check_setup(vault, {"max@work.com"})
    assert any("Attachments must be save or skip (got 'maybe')" in w for w in warnings) == warned


def test_check_setup_tolerates_non_utf8_claude_md(vaultlib, vault):
    (vault / "CLAUDE.md").write_bytes("## Owner\n\n- Owner: Zoë <your name>\n".encode("cp1252"))
    assert "<your name>" in vaultlib.check_setup(vault, {"me@x.com"})[0]


def test_setup_warnings_reach_lint_and_converter_output(lint_vault, msg_to_md, vault, monkeypatch, capsys):
    monkeypatch.delenv("EMAIL_VAULT_ME", raising=False)
    (vault / "CLAUDE.md").write_text(TEMPLATE_OWNER, encoding="utf-8")
    run_cli(lint_vault, monkeypatch, "--vault", str(vault))
    assert "## Setup (2)" in capsys.readouterr().out
    run_cli(msg_to_md, monkeypatch, "--vault", str(vault), "--json")
    assert len(json.loads(capsys.readouterr().out)["setup"]) == 2
    monkeypatch.setenv("EMAIL_VAULT_ME", "max@work.com")
    run_cli(msg_to_md, monkeypatch, "--vault", str(vault))
    assert "EMAIL_VAULT_ME" not in capsys.readouterr().out
