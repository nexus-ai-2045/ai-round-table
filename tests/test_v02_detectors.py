"""v0.2 Phase 2 detectors: human_actions / transitions / slug / failure stats."""
import json

import pytest

from roundtable.cli import main
from roundtable.journal import Journal
from roundtable.paths import ensure_topic


def test_slug_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError, match="slug"):
        ensure_topic(tmp_path, "..\\evil")
    with pytest.raises(ValueError, match="slug"):
        ensure_topic(tmp_path, "Bad_Slug")
    tp = ensure_topic(tmp_path, "good-slug-1")
    assert tp.minutes.parent.name == "good-slug-1"


def test_set_state_rejects_reverse_from_merged(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    j.set_state(inv, "merged")
    with pytest.raises(ValueError, match="transition"):
        j.set_state(inv, "prepared")


def test_set_state_rejects_reverse_from_failed(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    j.set_state(inv, "failed", "timeout")
    with pytest.raises(ValueError, match="transition"):
        j.set_state(inv, "merged")


def test_human_actions_recorded_by_cli(tmp_path, capsys):
    main([
        "new-topic", "t1", "--topic", "X", "--participants", "codex",
        "--background", "背景メモ", "--root", str(tmp_path),
    ])
    main([
        "dispatch", "t1", "--participant", "codex", "--no-clipboard",
        "--timeout", "0.1", "--root", str(tmp_path),
    ])
    main(["close", "t1", "--verdict", "見送り", "--root", str(tmp_path)])
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    assert j.human_action_count() >= 3
    actions = {e["action"] for e in j.data["human_actions"]}
    assert "new-topic" in actions
    assert "dispatch" in actions
    assert "close" in actions
    text = tp.minutes.read_text(encoding="utf-8")
    assert "背景メモ" in text


def test_status_shows_failure_stats_and_human_actions(tmp_path, capsys):
    main([
        "new-topic", "t1", "--topic", "X", "--participants", "codex",
        "--root", str(tmp_path),
    ])
    main([
        "dispatch", "t1", "--participant", "codex", "--no-clipboard",
        "--timeout", "0.1", "--root", str(tmp_path),
    ])
    capsys.readouterr()
    main(["status", "t1", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "human_actions:" in out
    assert "failure_stats:" in out
    assert "timeout" in out


def test_last_result_written_on_dispatch_failure(tmp_path):
    main([
        "new-topic", "t1", "--topic", "X", "--participants", "codex",
        "--root", str(tmp_path),
    ])
    rc = main([
        "dispatch", "t1", "--participant", "codex", "--no-clipboard",
        "--timeout", "0.1", "--root", str(tmp_path),
    ])
    assert rc == 1
    result = json.loads((tmp_path / "minutes" / "t1" / "last-result.json").read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert result["reason"] == "timeout"
    assert result["exit_code"] == 1
