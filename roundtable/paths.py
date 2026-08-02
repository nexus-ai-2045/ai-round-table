"""topic ディレクトリ規約の一元管理。

レイアウト (DESIGN v6 §3):
    <root>/minutes/<slug>/
        minutes.md      議事録 (blackboard)
        journal.json    invocation 状態機械
        scratch/        参加者の隔離出力 (invocation UUID 名の JSON)
        snapshot/       参加者に渡す読み取り用スナップショット
"""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TopicPaths:
    root: Path
    minutes: Path
    journal: Path
    scratch: Path
    snapshot: Path


def topic_dir(root: Path, slug: str) -> Path:
    return root / "minutes" / slug


def ensure_topic(root: Path, slug: str) -> TopicPaths:
    d = topic_dir(root, slug)
    scratch = d / "scratch"
    snapshot = d / "snapshot"
    scratch.mkdir(parents=True, exist_ok=True)
    snapshot.mkdir(parents=True, exist_ok=True)
    return TopicPaths(d, d / "minutes.md", d / "journal.json", scratch, snapshot)
