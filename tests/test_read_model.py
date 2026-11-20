import json
import socket
import subprocess
from datetime import datetime

import pytest

from roundtable.read_model import export_topic


def fixture_topic(root):
    directory = root / "minutes" / "selected"
    directory.mkdir(parents=True)
    (directory / "seats.json").write_text(json.dumps({
        "rt/selected/codex": {"participant": "codex", "thread_ref": "codex-id", "thread_name": "Codex"},
        "rt/selected/cc": {"participant": "cc", "thread_ref": {"session_id": "claude-id"}},
    }))
    (directory / "journal.json").write_text(json.dumps({
        "round": 1, "invocations": {"abc123": {"participant": "cc", "round": 1, "state": "delivered"}},
    }))
    (directory / "minutes.md").write_text("# 議題\n既存の議事録\n", encoding="utf-8")
    (directory / "deliveries").mkdir()
    (directory / "deliveries" / "abc123.json").write_text(json.dumps({
        "state": "submitted", "model_receipt_verified": False,
        "request_text": "EXPORT MUST NOT INCLUDE THIS",
    }))
    return directory


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def test_nonexistent_root_is_not_created(tmp_path):
    root = tmp_path / "absent"
    result = export_topic(root, "selected")
    assert result["status"] == "unknown"
    assert not root.exists()
    assert list(tmp_path.iterdir()) == []


def test_valid_projection_preserves_source_and_never_sends(tmp_path, monkeypatch):
    fixture_topic(tmp_path)
    before = snapshot(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("export attempted mutation or external operation")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    from roundtable import paths, journal, relay
    monkeypatch.setattr(paths, "ensure_topic", forbidden)
    monkeypatch.setattr(journal.Journal, "load", forbidden)
    monkeypatch.setattr(relay, "load_seats", forbidden)
    result = export_topic(tmp_path, "selected")
    assert result["status"] == "available"
    assert result["schema_version"] == 1
    assert datetime.fromisoformat(result["observed_at"]).utcoffset().total_seconds() == 0
    seats = {seat["provider"]: seat for seat in result["seats"]}
    assert seats["claude"]["conversation_id"] == {"session_id": "claude-id"}
    assert seats["codex"]["state"] == "unknown"
    assert result["invocations"][0]["state"] == "delivered"
    assert result["invocations"][0]["receipt"] == {"state": "submitted", "model_receipt_verified": False}
    assert "EXPORT MUST NOT INCLUDE THIS" not in json.dumps(result)
    assert "既存の議事録" in result["minutes"]
    assert snapshot(tmp_path) == before
    assert not (tmp_path / ".locks").exists()


@pytest.mark.parametrize("name,content", [
    ("seats.json", "[]"), ("seats.json", '{"seat": null}'),
    ("journal.json", "{"), ("journal.json", '{"round":1}'),
    ("journal.json", '{"round":1,"invocations":{"a":{"state":"future"}}}'),
    ("minutes.md", None),
])
def test_missing_or_malformed_is_unknown(tmp_path, name, content):
    directory = fixture_topic(tmp_path)
    path = directory / name
    if content is None:
        path.unlink()
    else:
        path.write_text(content)
    before = snapshot(tmp_path)
    result = export_topic(tmp_path, "selected")
    assert result["status"] == "unknown"
    assert result["reason"]
    assert result["seats"] == []
    assert snapshot(tmp_path) == before


def test_symlink_is_not_followed(tmp_path):
    directory = fixture_topic(tmp_path)
    path = directory / "minutes.md"
    path.unlink()
    try:
        path.symlink_to(tmp_path / "private-conversation")
    except OSError:
        pytest.skip("symlink creation unavailable")
    result = export_topic(tmp_path, "selected")
    assert result["status"] == "unknown"
    assert result["reason"] == "symlink"


def test_invalid_slug_does_not_escape(tmp_path):
    assert export_topic(tmp_path, "../outside")["status"] == "unknown"
    assert list(tmp_path.iterdir()) == []


def test_claude_handoff_without_seats_and_codex_ack(tmp_path):
    directory = fixture_topic(tmp_path)
    (directory / "seats.json").unlink()
    journal = json.loads((directory / "journal.json").read_text())
    journal["invocations"]["codex1"] = {
        "participant": "codex", "round": 1, "state": "delivered",
        "submission_receipt": {"provider": "codex", "thread_id": "thread", "turn_id": "turn", "status": "accepted"},
    }
    (directory / "journal.json").write_text(json.dumps(journal))
    delivery = directory / "deliveries" / "abc123.json"
    delivery.write_text(json.dumps({"state": "submitted", "claude_session": "selected-claude-session"}))
    before = snapshot(tmp_path)
    result = export_topic(tmp_path, "selected")
    assert result["status"] == "available"
    assert result["seats"][0]["provider"] == "claude"
    assert result["seats"][0]["conversation_id"] == "selected-claude-session"
    ack = next(rec for rec in result["invocations"] if rec["id"] == "codex1")
    assert ack["submission_receipt"]["status"] == "accepted"
    assert ack["state"] == "delivered"
    assert snapshot(tmp_path) == before
