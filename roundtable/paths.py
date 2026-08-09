"""topic ディレクトリ規約の一元管理。

レイアウト (DESIGN v6 §3):
    <root>/minutes/<slug>/
        minutes.md      議事録 (blackboard)
        journal.json    invocation 状態機械
        scratch/        参加者の隔離出力 (invocation UUID 名の JSON)
        snapshot/       参加者に渡す読み取り用スナップショット
        last-result.json  直近 CLI 結果 (パイプで exit code が消えても機械確認可)
        seats.json      席メタ (tier / thread_ref)
    <root>/.integrity/<slug>/
        journal.json.sha256 / seats.json.sha256   状態ファイルの hash 証跡
        journal.json.lock   / seats.json.lock     read-modify-write の排他ロック

証跡とロックだけ議題ディレクトリの外に出す (詳細は TopicPaths.integrity)。
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

    @property
    def integrity(self) -> Path:
        """状態ファイル / 議事録の hash 証跡・ロックの置き場。**議題ディレクトリの外**。

        minutes.md / journal.json / seats.json は Tier1 の席の書込範囲
        (sandbox=workspace-write, cwd=議題ディレクトリ) の中にある。証跡を同じ場所に
        置くと、対象を書き換えられる相手が証跡も書き換えられるので照合が成立しない。
        そこで `<root>/.integrity/<slug>/` (議題ディレクトリの 2 階層上) へ逃がす。

        **この前提は席ごとに成否が違う** (2026-08-07 / レビュー H4・H3):

        - **codex 席: 未検証**。「席がこの場所に届かない」は `workspace-write` の
          実効書込範囲が cwd 配下に閉じていることを仮定している。実効範囲を決めるのは
          サーバ側で、repo 内にも `references/` にも**それを示す一次情報が無い**。
          `turn/start` の `sandboxPolicy.writableRoots` は追加リストであって上限指定では
          ない (schema: `writableRoots` の既定は空 + `/tmp` と `$TMPDIR` は専用の除外
          フラグで別管理) ため、送っても範囲を絞れない。閉じるには実測しかない:
          席に `../../.integrity/<slug>/probe` への書き込みを 1 回試させ、拒否される
          ことを spike 記録に残す。
        - **grok 席: 実測で破れている**。Windows で grok の sandbox は一次 docs の
          Platform Support 表に載っておらず、実測でも席は `run_terminal_command` で
          PowerShell を任意実行できた (spike `raw-grok-p3-packet.jsonl`: 絶対パス指定の
          `[System.IO.File]::WriteAllText`)。cwd は境界として機能していないので、
          **grok 席が立った議題では本 module の検知は成立しない**。CLI は dispatch /
          status でその旨を必ず印字する (`cli._warn_if_integrity_detection_does_not_hold`)。

        詳細と手順は docs/review-backlog.md。

        なお path の入れ子関係 (証跡が議題ディレクトリの外にあること) はテストで
        固定してある。前提が崩れているのは **sandbox の実効範囲** の方である。
        """
        return self.root.parent.parent / ".integrity" / self.root.name

    def witness(self, name: str) -> Path:
        """`name` (journal.json 等) の hash 証跡ファイル。"""
        return self.integrity / f"{name}.sha256"

    def lock(self, name: str) -> Path:
        """`name` を read-modify-write する間だけ握るロックファイル。"""
        return self.integrity / f"{name}.lock"


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
    slug = validate_slug(slug)
    d = topic_dir(root, slug)
    scratch = d / "scratch"
    snapshot = d / "snapshot"
    scratch.mkdir(parents=True, exist_ok=True)
    snapshot.mkdir(parents=True, exist_ok=True)
    tp = TopicPaths(d, d / "minutes.md", d / "journal.json", scratch, snapshot)
    tp.integrity.mkdir(parents=True, exist_ok=True)
    return tp
