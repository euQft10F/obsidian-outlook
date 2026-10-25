"""prune.py: preview/apply, the three levels, index and wiki repair, refusals."""
import json
from email.message import EmailMessage
from pathlib import Path

import pytest
from test_vault_scripts import run_cli, write_eml

THREAD = "Q3 budget (2026-10)"


def convert(msg_to_md, vault, monkeypatch, name="a.eml", **eml):
    write_eml(vault / "00-inbox" / name, **eml)
    assert run_cli(msg_to_md, monkeypatch, "--vault", str(vault)) == 0


def email_notes(vault):
    return sorted((vault / "raw" / "emails").rglob("*.md"))


def raw_files(vault, folder):
    root = vault / "raw" / folder
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def write_thread(vault, stem, body="", name=THREAD):
    page = vault / "wiki" / "threads" / f"{name}.md"
    page.write_text("---\ntype: thread\nstatus: open\nsources:\n"
                    f'  - "[[{stem}]]"\n  - "[[Other email]]"\n---\n\n## Timeline\n'
                    f"- 2026-10-05 Priya sent numbers ([[{stem}|5 Oct, Priya]])\n{body}", encoding="utf-8")
    return page


def prune_json(prune, vault, monkeypatch, capsys, *argv):
    capsys.readouterr()
    assert run_cli(prune, monkeypatch, "--vault", str(vault), *argv, "--json") == 0
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def one_email(msg_to_md, vault, monkeypatch):
    """One converted email with a stored attachment (+ extracted text) and an archived original."""
    convert(msg_to_md, vault, monkeypatch, attachments=[("budget.txt", b"line one")])
    return email_notes(vault)[0]


