"""topic ディレクトリ規約の一元管理。

レイアウト (DESIGN v6 §3 + D12/D13):
    <root>/minutes/<slug>/
        minutes.md      議事録 (blackboard) — git が改ざん証跡 (D12)
        journal.json    invocation 状態機械
        scratch/        参加者の隔離出力 (invocation UUID 名の JSON)
        snapshot/       参加者に渡す読み取り用スナップショット
        last-result.json  直近 CLI 結果 (パイプで exit code が消えても機械確認可)
        seats.json      席メタ (tier / thread_ref)
    <root>/.locks/<slug>/
        journal.json.lock 等 — read-modify-write の排他ロック (gitignore 対象)

旧 `<root>/.integrity/<slug>/` (hash 証跡) は D12 で廃止。証跡は git そのもの:
dispatcher の書込は ledger.commit で履歴に残り、外部の書込は次操作の
require_clean が dirty として検知する。lock だけが残るのは、並行 dispatch の
journal 消失事故 (2026-08-07) の再発防止が改ざん検知とは別問題だから。
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

    def lock(self, name: str) -> Path:
        """`name` を read-modify-write する間だけ握るロックファイル。

        議題ディレクトリの外 (`<root>/.locks/<slug>/`) に置くのは、topic 配下に
        置くと git の履歴・status に混ざり、証跡 (D12) にノイズが入るため。
        lock は排他の道具であって記録ではない。
        """
        return self.root.parent.parent / ".locks" / self.root.name / f"{name}.lock"


def topic_dir(root: Path, slug: str) -> Path:
    """topic ディレクトリを返す。**ここが唯一の path 合成点なので検証もここで行う**。

    ensure_topic 側だけで検証すると、topic_dir を直接使う経路 (将来の CLI・
    ツール) が検証を素通りして root 外を指せる。合成と検証を同じ場所に置く。
    """
    return root / "minutes" / validate_slug(slug)


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
    """topic ディレクトリを用意し、root の git 管理を保証する (D13)。

    git 保証をここに置くのは topic_dir の検証と同じ理由 — 全経路が通る合成点で
    行わないと、「git 外に議事録が作られて検知 (D12) が静かに消える」経路が残る。
    """
    from . import ledger  # 循環 import 回避 (ledger は paths を知らない)

    slug = validate_slug(slug)
    d = topic_dir(root, slug)
    scratch = d / "scratch"
    snapshot = d / "snapshot"
    scratch.mkdir(parents=True, exist_ok=True)
    snapshot.mkdir(parents=True, exist_ok=True)
    ledger.ensure_git_root(root)
    return TopicPaths(d, d / "minutes.md", d / "journal.json", scratch, snapshot)
