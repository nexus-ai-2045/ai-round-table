"""ledger.py (D12/D13) の振る舞い固定。

守る性質:

- D13: root は必ず git 管理下になる。既存 repo があれば再利用し、ネスト repo を作らない
- D12: dispatcher 所有ファイルへの外部書込は次の操作で fail-closed に検知される。
  scope 外 (席が正当に書く scratch 等) は警報にしない (狼少年防止)
- 機械 commit は pathspec 限定 + dispatcher 名義 (人間の commit と判別可能)
"""
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from roundtable import ledger
from roundtable.ledger import LedgerDirtyError


@pytest.fixture
def iso_path():
    """repo の外に隔離した一時ディレクトリ。

    この repo は pytest の basetemp を repo 内 `.pytest-tmp/` に置くため、
    通常の tmp_path は **常に ai-roundtable repo の中**にある。そのままだと
    ensure_git_root が enclosing repo (= 本物の repo) を掴み、テストの commit が
    実 repo に向かう。「repo 外」を検証するテストは実 %TEMP% に出る必要がある
    (%TEMP% が repo でないことは前提。崩れたら下の assert で即死する)。
    """
    d = Path(tempfile.mkdtemp(prefix="rt-ledger-"))
    assert ledger.work_tree_root(d) is None, "システム TEMP が git repo 内にある"
    yield d

    def _force_rw(fn, p, exc):  # .git 内の read-only ファイル対策 (Windows)
        os.chmod(p, stat.S_IWRITE)
        fn(p)

    shutil.rmtree(d, onerror=_force_rw)


def _state(root: Path, name: str = "journal.json", text: str = "{}") -> Path:
    p = root / "minutes" / "t1" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _setup(iso_path: Path) -> tuple[Path, Path]:
    """init + 状態ファイル 1 個 commit 済みの root を作る。"""
    root = iso_path / "rt"
    root.mkdir()
    ledger.ensure_git_root(root)
    p = _state(root)
    ledger.commit(root, [p], "minutes(t1): create")
    return root, p


# --- D13: git 管理の保証 ---


def test_init_when_outside_any_repo(iso_path):
    root = iso_path / "rt"
    root.mkdir()
    assert ledger.work_tree_root(root) is None
    top = ledger.ensure_git_root(root)
    assert top == root
    assert (root / ".git").exists()


def test_reuses_enclosing_repo_without_nesting(iso_path):
    outer = iso_path / "repo"
    outer.mkdir()
    ledger.ensure_git_root(outer)  # ここで init
    inner = outer / "sub" / "minutes-root"
    inner.mkdir(parents=True)
    top = ledger.ensure_git_root(inner)
    assert top == outer  # 既存 repo を使う
    assert not (inner / ".git").exists()  # ネスト repo を作らない


def test_idempotent(iso_path):
    root = iso_path / "rt"
    root.mkdir()
    assert ledger.ensure_git_root(root) == ledger.ensure_git_root(root)


# --- D12: 外部書込の検知 (fail-closed) ---


def test_clean_after_commit_passes(iso_path):
    root, p = _setup(iso_path)
    ledger.require_clean(root, [p])  # 例外なし


def test_modification_detected_with_diff_material(iso_path):
    root, p = _setup(iso_path)
    p.write_text('{"tampered": true}', encoding="utf-8")
    with pytest.raises(LedgerDirtyError) as got:
        ledger.require_clean(root, [p])
    msg = str(got.value)
    assert "journal.json" in msg  # どのファイルかが CEO に見える
    assert "checkout" in msg  # Repair Path を案内する


def test_deletion_detected(iso_path):
    root, p = _setup(iso_path)
    p.unlink()
    with pytest.raises(LedgerDirtyError):
        ledger.require_clean(root, [p])


def test_rogue_untracked_state_file_detected(iso_path):
    """席が pathspec 内に勝手にファイルを作った場合も dirty。"""
    root, p = _setup(iso_path)
    rogue = p.parent / "seats.json"
    rogue.write_text("{}", encoding="utf-8")
    with pytest.raises(LedgerDirtyError):
        ledger.require_clean(root, [p, rogue])


def test_out_of_scope_writes_are_not_alarms(iso_path):
    """scratch への席の正当な書込で警報を出さない (狼少年防止)。"""
    root, p = _setup(iso_path)
    scratch = p.parent / "scratch" / "abc.json"
    scratch.parent.mkdir()
    scratch.write_text('{"opinion": "x"}', encoding="utf-8")
    ledger.require_clean(root, [p])  # scope は状態ファイルのみ → clean


def test_empty_paths_is_noop(iso_path):
    root, _ = _setup(iso_path)
    ledger.require_clean(root, [])


# --- 機械 commit の性質 ---


def test_commit_is_pathspec_limited(iso_path):
    root, p = _setup(iso_path)
    other = _state(root, "seats.json")
    p.write_text('{"round": 2}', encoding="utf-8")
    ledger.commit(root, [p], "minutes(t1): merge")
    # scope 外の seats.json は commit されず dirty のまま
    with pytest.raises(LedgerDirtyError):
        ledger.require_clean(root, [other])
    ledger.require_clean(root, [p])  # scope 内は clean


def test_commit_identity_is_machine(iso_path):
    root, p = _setup(iso_path)
    r = ledger._git(root, "log", "-1", "--format=%an")
    assert r.stdout.strip() == "roundtable-dispatcher"


def test_commit_without_changes_is_noop(iso_path):
    root, p = _setup(iso_path)
    before = ledger._git(root, "rev-parse", "HEAD").stdout
    ledger.commit(root, [p], "minutes(t1): no-op")
    after = ledger._git(root, "rev-parse", "HEAD").stdout
    assert before == after  # 空 commit を作らない


def test_tamper_then_commit_shows_in_history(iso_path):
    """検知 → CEO が正当と裁定して commit → 履歴に残る (Repair Path の実走)。"""
    root, p = _setup(iso_path)
    p.write_text('{"edited": "by CEO"}', encoding="utf-8")
    with pytest.raises(LedgerDirtyError):
        ledger.require_clean(root, [p])
    ledger.commit(root, [p], "minutes(t1): CEO 裁定による手動修正")
    ledger.require_clean(root, [p])  # 裁定後は clean に戻る
    log = ledger._git(root, "log", "--oneline").stdout
    assert "CEO" in log  # 裁定の痕跡が履歴に残る
