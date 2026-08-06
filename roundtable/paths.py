"""topic ディレクトリ規約の一元管理。

レイアウト (DESIGN v6 §3):
    <root>/minutes/<slug>/
        minutes.md      議事録 (blackboard)
        journal.json    invocation 状態機械
        scratch/        参加者の隔離出力 (invocation UUID 名の JSON)
        snapshot/       参加者に渡す読み取り用スナップショット
        last-result.json  直近 CLI 結果 (パイプで exit code が消えても機械確認可)
        seats.json      席メタ (tier / thread_ref)
"""
import re
from dataclasses import dataclass
from pathlib import Path

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class TopicPaths:
    root: Path
    minutes: Path
    journal: Path
    scratch: Path
    snapshot: Path

    @property
    def last_result(self) -> Path:
        return self.root / "last-result.json"

    @property
    def seats(self) -> Path:
        return self.root / "seats.json"


def topic_dir(root: Path, slug: str) -> Path:
    return root / "minutes" / slug


def validate_slug(slug: str) -> str:
    """dispatcher が topic 配下以外に触れないための境界 (軸 D detector)。

    許可: 小文字英数字と単一ハイフン区切り。`..` / 絶対パス / 大文字を拒否。
    """
    if not slug or not _SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"invalid slug: {slug!r} (allowed: [a-z0-9]+(?:-[a-z0-9]+)*)"
        )
    return slug


def ensure_topic(root: Path, slug: str) -> TopicPaths:
    slug = validate_slug(slug)
    d = topic_dir(root, slug)
    scratch = d / "scratch"
    snapshot = d / "snapshot"
    scratch.mkdir(parents=True, exist_ok=True)
    snapshot.mkdir(parents=True, exist_ok=True)
    return TopicPaths(d, d / "minutes.md", d / "journal.json", scratch, snapshot)
