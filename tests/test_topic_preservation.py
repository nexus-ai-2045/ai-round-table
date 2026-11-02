import pytest

from roundtable import minutes
from roundtable.journal import Journal
from roundtable.paths import ensure_topic
from roundtable.cli import main


def test_duplicate_topic_preserves_minutes_and_invocations(tmp_path):
    tp = ensure_topic(tmp_path, "existing")
    minutes.create(tp, "original", ["claude"])
    journal = Journal.load(tp)
    invocation = journal.new_invocation("claude", 1)
    before = {path: path.read_bytes() for path in (tp.minutes, tp.journal)}

    with pytest.raises(FileExistsError, match="topic already exists"):
        minutes.create(tp, "replacement", ["codex"])

    assert {path: path.read_bytes() for path in before} == before
    assert invocation in Journal.load(tp).data["invocations"]


def test_existing_journal_without_minutes_is_not_reinitialized(tmp_path):
    tp = ensure_topic(tmp_path, "partial")
    Journal.load(tp).new_invocation("claude", 1)
    before = tp.journal.read_bytes()
    with pytest.raises(FileExistsError, match="topic already exists"):
        minutes.create(tp, "replacement", ["claude"])
    assert tp.journal.read_bytes() == before
    assert not tp.minutes.exists()


def test_cli_duplicate_topic_returns_error_without_changing_data(tmp_path, capsys):
    args = ["new-topic", "existing", "--topic", "original", "--participants",
            "claude", "--root", str(tmp_path)]
    assert main(args) == 0
    tp = ensure_topic(tmp_path, "existing")
    before = {path: path.read_bytes() for path in (tp.minutes, tp.journal)}
    assert main(args) == 2
    assert "既存" in capsys.readouterr().err
    assert {path: path.read_bytes() for path in before} == before
