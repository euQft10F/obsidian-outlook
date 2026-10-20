import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "export_outlook_msg.py"
spec = importlib.util.spec_from_file_location("export_outlook_msg", SCRIPT)
exporter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = exporter
spec.loader.exec_module(exporter)

NOW = dt.datetime(2026, 10, 13, 9, 0)


class FakeSender:
    def __init__(self, smtp=None):
        self.smtp = smtp

    def GetExchangeUser(self):  # noqa: N802
        if self.smtp is None:
            raise AttributeError("not an Exchange user")
        return SimpleNamespace(PrimarySmtpAddress=self.smtp)


class FakeMail:
    def __init__(self, entry_id, received=NOW, *, klass=43, fail=False, unread=True,
                 name="Shah, Priya", address="", smtp=None, subject="Q3: budget?"):
        self.EntryID, self.ReceivedTime, self.SentOn = entry_id, received, received
        self.SenderName, self.SenderEmailAddress, self.Sender = name, address, FakeSender(smtp)
        self.Subject, self.Class, self.UnRead, self.fail = subject, klass, unread, fail

    def SaveAs(self, path, save_type):  # noqa: N802
        if self.fail:
            raise OSError("IRM protected")
        Path(path).write_bytes(b"msg")


class FakeFolder:
    """Doubles as its own `Items` collection, like the bits of the COM API the exporter uses."""

    def __init__(self, name, mails=(), subfolders=()):
        self.Name, self.Folders, self._mails = name, list(subfolders), list(mails)
        self.Items = self

    def Sort(self, *_):  # noqa: N802
        self._mails.sort(key=lambda mail: mail.ReceivedTime, reverse=True)

    def __iter__(self):
        return iter(self._mails)


def make_exporter(tmp_path, exported=None, dry_run=False, **ignore_lists):
    return exporter.MailExporter(tmp_path / "out", {} if exported is None else exported, dry_run=dry_run,
                                 **ignore_lists)


@pytest.mark.parametrize("raw, expected", [
    ('a/b:c*d?"e"', "a b c d e"),
    ("  Re:  hi ... ", "Re hi"),
    ("x" * 300, "x" * 180),
    ("CON", "_CON"),
    ("///", "untitled"),
    (None, "untitled"),
])
def test_sanitize_filename(raw, expected):
    assert exporter.sanitize_filename(raw) == expected


def test_export_item_saves_skips_and_survives_failures(tmp_path):
    state = {}
    ex = make_exporter(tmp_path, state)
    assert ex.export_item(FakeMail("a"))
    assert not ex.export_item(FakeMail("a"))                    # already exported
    assert not ex.export_item(FakeMail("b", klass=26))          # meeting request
    assert not ex.export_item(FakeMail("c", fail=True))         # SaveAs raises
    assert (len(ex.saved_paths), ex.skipped, ex.failed) == (1, 2, 1)
    assert ex.export_item(FakeMail("a"), force=True)            # forced re-export
    assert set(state) == {"a"}


def test_name_collisions_get_numbered_suffix_and_dry_run_writes_nothing(tmp_path):
    ex = make_exporter(tmp_path, dry_run=True)
    ex.export_item(FakeMail("a"))
    ex.export_item(FakeMail("b"))
    assert sorted(path.name for path in ex.saved_paths) == [
        "2026-10-13 0900 Shah, Priya - Q3 budget (2).msg",
        "2026-10-13 0900 Shah, Priya - Q3 budget.msg",
    ]
    assert not (tmp_path / "out").exists()


def test_export_folder_stops_at_cutoff_and_limit_counts_new_saves(tmp_path):
    mails = [FakeMail(str(i), NOW - dt.timedelta(hours=i)) for i in range(5)]
    folder = FakeFolder("Inbox", mails)
    since = NOW - dt.timedelta(hours=3, minutes=30)  # mails 0..3 are in range
    ex = make_exporter(tmp_path, {"0": {}, "1": {}})  # 0 and 1 already exported
    ex.export_folder(folder, since, limit=1, unread_only=False)
    assert (len(ex.saved_paths), ex.skipped) == (1, 2)  # skipped 0,1; saved 2; stopped by limit


def test_state_round_trip_leaves_no_temp_file(tmp_path):
    path = tmp_path / "raw" / "state.json"
    exporter.save_state(path, {"a": {"file": "x.msg"}})
    assert exporter.load_state(path) == {"a": {"file": "x.msg"}}
    assert not path.with_name("state.json.tmp").exists()
    assert exporter.load_state(tmp_path / "missing.json") == {}


def test_resolve_folder():
    inbox = FakeFolder("Inbox", subfolders=[FakeFolder("Q3")])
    mailbox = FakeFolder("me@x.com", subfolders=[inbox])
    namespace = SimpleNamespace(Folders=[mailbox], GetDefaultFolder=lambda folder_id: inbox)
    assert exporter.resolve_folder(namespace, "inbox/q3").Name == "Q3"
    assert exporter.resolve_folder(namespace, "ME@x.com\\Inbox") is inbox
    with pytest.raises(exporter.ExportError, match="me@x.com"):
        exporter.resolve_folder(namespace, "Nope")


