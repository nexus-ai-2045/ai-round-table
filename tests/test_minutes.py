from roundtable.paths import ensure_topic
from roundtable import minutes


def test_create_and_snapshot(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    minutes.create(tp, "議題X", ["codex", "cc"])
    text = tp.minutes.read_text(encoding="utf-8")
    assert "topic: 議題X" in text and "status: open" in text
    snap = minutes.make_snapshot(tp)
    assert snap.read_text(encoding="utf-8") == text
    assert minutes.sha256(snap) == minutes.sha256(tp.minutes)


def test_atomic_write_no_partial(tmp_path):
    p = tmp_path / "f.md"
    minutes.atomic_write(p, "A" * 10000)
    assert p.read_text(encoding="utf-8") == "A" * 10000


def test_parse_participants(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    minutes.create(tp, "X", ["codex", "cc"])
    assert minutes.parse_participants(tp) == ["codex", "cc"]
