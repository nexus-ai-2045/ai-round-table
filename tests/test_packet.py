from pathlib import Path

from roundtable.packet import build
from roundtable.paths import ensure_topic


def test_packet_contains_contract(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    text = build(tp, "codex", "inv123", role_hint="実装視点で")
    assert "inv123" in text
    assert str(tp.snapshot / "minutes.snapshot.md") in text
    assert str(tp.scratch / "inv123.json") in text
    assert "invocation_id" in text and "claims" in text  # 出力契約の説明
    assert "tmp" in text.lower()  # tmp→rename 指示


def test_packet_includes_role_hint_and_participant(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    text = build(tp, "cc", "inv999", role_hint="批判的にレビューして")
    assert "cc" in text
    assert "批判的にレビューして" in text


def test_packet_claims_minimum_one_required(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    text = build(tp, "codex", "inv1")
    assert "1 件以上" in text


def test_packet_relative_root_stays_bound_after_recipient_changes_cwd(tmp_path, monkeypatch):
    dispatcher = tmp_path / "dispatcher"
    recipient = tmp_path / "desktop-worktree"
    dispatcher.mkdir()
    recipient.mkdir()
    monkeypatch.chdir(dispatcher)
    tp = ensure_topic(Path("review-root"), "t1")
    snapshot = (tp.snapshot / "minutes.snapshot.md").resolve()
    output = (tp.scratch / "inv1.json").resolve()
    snapshot.write_text("review snapshot", encoding="utf-8")

    text = build(tp, "cc", "inv1")
    monkeypatch.chdir(recipient)

    assert str(snapshot) in text
    assert str(output) in text
    assert f"{output}.tmp" in text
    delivered_snapshot = Path(text.split("1. 議事録スナップショットを読む: ", 1)[1].splitlines()[0])
    assert delivered_snapshot.is_absolute()
    assert delivered_snapshot.read_text(encoding="utf-8") == "review snapshot"
    assert output.parent.is_dir()