def test_resolve_folders_wildcard_expands_all_subfolders():
    inbox = FakeFolder("Inbox", subfolders=[FakeFolder("A", subfolders=[FakeFolder("A1")]), FakeFolder("B")])
    namespace = SimpleNamespace(Folders=[], GetDefaultFolder=lambda folder_id: inbox)
    assert [path for path, _ in exporter.resolve_folders(namespace, "Inbox\\*")] == [
        "Inbox/A", "Inbox/A/A1", "Inbox/B"]
    assert [path for path, _ in exporter.resolve_folders(namespace, "Inbox/A / *")] == ["Inbox/A/A1"]
    assert exporter.resolve_folders(namespace, "Inbox/B/*") == []
    assert exporter.resolve_folders(namespace, "Inbox") == [("Inbox", inbox)]


def test_folder_given_twice_is_exported_once(tmp_path, monkeypatch, capsys):
    inbox = FakeFolder("Inbox", [FakeMail("a")], subfolders=[FakeFolder("Q3", [FakeMail("b")])])
    namespace = SimpleNamespace(Folders=[], GetDefaultFolder=lambda folder_id: inbox)
    monkeypatch.setattr(exporter, "connect_to_outlook", lambda: (None, namespace))
    argv = ["--days", "36500", "--dry-run", "--vault", str(tmp_path),
            "--folder", "Inbox", "--folder", "Inbox/*", "--folder", "inbox/Q3"]
    assert exporter.main(argv) == 0
    out = capsys.readouterr().out
    assert out.count("would save:") == 2 and "inbox/Q3:" not in out


def test_no_arguments_prints_help(capsys):
    assert exporter.main([]) == 0
    assert "--output-dir" in capsys.readouterr().out


def test_load_ignore_list(tmp_path):
    assert exporter.load_ignore_list(tmp_path / "missing.txt") == []
    path = tmp_path / "ignore.txt"
    path.write_bytes("\ufeffNoReply@X.com\n\n  # comment\n  Newsletter  \n#x\n".encode())
    assert exporter.load_ignore_list(path) == ["noreply@x.com", "newsletter"]


@pytest.mark.parametrize("mail, ignored", [
    (FakeMail("a", subject="Your WEEKLY digest"), True),                     # subject pattern
    (FakeMail("a", name="Jira Notifications"), True),                        # SenderName
    (FakeMail("a", address="NoReply@acme.com"), True),                       # SenderEmailAddress
    (FakeMail("a", address="/O=EXCH/CN=BOT", smtp="noreply@acme.com"), True),  # Exchange SMTP
    (FakeMail("a", address="priya@acme.com"), False),                        # GetExchangeUser raises
])
def test_is_ignored(tmp_path, mail, ignored):
    ex = make_exporter(tmp_path, ignore_from=["noreply@", "jira"], ignore_subject=["weekly digest"])
    assert ex.is_ignored(mail) is ignored


def test_no_ignore_patterns_never_ignore(tmp_path):
    assert not make_exporter(tmp_path).is_ignored(FakeMail("a", address="noreply@acme.com"))


def test_export_item_ignored_is_counted_not_saved_or_remembered(tmp_path):
    state = {}
    ex = make_exporter(tmp_path, state, ignore_subject=["budget"])
    assert not ex.export_item(FakeMail("a"))
    assert (ex.ignored, ex.skipped, len(ex.saved_paths)) == (1, 0, 0)
    assert state == {} and not (tmp_path / "out").exists()
    ex.ignore_subject = []  # list edited later: the same email now exports
    assert ex.export_item(FakeMail("a"))
    assert set(state) == {"a"}


@pytest.mark.parametrize("action", [["--days", "1"], ["--list-folders"]])
def test_missing_explicit_ignore_file_fails_before_outlook(tmp_path, monkeypatch, capsys, action):
    def no_outlook():
        raise AssertionError("must fail before connecting to Outlook")
    monkeypatch.setattr(exporter, "connect_to_outlook", no_outlook)
    argv = [*action, "--vault", str(tmp_path), "--ignore-from-file", str(tmp_path / "nope.txt")]
    assert exporter.main(argv) == 1
    assert "ignore file not found" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["--vault", "x"],                       # no action
    ["--folder", "Inbox"],                  # folder option without --days/--since
    ["--force", "--days", "1"],             # --force is selection-only
    ["--days", "0"],
    ["--selection", "--days", "1"],         # actions are exclusive
])
def test_invalid_arguments_exit_2(argv):
    with pytest.raises(SystemExit) as raised:
        exporter.main(argv)
    assert raised.value.code == 2
