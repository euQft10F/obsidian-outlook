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


class FakeMail:
    def __init__(self, entry_id, received=NOW, *, klass=43, fail=False, unread=True):
        self.EntryID, self.ReceivedTime, self.SentOn = entry_id, received, received
        self.SenderName, self.Subject, self.Class = "Shah, Priya", "Q3: budget?", klass
        self.UnRead, self.fail = unread, fail

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


def make_exporter(tmp_path, exported=None, dry_run=False):
    return exporter.MailExporter(tmp_path / "out", {} if exported is None else exported, dry_run=dry_run)


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


def test_no_arguments_prints_help(capsys):
    assert exporter.main([]) == 0
    assert "--output-dir" in capsys.readouterr().out


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
