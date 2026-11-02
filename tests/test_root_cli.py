import subprocess
import pytest

from roundtable import ledger
from roundtable.cli import main


def test_nested_data_root_is_rejected_before_staging(tmp_path, capsys):
    parent = tmp_path / "parent"
    parent.mkdir()
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    root = parent / "data"
    result = main(["new-topic", "example", "--topic", "example",
                   "--participants", "claude", "--root", str(root)])
    assert result == 2
    assert "[ledger]" in capsys.readouterr().err
    assert not (root / "minutes/example/minutes.md").exists()
    assert subprocess.check_output(
        ["git", "diff", "--cached", "--name-only"], cwd=parent) == b""


def test_legal_minutes_slug_can_store_requests(tmp_path):
    assert main(["new-topic", "minutes", "--topic", "example", "--participants",
                 "claude", "--root", str(tmp_path)]) == 0
    assert main(["dispatch", "minutes", "--participant", "claude", "--tier", "3",
                 "--async", "--no-clipboard", "--root", str(tmp_path)]) == 0


@pytest.mark.parametrize("directory", ["requests", "deliveries", "followups", "snapshot", "scratch"])
def test_minutes_slug_supports_state_subdirectories(tmp_path, directory):
    root = ledger.ensure_git_root(tmp_path)
    path = root / "minutes" / "minutes" / directory / "state.json"
    path.parent.mkdir(parents=True)
    ledger.write_state(path, "{}", "state round trip")
    assert ledger.read_state(path) == b"{}"