def test_preview_changes_nothing(prune, one_email, vault, monkeypatch, capsys):
    write_thread(vault, one_email.stem)
    before = {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    report = prune_json(prune, vault, monkeypatch, capsys, "preview", "--status", "pending")
    assert report["applied"] is False and report["files"] == 4  # original, attachment, extracted text, note
    assert report["index_entries"] == 2 and report["wiki"] == {f"wiki/threads/{THREAD}.md": 2}
    assert {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()} == before


def test_level_all_removes_everything_and_allows_reimport(prune, msg_to_md, one_email, vault, monkeypatch, capsys):
    page = write_thread(vault, one_email.stem, body="Example: `[[" + one_email.stem + "]]`\n")
    relative = one_email.relative_to(vault).as_posix()
    report = prune_json(prune, vault, monkeypatch, capsys, "apply", relative)
    assert report["applied"] is True and report["emptied_threads"] == []
    assert email_notes(vault) == [] and raw_files(vault, "attachments") == [] and raw_files(vault, "msg") == []
    assert (vault / "raw" / "emails").is_dir() and not (vault / "raw" / "emails" / "2026").exists()
    index = json.loads((vault / "raw" / ".email-index.json").read_text(encoding="utf-8"))
    assert index == {"by_msgid": {}, "by_sha": {}}

    text = page.read_text(encoding="utf-8")
    assert '- "[[Other email]]"' in text and f'"[[{one_email.stem}]]"' not in text.split("---")[1]
    assert "(5 Oct, Priya (deleted))" in text
    assert f"`[[{one_email.stem}]]`" in text  # code spans are examples, left alone

    log = [json.loads(line) for line in (vault / "raw" / ".prune-log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(line["level"], line["note"], line["files"]) for line in log] == [("all", relative, 4)]

    convert(msg_to_md, vault, monkeypatch, attachments=[("budget.txt", b"line one")])  # same email again
    assert [path.stem for path in email_notes(vault)] == [one_email.stem]


def test_level_originals_keeps_note_and_attachments(prune, one_email, vault, monkeypatch, capsys):
    prune_json(prune, vault, monkeypatch, capsys, "apply", "--status", "pending", "--level", "originals")
    assert raw_files(vault, "msg") == [] and len(raw_files(vault, "attachments")) == 2
    text = one_email.read_text(encoding="utf-8")
    assert 'source_file: ""' in text and "pruned: originals" in text and "[[raw/attachments/" in text
    assert json.loads((vault / "raw" / ".email-index.json").read_text(encoding="utf-8"))["by_msgid"]


def test_level_attachments_rewrites_bullets_and_never_lowers_pruned(prune, one_email, vault, monkeypatch, capsys):
    page = write_thread(vault, one_email.stem, body="- Budget: [[budget.txt]]\n")
    report = prune_json(prune, vault, monkeypatch, capsys, "apply", "--status", "pending", "--level", "attachments")
    assert report["files"] == 3 and report["index_entries"] == 0
    assert raw_files(vault, "msg") == [] and raw_files(vault, "attachments") == []
    text = one_email.read_text(encoding="utf-8")
    assert "pruned: attachments" in text and "[[raw/attachments/" not in text
    assert "- budget.txt (0 KB) · removed (pruned " in text
    page_text = page.read_text(encoding="utf-8")
    assert "- Budget: budget.txt (deleted)" in page_text and f"[[{one_email.stem}|5 Oct, Priya]]" in page_text

    report = prune_json(prune, vault, monkeypatch, capsys, "apply", "--status", "pending", "--level", "originals")
    assert report["files"] == 0 and "pruned: attachments" in one_email.read_text(encoding="utf-8")


def test_embedded_emails_go_with_their_parent(prune, msg_to_md, vault, monkeypatch, capsys):
    inner = EmailMessage()
    inner["Subject"], inner["From"], inner["Message-ID"] = "Rate card", "Tom Reid <tom@x.com>", "<inner@x.com>"
    inner["Date"] = "Fri, 02 Oct 2026 10:00:00 +0000"
    inner.set_content("See the card.")
    inner.add_attachment(b"rates", maintype="application", subtype="octet-stream", filename="rates.csv")
    convert(msg_to_md, vault, monkeypatch, subject="FW: Rate card", embedded=inner)
    parent = next(path for path in email_notes(vault) if "Shah" in path.stem)

    report = prune_json(prune, vault, monkeypatch, capsys, "preview", parent.relative_to(vault).as_posix())
    assert {"Shah" in note["note"]: note["embedded"] for note in report["notes"]} == {True: False, False: True}
    prune_json(prune, vault, monkeypatch, capsys, "apply", parent.relative_to(vault).as_posix())
    assert email_notes(vault) == [] and raw_files(vault, "attachments") == []


def test_filters_combine(prune, msg_to_md, vault, monkeypatch, capsys):
    convert(msg_to_md, vault, monkeypatch, "a.eml", sender="News <news@letters.example>", message_id="<a@x>")
    convert(msg_to_md, vault, monkeypatch, "b.eml", subject="Lunch", message_id="<b@x>")
    report = prune_json(prune, vault, monkeypatch, capsys, "preview", "--from", "@LETTERS.example")
    assert [note["note"].split(" ", 2)[2] for note in report["notes"]] == ["News - Q3 budget.md"]
    assert prune_json(prune, vault, monkeypatch, capsys, "preview", "--before", "2026-10-05")["notes"] == []
    assert len(prune_json(prune, vault, monkeypatch, capsys, "preview", "--before", "2026-10-06")["notes"]) == 2
    report = prune_json(prune, vault, monkeypatch, capsys, "preview", "--before", "2026-10-06", "--status", "compiled")
    assert report["notes"] == []


def test_thread_selector_and_emptied_thread(prune, one_email, vault, monkeypatch, capsys):
    page = vault / "wiki" / "threads" / f"{THREAD}.md"
    page.write_text(f'---\ntype: thread\nsources:\n  - "[[{one_email.stem}]]"\n---\n', encoding="utf-8")
    report = prune_json(prune, vault, monkeypatch, capsys, "apply", "--thread", THREAD)
    assert len(report["notes"]) == 1 and report["emptied_threads"] == [f"wiki/threads/{THREAD}.md"]
    assert page.exists()  # proposing its deletion is the skill's job


def test_duplicates_folder_is_emptied(prune, msg_to_md, one_email, vault, monkeypatch, capsys):
    convert(msg_to_md, vault, monkeypatch, "again.eml", attachments=[("budget.txt", b"line one")])
    assert raw_files(vault, "msg/_duplicates") == ["again.eml"]
    report = prune_json(prune, vault, monkeypatch, capsys, "apply", "--duplicates")
    assert report["duplicates"] == 1 and report["notes"] == []
    assert not (vault / "raw" / "msg" / "_duplicates").exists() and email_notes(vault) == [one_email]


def test_lint_finds_no_broken_links_after_prune(prune, lint_vault, one_email, vault, monkeypatch, capsys):
    (vault / "wiki" / "people").mkdir(exist_ok=True)
    (vault / "wiki" / "people" / "Priya Shah.md").write_text(
        f"---\ntype: person\n---\n| Date | Email |\n|---|---|\n| 5 Oct | [[{one_email.stem}\\|budget]] |\n",
        encoding="utf-8")
    write_thread(vault, one_email.stem)
    (vault / "wiki" / "threads" / "Other email.md").write_text("---\ntype: thread\n---\n", encoding="utf-8")
    prune_json(prune, vault, monkeypatch, capsys, "apply", "--status", "pending")
    assert "| 5 Oct | budget (deleted) |" in (vault / "wiki" / "people" / "Priya Shah.md").read_text(encoding="utf-8")
    run_cli(lint_vault, monkeypatch, "--vault", str(vault), "--json")
    assert "Broken wikilinks" not in json.loads(capsys.readouterr().out)["issues"]


@pytest.mark.parametrize("argv", [
    ("apply",),  # no selector at all
    ("apply", "wiki/threads/x.md"),  # not under raw/emails
    ("apply", "raw/emails/2026/10/missing.md"),
    ("apply", "--thread", "No such thread"),
])
def test_refusals_delete_nothing(prune, one_email, vault, monkeypatch, capsys, argv):
    (vault / "wiki" / "threads" / "x.md").write_text("---\ntype: thread\n---\n", encoding="utf-8")
    assert run_cli(prune, monkeypatch, "--vault", str(vault), *argv) == 2
    assert email_notes(vault) == [one_email] and len(raw_files(vault, "msg")) == 1


def test_rejects_abbreviated_vault_and_options(prune, vault, monkeypatch):
    for argv in (("--vau", str(vault), "preview", "--status", "pending"),
                 ("--vault", str(vault), "apply", "--stat", "pending")):
        with pytest.raises(SystemExit) as refused:
            run_cli(prune, monkeypatch, *argv)
        assert refused.value.code == 2


def test_before_never_selects_undated_notes(prune, one_email, vault, monkeypatch, capsys):
    undated = one_email.with_name("undated.md")
    undated.write_text("---\ntype: email\nwiki_status: pending\n---\n# body\n", encoding="utf-8")
    report = prune_json(prune, vault, monkeypatch, capsys, "preview", "--before", "2030-01-01")
    assert [note["note"].rsplit("/", 1)[-1] for note in report["notes"]] == [one_email.name]


def test_failed_delete_still_finishes_log_index_and_wiki(prune, one_email, vault, monkeypatch, capsys):
    page = write_thread(vault, one_email.stem)
    unlink = Path.unlink

    def locked_original(path, missing_ok=False):
        if path.suffix == ".eml":
            raise PermissionError(13, "Permission denied")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked_original)
    capsys.readouterr()
    assert run_cli(prune, monkeypatch, "--vault", str(vault), "apply", "--status", "pending", "--json") == 1
    report = json.loads(capsys.readouterr().out)
    assert len(report["failed"]) == 1 and report["failed"][0].endswith("Permission denied")
    assert report["files"] == 3 and report["index_entries"] == 2  # attachment, extracted text, note
    assert email_notes(vault) == [] and len(raw_files(vault, "msg")) == 1
    assert json.loads((vault / "raw" / ".email-index.json").read_text(encoding="utf-8")) == {"by_msgid": {}, "by_sha": {}}
    assert "(5 Oct, Priya (deleted))" in page.read_text(encoding="utf-8")
    log = (vault / "raw" / ".prune-log.jsonl").read_text(encoding="utf-8")
    assert '"files": 3' in log


def test_embedded_email_alone_marks_parent_bullet(prune, msg_to_md, vault, monkeypatch, capsys):
    inner = EmailMessage()
    inner["Subject"], inner["From"], inner["Message-ID"] = "Rate card", "Tom Reid <tom@x.com>", "<inner@x.com>"
    inner["Date"] = "Fri, 02 Oct 2026 10:00:00 +0000"
    inner.set_content("See the card.")
    convert(msg_to_md, vault, monkeypatch, subject="FW: Rate card", embedded=inner)
    parent = next(path for path in email_notes(vault) if "Shah" in path.stem)
    child = next(path for path in email_notes(vault) if "Tom Reid" in path.stem)

    report = prune_json(prune, vault, monkeypatch, capsys, "apply", child.relative_to(vault).as_posix())
    assert report["raw_notes"] == [parent.relative_to(vault).as_posix()]
    text = parent.read_text(encoding="utf-8")
    assert f"- Embedded email: {child.stem} · removed (pruned " in text and "[[raw/emails/" not in text


def test_note_named_without_md_may_contain_dots(prune, msg_to_md, vault, monkeypatch, capsys):
    convert(msg_to_md, vault, monkeypatch, subject="Q3 v1.2 budget")
    note = email_notes(vault)[0]
    stem_path = note.relative_to(vault).as_posix().removesuffix(".md")
    assert len(prune_json(prune, vault, monkeypatch, capsys, "preview", stem_path)["notes"]) == 1
