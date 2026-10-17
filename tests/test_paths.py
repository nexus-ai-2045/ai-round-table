from pathlib import Path

from roundtable.paths import ensure_topic


def test_ensure_topic_creates_layout(tmp_path: Path):
    tp = ensure_topic(tmp_path, "2026-07-27-test")
    assert tp.minutes == tmp_path / "minutes" / "2026-07-27-test" / "minutes.md"
    assert tp.scratch.is_dir()
    assert tp.snapshot.is_dir()
    assert tp.journal.parent.is_dir()
