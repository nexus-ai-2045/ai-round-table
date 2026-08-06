"""topic ディレクトリ規約の一元管理。

レイアウト (DESIGN v6 §3):
    <root>/minutes/<slug>/
        minutes.md      議事録 (blackboard)
        journal.json    invocation 状態機械
        scratch/        参加者の隔離出力 (invocation UUID 名の JSON)
        snapshot/       参加者に渡す読み取り用スナップショット

v0.2 で slug 検証を足した (計画 §4 Phase 2 / 軸 D):
dispatcher は topic 配下しか触らない、を入口で担保する。slug は CLI 引数として
外から来るため、`..` や `/` を含む値をそのまま結合すると root の外に書ける。
"""
import re
from dataclasses import dataclass
from pathlib import Path

# 許可する slug: 英小文字・数字で始まり、以降は英小文字・数字・ハイフンのみ。
# `.` を許さないので `..` / `../x` / `a/b` / `a\b` / 絶対パスは構造的に通らない。
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class TopicPaths:
    root: Path
    minutes: Path
    journal: Path
    scratch: Path
    snapshot: Path


def validate_slug(slug: str) -> str:
    """slug を検証してそのまま返す。違反は ValueError (軸 D の detector)。"""
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"invalid topic slug: {slug!r} (期待: ^[a-z0-9][a-z0-9-]*$)"
        )
    return slug


def topic_dir(root: Path, slug: str) -> Path:
    """topic ディレクトリを返す。ここが唯一の path 合成点なので検証もここで行う。"""
    return root / "minutes" / validate_slug(slug)


def ensure_topic(root: Path, slug: str) -> TopicPaths:
    """topic ディレクトリ規約を作る。slug 検証は topic_dir 経由で必ず通る。"""
    d = topic_dir(root, slug)
    scratch = d / "scratch"
    snapshot = d / "snapshot"
    scratch.mkdir(parents=True, exist_ok=True)
    snapshot.mkdir(parents=True, exist_ok=True)
    return TopicPaths(d, d / "minutes.md", d / "journal.json", scratch, snapshot)
