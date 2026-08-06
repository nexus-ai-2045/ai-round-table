from pathlib import Path

import pytest

from roundtable.paths import ensure_topic, topic_dir, validate_slug


def test_ensure_topic_creates_layout(tmp_path: Path):
    tp = ensure_topic(tmp_path, "2026-07-27-test")
    assert tp.minutes == tmp_path / "minutes" / "2026-07-27-test" / "minutes.md"
    assert tp.scratch.is_dir()
    assert tp.snapshot.is_dir()
    assert tp.journal.parent.is_dir()


# --- 軸 D: slug 検証 (v0.2 detector) ---

# 「dispatcher は topic 配下しか触らない」を破りうる入力。
TRAVERSAL_SLUGS = [
    "..",
    "../evil",
    "../../etc",
    "a/../../b",
    "a/b",
    "a\\b",
    "..\\evil",
    "/abs",
    "C:/abs",
    "\\\\server\\share",
]

MALFORMED_SLUGS = [
    "",
    ".",
    ".hidden",
    "a.b",
    "-leading-hyphen",
    "UPPER",
    "Mixed-Case",
    "with space",
    "日本語",
    "under_score",
    "trailing\n",
    "nul\x00byte",
]


@pytest.mark.parametrize("slug", TRAVERSAL_SLUGS)
def test_traversal_slug_rejected(tmp_path: Path, slug: str):
    """パストラバーサルは ensure_topic / topic_dir の両方で弾かれる。"""
    with pytest.raises(ValueError):
        topic_dir(tmp_path, slug)
    with pytest.raises(ValueError):
        ensure_topic(tmp_path, slug)


@pytest.mark.parametrize("slug", MALFORMED_SLUGS)
def test_malformed_slug_rejected(tmp_path: Path, slug: str):
    with pytest.raises(ValueError):
        ensure_topic(tmp_path, slug)


@pytest.mark.parametrize("slug", TRAVERSAL_SLUGS + MALFORMED_SLUGS)
def test_rejected_slug_creates_nothing(tmp_path: Path, slug: str):
    """拒否時にディレクトリを作らない (弾く前に mkdir していたら意味がない)。"""
    with pytest.raises(ValueError):
        ensure_topic(tmp_path, slug)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("slug", ["t1", "a", "0", "2026-08-05-v02", "a-b-c"])
def test_valid_slug_accepted(tmp_path: Path, slug: str):
    assert validate_slug(slug) == slug
    tp = ensure_topic(tmp_path, slug)
    # 生成される全パスが root 配下に収まっている
    root = (tmp_path / "minutes").resolve()
    for p in (tp.root, tp.minutes, tp.journal, tp.scratch, tp.snapshot):
        assert root in p.resolve().parents or p.resolve() == root


def test_non_str_slug_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        ensure_topic(tmp_path, None)  # type: ignore[arg-type]
