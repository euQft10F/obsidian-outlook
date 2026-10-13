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
              attachments=()):
    message = EmailMessage()
    message["Subject"], message["From"] = subject, sender
    message["To"], message["Date"] = "me@work.com", "Mon, 05 Oct 2026 08:14:00 +0000"
    message["Message-ID"] = "<root-1@acme.co.uk>"
    message.set_content(body)
    for name, data in attachments:
        message.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
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
    assert 'source_file: "raw/msg/2026/10/' in text
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
