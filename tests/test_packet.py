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
