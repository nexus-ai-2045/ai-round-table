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
from pathlib import Path

# 機械 commit の名義。git log --author=roundtable-dispatcher で機械分だけ抽出できる
_IDENTITY = [
    "-c", "user.name=roundtable-dispatcher",
    "-c", "user.email=dispatcher@ai-round-table.local",
]


class LedgerError(Exception):
    """git 証跡層の失敗。"""


class LedgerDirtyError(LedgerError):
    """dispatcher 以外の書込を検知した (fail-closed)。

    正当な変更なら CEO が commit して再実行、不当なら checkout で復元する
    (Repair Path)。このエラーを握り潰して続行してはならない。
    """


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def work_tree_root(path: Path) -> Path | None:
    """`path` を含む git work tree の最上位。無ければ None。"""
    r = _git(path, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        return None
    return Path(r.stdout.strip())


def ensure_git_root(root: Path) -> Path:
    """root が git 管理下であることを保証する (D13)。

    既存の work tree に入っていればそれを使う (実運用: 本 repo の minutes/)。
    どの repo にも属さない場合だけ init する (テスト・独立運用)。
    ネスト repo を作らないのは、証跡が二重管理になり「どちらの履歴が正か」で
    witness 層と同じ曖昧さが再発するため。
    """
    top = work_tree_root(root)
    if top is not None:
        return top
    r = _git(root, "init", "-q")
    if r.returncode != 0:
        raise LedgerError(f"git init 失敗: {root}: {r.stderr.strip()}")
    return root


def require_clean(root: Path, paths: list[Path]) -> None:
    """`paths` が HEAD/index と一致していることを要求する。

    呼び出し側の責務: pathspec は **dispatcher が所有するファイルだけ** に絞ること
    (minutes.md / journal.json / seats.json)。席が正当に書く scratch/ を含めると
    正常運転が dirty 扱いになり、狼少年化して本物の警報が無視される
    (base_hash 設計の失敗 (2026-08-07) と同型)。
    """
    if not paths:
        return
    spec = [str(p) for p in paths]
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
    spec = [str(p) for p in paths]
    r = _git(root, "add", "--", *spec)
    if r.returncode != 0:
        raise LedgerError(f"git add 失敗: {r.stderr.strip()}")
    staged = _git(root, "diff", "--cached", "--quiet", "--", *spec)
    if staged.returncode == 0:
        return  # 差分なし
    r = _git(root, *_IDENTITY, "commit", "-q", "-m", message, "--", *spec)
    if r.returncode != 0:
        raise LedgerError(f"git commit 失敗: {r.stderr.strip()}")
