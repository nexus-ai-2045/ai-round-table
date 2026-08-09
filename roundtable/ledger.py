"""git を唯一の改ざん証跡として扱う層 (DESIGN v6 D12/D13)。

witness 層 (旧 integrity.py) の置き換え。sha256 の控えを自前で持つ代わりに、

- 書込前: `require_clean` — 対象ファイルの working tree が clean であることを要求。
  dirty = dispatcher 以外の書込 → diff 要約付きで fail-closed に停止 (D7)
- 書込後: `commit` — pathspec 限定で auto-commit。機械 commit は
  author を dispatcher 名義に固定し、人間の commit と判別できるようにする
- 最終証跡: origin へ push した履歴 (席はローカル FS には届いてもリモートには届かない)

**検知者は最終的に人間** (D1)。ここで出す diff は CEO が裁定するための材料であり、
自動修復はしない。

lock (排他) はここでは扱わない。並行 dispatch の journal 消失事故 (2026-08-07) の
再発防止は filelock.py の責務で、改ざん検知とは別問題 (D12)。
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

# 機械 commit の名義。git log --author=roundtable-dispatcher で機械分だけ抽出できる。
# gpgsign 無効は保険: グローバル設定で署名が有効だと auto-commit が署名待ちで
# 止まる/失敗する。機械 commit の真正性は名義でなく push 先履歴で担保する (D12)。
_IDENTITY = [
    "-c", "user.name=roundtable-dispatcher",
    "-c", "user.email=dispatcher@ai-round-table.local",
    "-c", "commit.gpgsign=false",
]


class LedgerError(Exception):
    """git 証跡層の失敗。"""


class LedgerDirtyError(LedgerError):
    """dispatcher 以外の書込を検知した (fail-closed)。

    正当な変更なら CEO が commit して再実行、不当なら checkout で復元する
    (Repair Path)。このエラーを握り潰して続行してはならない。
    """


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # --no-optional-locks: 読み取り系 (status 等) が index を触って書き手と
    # 競合しないようにする。書き込み系の index.lock 競合は _git_locked が持つ。
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(cwd), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _git_locked(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """index.lock 競合を backoff 再試行する git 呼び出し (add / commit 用)。

    journal / seats / minutes は**別々の FileLock** で守られているため、同一 repo への
    commit は並行しうる。git の index.lock は排他そのものなので、負けた側は
    「一時的に取れなかった」だけ — 失敗にせず待って撃ち直す (filelock.py と同じ思想)。
    """
    wait = 0.05
    for _ in range(20):
        r = _git(cwd, *args)
        if r.returncode == 0 or "index.lock" not in (r.stderr or ""):
            return r
        time.sleep(wait)
        wait = min(wait * 2, 0.5)
    return r


def work_tree_root(path: Path) -> Path | None:
    """`path` を含む git work tree の最上位。無ければ None。

    入口で resolve する: 相対パス (`--root .` 等) をそのまま git に渡すと、
    `-C <top>` 以降の pathspec が **top 基準で再解決**されて別の場所を指す
    (test_cli_cwd_is_absolute_for_relative_root で実測)。ledger の公開関数は
    すべて絶対パスに正規化してから git を呼ぶ。
    """
    r = _git(Path(path).resolve(), "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        return None
    return Path(r.stdout.strip())


def ensure_git_root(root: Path) -> Path:
    """root が git 管理下であることを保証する (D13)。

    既存の work tree に入っていればそれを使う (実運用: 本 repo の minutes/)。
    どの repo にも属さない場合だけ init する (テスト・独立運用)。
    ネスト repo を作らないのは、証跡が二重管理になり「どちらの履歴が正か」で
    witness 層と同じ曖昧さが再発するため。

    例外: enclosing repo で **root 自体が gitignore されている**場合は、その repo に
    とって存在しないのと同じ (add できず証跡にならない)。この場合だけ root に
    独立 repo を init する。実利: この repo は pytest の basetemp を repo 内
    `.pytest-tmp/` (ignore 済み) に置くため、テストの議題 root が本物の repo を
    enclosing として掴み、**テストの auto-commit が実 repo に向かう**。ignore 判定が
    その事故を構造的に塞ぐ (Phase 1 で実際に踏みかけた)。
    """
    root = Path(root).resolve()
    top = work_tree_root(root)
    if top == root:
        return root
    if top is not None and not _ignored_by(top, root):
        return top
    r = _git(root, "init", "-q")
    if r.returncode != 0:
        raise LedgerError(f"git init 失敗: {root}: {r.stderr.strip()}")
    # 自前 init した repo だけ高速化 config を入れる (enclosing repo には触らない)。
    # 保存 1 回 = commit 1 回 (D12) なので、Windows での 1 呼び出しコストが
    # そのまま dispatch のロック保持時間になる。
    for k, v in (
        ("core.autocrlf", "false"),   # 改行変換は clean 検査の偽陽性源でもある
        ("core.fscache", "true"),
        ("gc.auto", "0"),             # auto-gc の突発停止を避ける (縮退時は手動 gc)
    ):
        _git(root, "config", k, v)
    return root


def _ignored_by(top: Path, path: Path) -> bool:
    """`path` が repo `top` の gitignore 対象か。"""
    return _git(top, "check-ignore", "-q", "--", str(Path(path).resolve())).returncode == 0


def require_root(path: Path) -> Path:
    """path が git 管理下であることを要求する (読み書き経路の D13 detector)。

    ensure_git_root と違い **init しない**。議事録が git 外に置かれたまま操作が
    進むと検知が静かに消えるので、ここで fail-closed に止める。
    """
    top = work_tree_root(path)
    if top is None:
        raise LedgerError(
            f"議事録 root が git 管理下にない (D13): {path}. "
            "new-topic (ensure_topic) を先に通すこと"
        )
    return top


def require_clean(root: Path, paths: list[Path]) -> None:
    """`paths` が HEAD/index と一致していることを要求する。

    呼び出し側の責務: pathspec は **dispatcher が所有するファイルだけ** に絞ること
    (minutes.md / journal.json / seats.json)。席が正当に書く scratch/ を含めると
    正常運転が dirty 扱いになり、狼少年化して本物の警報が無視される
    (base_hash 設計の失敗 (2026-08-07) と同型)。
    """
    if not paths:
        return
    spec = [str(Path(p).resolve()) for p in paths]
    r = _git(root, "status", "--porcelain", "--", *spec)
    if r.returncode != 0:
        raise LedgerError(f"git status 失敗: {r.stderr.strip()}")
    if not r.stdout.strip():
        return
    diff = _git(root, "diff", "--stat", "HEAD", "--", *spec)
    raise LedgerDirtyError(
        "dispatcher 以外の書込を検知した (fail-closed で停止):\n"
        f"{r.stdout.rstrip()}\n"
        f"{diff.stdout.rstrip() or '(diff --stat なし: 未追跡または削除)'}\n"
        "正当な変更なら commit して再実行、不当なら git checkout で復元する。"
    )


def commit(root: Path, paths: list[Path], message: str) -> None:
    """pathspec 限定で auto-commit する。

    `git add -A` は使わない (CLAUDE.md Git 規約)。変更が無ければ何もしない
    (同一内容の再書込は正常系)。
    """
    spec = [str(Path(p).resolve()) for p in paths]
    # 経路 1 (定常): tracked なファイルの変更は pathspec 付き commit 一発で拾える
    # (add 不要)。journal.save は FileLock を握ったままここへ来るので、保持時間 =
    # git 呼び出し回数。1 回で済む形を先に試す (並行 dispatch の LockTimeout 抑制)。
    r = _git_locked(root, *_IDENTITY, "commit", "-q", "-m", message, "--", *spec)
    if r.returncode == 0:
        return
    # 経路 2 (初回 / 競合): untracked (初回書込) は commit が pathspec を解決できない。
    # add してから commit し直す。
    r2 = _git_locked(root, "add", "--", *spec)
    if r2.returncode != 0:
        raise LedgerError(f"git add 失敗: {r2.stderr.strip()}")
    staged = _git(root, "diff", "--cached", "--quiet", "--", *spec)
    if staged.returncode == 0:
        return  # 差分なし (同一内容の再書込 / 並行 commit が先に拾った) — 正常系
    r3 = _git_locked(root, *_IDENTITY, "commit", "-q", "-m", message, "--", *spec)
    if r3.returncode != 0:
        check = _git(root, "status", "--porcelain", "--", *spec)
        if check.returncode == 0 and not check.stdout.strip():
            return  # 履歴に残ってさえいれば失敗ではない
        raise LedgerError(f"git commit 失敗: {r3.stderr.strip() or r3.stdout.strip()}")


def read_state(path: Path) -> bytes | None:
    """状態ファイルを clean 検査つきで読む (呼び出し側のロック配下で呼ぶこと)。

    旧 `integrity.verify_and_read` の後継。あちらと違い読取は純粋 (証跡の巻き戻し
    書込が無い) ので、reader が writer の途中状態を壊す経路 (レビュー H1/H2) は
    構造ごと消えている。ロックが要るのは read-modify-write の不可分性のためだけ。
    """
    path = Path(path).resolve()
    root = require_root(path.parent)
    require_clean(root, [path])
    return path.read_bytes() if path.exists() else None


def write_state(path: Path, text: str, message: str) -> None:
    """状態ファイルを atomic に書いて commit する (呼び出し側のロック配下で呼ぶこと)。

    旧 `integrity.write_verified` の後継。書込〜commit の間に落ちると
    「働きかけ中のファイルが dirty のまま残る」が、それは次操作の require_clean が
    diff 付きで報告する — 検知は失われない (防止でなく検知, D7)。
    """
    from .atomicio import atomic_write

    path = Path(path).resolve()
    root = require_root(path.parent)
    atomic_write(path, text)
    commit(root, [path], message)
