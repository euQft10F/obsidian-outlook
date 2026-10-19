import json
from pathlib import Path

import pytest

SOURCE = "2026-10-08 1105 Okafor, Daniel - Vendor quote"
RAW_NOTE = f"""---
type: email
subject: "RE: Vendor quote"
date: 2026-10-08T11:05:00
from_email: "daniel.okafor@bright.example"
participants:
  - "me@example.com"
  - "daniel.okafor@bright.example"
  - "lena@bright.example"
message_id: "<A3@msg.example>"
source_file: "raw/msg/2026/10/{SOURCE}.msg"
---

## Message
Quote attached.
"""
OLDER = "2026-10-05 0912 Okafor, Daniel - Vendor quote"
DRAFT = f"""---
type: draft
thread: "[[Vendor quote (2026-10)]]"
to:
  - "Okafor, Daniel <daniel.okafor@bright.example>"
cc:
  - "Lena Fischer <lena@bright.example>"
subject: "RE: Vendor quote"
created: 2026-10-19
sources:
  - "[[{OLDER}|5 Oct, Daniel]]"
  - "[[{SOURCE}|8 Oct, Daniel]]"
---

Hi Daniel,

Thanks - 3 < 4 & <b>bold</b>.
Second line.

Best,
Me

## Notes
- used [[{SOURCE}|8 Oct]]
"""


class FakeMail:
    def __init__(self, html='<html><body lang="EN"><div>-----Original Message-----</div></body></html>'):
        self.HTMLBody, self.To, self.CC, self.Subject = html, "x", "y", "RE: Vendor quote"
        self.calls = []

    @property
    def GetInspector(self):  # noqa: N802
        self.calls.append("GetInspector")
        return object()

    def Reply(self):  # noqa: N802
        self.calls.append("Reply")
        self.reply = FakeMail()
        return self.reply

    def SaveAs(self, path, save_type):  # noqa: N802
        self.calls.append(("SaveAs", save_type))
        Path(path).write_bytes(b"msg")

    def Close(self, mode):  # noqa: N802
        self.calls.append(("Close", mode))

    def Send(self):  # noqa: N802
        raise AssertionError("must never send")

    def Save(self):  # noqa: N802
        raise AssertionError("must never save to the mailbox")


class FakeItems(list):
    def Restrict(self, query):  # noqa: N802
        self.query = query
        return self


class FakeNamespace:
    def __init__(self, shared=True, mailbox=()):
        self.shared, self.items, self.opened = shared, FakeItems(mailbox), []

    def OpenSharedItem(self, path):  # noqa: N802
        self.opened.append(path)
        if not self.shared:
            raise OSError("cannot open")
        self.original = FakeMail()
        return self.original

    def GetDefaultFolder(self, folder_id):  # noqa: N802
        return type("Folder", (), {"Items": self.items})()


class FakeApplication:
    def CreateItem(self, item_type):  # noqa: N802
        assert item_type == 0  # olMailItem
        self.created = FakeMail(html="<html><body></body></html>")
        return self.created


@pytest.fixture
def draft_vault(vault):
    emails = vault / "raw" / "emails" / "2026" / "10"
    emails.mkdir(parents=True)
    (emails / f"{SOURCE}.md").write_text(RAW_NOTE, encoding="utf-8")
    (emails / f"{OLDER}.md").write_text(RAW_NOTE.replace("2026-10-08T", "2026-10-05T"), encoding="utf-8")
    archived = vault / "raw" / "msg" / "2026" / "10" / f"{SOURCE}.msg"
    archived.parent.mkdir(parents=True)
    archived.write_bytes(b"orig")
    (vault / "wiki" / "drafts").mkdir(parents=True)
    (vault / "wiki" / "drafts" / "Draft - Vendor quote (2026-10).md").write_text(DRAFT, encoding="utf-8")
    return vault


def load(draft_to_msg, vault):
    return draft_to_msg.read_draft(vault, draft_to_msg.resolve_draft_path(
        vault, "wiki/drafts/Draft - Vendor quote (2026-10).md"))


