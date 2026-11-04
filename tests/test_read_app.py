import json
from pathlib import Path

from roundtable.app import read_safe_export, seat_rows
from roundtable.cli import main


def test_cli_missing_export_does_not_create_root(tmp_path, capsys):
    root = tmp_path / "absent"
    assert main(["export", "test-topic", "--root", str(root)]) == 2
    value = json.loads(capsys.readouterr().out)
    assert value["schema_version"] == 1
    assert value["status"] == "unknown"
    assert not root.exists()


def test_common_seats_keep_native_identity_and_unknown_state():
    snapshot = {"seats": [
        {"provider": "codex", "title": "設計席", "conversation_id": "codex-thread", "state": "unknown"},
        {"provider": "claude", "title": "レビュー席", "conversation_id": "claude-session", "state": "unknown"},
    ]}
    assert seat_rows(snapshot) == [
        ("codex", "設計席", "codex-thread", "unknown"),
        ("claude", "レビュー席", "claude-session", "unknown"),
    ]


def test_safe_export_missing_is_unknown_and_not_created(tmp_path):
    path = tmp_path / "missing" / "safe.json"
    assert read_safe_export(path)["status"] == "unknown"
    assert not path.parent.exists()


def test_safe_export_preserves_frozen_time_and_source_bytes(tmp_path):
    path = tmp_path / "safe.json"
    payload = {"schema_version": 1, "provider": "codex", "status": "ok",
               "observed_at": "2026-11-03T00:00:00Z", "conversations": []}
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert read_safe_export(path) == payload
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_safe_export_rejects_wrong_provider_and_schema(tmp_path):
    path = tmp_path / "safe.json"
    for payload in ({"schema_version": 99, "provider": "codex"},
                    {"schema_version": 1, "provider": "claude"}, []):
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert read_safe_export(path)["status"] == "unknown"


def test_safe_export_rejects_nonregular_input(tmp_path):
    assert read_safe_export(tmp_path)["reason"] == "not-regular-file"


def test_safe_export_rejects_symlink(tmp_path):
    import pytest

    path = tmp_path / "export.json"
    target = tmp_path / "outside.json"
    target.write_text('{"schema_version":1,"provider":"codex"}')
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    assert read_safe_export(path)["reason"] == "symlink"