def test_read_draft_takes_body_above_notes_and_latest_source(draft_to_msg, draft_vault):
    draft = load(draft_to_msg, draft_vault)
    assert draft.body.startswith("Hi Daniel,") and draft.body.endswith("Me")
    assert "## Notes" not in draft.body
    assert draft.to == ["Okafor, Daniel <daniel.okafor@bright.example>"]
    assert draft.source_meta["date"] == "2026-10-08T11:05:00"


def test_body_html_escapes_markup(draft_to_msg):
    rendered = draft_to_msg.body_html("3 < 4 & <b>x</b>\nnext\n\npara")
    assert "&lt;b&gt;" in rendered and "&amp;" in rendered and "<b>" not in rendered
    assert rendered.count("<p>") == 2 and "<br>" in rendered


@pytest.mark.parametrize("path", ["wiki/threads/Vendor quote (2026-10).md", "wiki/drafts/../threads/x.md",
                                  "raw/emails/x.md", "wiki/drafts/Draft - Vendor quote (2026-10).msg"])
def test_rejects_paths_outside_drafts(draft_to_msg, draft_vault, path):
    with pytest.raises(draft_to_msg.DraftError):
        draft_to_msg.resolve_draft_path(draft_vault, path)


def test_reply_from_archived_msg(draft_to_msg, draft_vault):
    namespace = FakeNamespace()
    result = draft_to_msg.build_reply_msg(draft_vault, load(draft_to_msg, draft_vault), FakeApplication(), namespace)
    reply = namespace.original.reply
    assert result.anchor == "archived-msg" and not result.warnings
    assert namespace.opened[0].endswith(f"{SOURCE}.msg")
    assert reply.To == "Okafor, Daniel <daniel.okafor@bright.example>"
    assert reply.CC == "Lena Fischer <lena@bright.example>"
    assert reply.HTMLBody.index("Hi Daniel") < reply.HTMLBody.index("Original Message")
    assert reply.HTMLBody.startswith('<html><body lang="EN"><div class="email-vault-draft">')
    assert ("SaveAs", draft_to_msg.OL_MSG_UNICODE) in reply.calls
    assert ("Close", draft_to_msg.OL_DISCARD) in reply.calls
    assert ("Close", draft_to_msg.OL_DISCARD) in namespace.original.calls
    assert result.msg == draft_vault / "wiki" / "drafts" / "Draft - Vendor quote (2026-10).msg"
    assert result.msg.read_bytes() == b"msg"


def test_falls_back_to_mailbox_search(draft_to_msg, draft_vault):
    in_mailbox = FakeMail()
    namespace = FakeNamespace(shared=False, mailbox=[in_mailbox])
    result = draft_to_msg.build_reply_msg(draft_vault, load(draft_to_msg, draft_vault), FakeApplication(), namespace)
    assert result.anchor == "mailbox"
    assert "'<A3@msg.example>'" in namespace.items.query
    assert "Reply" in in_mailbox.calls


def test_falls_back_to_new_mail_with_warning(draft_to_msg, draft_vault):
    application = FakeApplication()
    result = draft_to_msg.build_reply_msg(draft_vault, load(draft_to_msg, draft_vault), application,
                                          FakeNamespace(shared=False))
    assert result.anchor == "none"
    assert any("original not found" in warning for warning in result.warnings)
    assert "Hi Daniel" in application.created.HTMLBody


def test_warns_about_recipients_not_on_original(draft_to_msg, draft_vault):
    path = draft_vault / "wiki" / "drafts" / "Draft - Vendor quote (2026-10).md"
    path.write_text(DRAFT.replace("lena@bright.example>", "someone@evil.example>"), encoding="utf-8")
    result = draft_to_msg.build_reply_msg(draft_vault, load(draft_to_msg, draft_vault), FakeApplication(), FakeNamespace())
    assert any("someone@evil.example" in warning for warning in result.warnings)


def test_main_reports_missing_outlook_as_json(draft_to_msg, draft_vault, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(draft_vault))

    def no_outlook():
        raise draft_to_msg.DraftError("Could not start classic Outlook")

    monkeypatch.setattr(draft_to_msg, "connect_to_outlook", no_outlook)
    assert draft_to_msg.main(["wiki/drafts/Draft - Vendor quote (2026-10).md", "--json"]) == 1
    assert "classic Outlook" in json.loads(capsys.readouterr().out)["error"]
