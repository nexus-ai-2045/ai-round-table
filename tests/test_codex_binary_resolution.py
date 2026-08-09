"""codex バイナリ解決の振る舞い固定 (PR #7 レビュー P2-a)。

守る性質:

- **古いと実測できた候補しか無い環境では、待たずに落ちる**。
  0.130 系の app-server は `thread/start` に永久に応答しない (実測)。
  それを掴んだまま返すと、relay は 300s、doctor は 180s 待ってから
  「既に分かっていた非互換」を報告する。解決の時点で落とす。
- **版が読めない候補は落とさない**。`--version` の出力形式が変わっただけの
  可能性があり、古いという積極的な証拠が無い。
- 例外は RelayError の系統。FallbackRelay の既存 except がそのまま拾う。
"""
import os

import pytest

from roundtable.relay import RelayError
from roundtable.relay_codex import (
    MIN_APP_SERVER_VERSION,
    UnsupportedCodexError,
    resolve_codex_binary,
)

OLD = (0, 130, 0)
NEW = (MIN_APP_SERVER_VERSION[0], MIN_APP_SERVER_VERSION[1] + 1, 0)


def _fake_path(monkeypatch, tmp_path, layout: dict[str, tuple[int, ...]]) -> dict[str, str]:
    """`{ディレクトリ名: 版}` から PATH 上の偽 codex を作り、版判定を差し替える。

    PATH は **完全に置き換える**。prepend だと実機の codex が候補に残り、
    「古い版しか無い環境」を再現できない (最初の実行でそれに気付いた)。
    dict の挿入順がそのまま PATH の探索順になる。

    実ファイルが要るのは resolve が os.path.isfile で候補を絞るため。
    版は実行せずに表引きする (`--version` を撃たない)。
    """
    made: dict[str, str] = {}
    dirs: list[str] = []
    for name in layout:
        d = tmp_path / name
        d.mkdir()
        binary = d / "codex.cmd"
        binary.write_text("", encoding="utf-8")
        made[name] = str(binary)
        dirs.append(str(d))
    monkeypatch.setenv("PATH", os.pathsep.join(dirs))
    versions = {p: layout[n] for n, p in made.items()}
    monkeypatch.setattr(
        "roundtable.relay_codex._probe_version", lambda p: versions.get(p, ())
    )
    return made


def test_all_candidates_old_raises_instead_of_returning(monkeypatch, tmp_path):
    """全部古い → 一番マシな古版を返さず、その場で落ちる。"""
    _fake_path(monkeypatch, tmp_path, {"older": (0, 120, 0), "old": OLD})
    with pytest.raises(UnsupportedCodexError) as got:
        resolve_codex_binary()
    assert "0.130.0" in str(got.value)  # どの版が問題かを名指しする
    assert got.value.version == OLD  # 最も新しい古版を報告 (更新目標が分かる)


def test_unsupported_is_relay_error(monkeypatch, tmp_path):
    """RelayError 系統なので FallbackRelay が分岐追加なしで Tier3 へ縮退できる。"""
    _fake_path(monkeypatch, tmp_path, {"old": OLD})
    with pytest.raises(RelayError):
        resolve_codex_binary()


def test_supported_wins_over_old_regardless_of_path_order(monkeypatch, tmp_path):
    """PATH 先頭が古くても、対応版があればそちらを選ぶ (元々の目的を壊さない)。"""
    made = _fake_path(monkeypatch, tmp_path, {"old": OLD, "new": NEW})
    assert resolve_codex_binary() == made["new"]


def test_unreadable_version_is_not_rejected(monkeypatch, tmp_path):
    """版が読めない候補は「古い」証拠が無い。落とさず試させる。"""
    made = _fake_path(monkeypatch, tmp_path, {"old": OLD, "mystery": ()})
    assert resolve_codex_binary() == made["mystery"]


def test_no_candidates_does_not_raise(monkeypatch, tmp_path):
    """候補ゼロは非互換ではない。spawn 時の OSError に任せる。"""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr("roundtable.relay_codex._probe_version", lambda p: ())
    assert resolve_codex_binary() == "codex"


def test_preferred_bypasses_probing(monkeypatch, tmp_path):
    """明示指定は尊重する (古い版を意図的に検証したい場合を塞がない)。"""
    _fake_path(monkeypatch, tmp_path, {"old": OLD})
    assert resolve_codex_binary("C:/explicit/codex.exe") == "C:/explicit/codex.exe"


# --- doctor 側: 既知の非互換で start_timeout を消費しない ---


def test_doctor_reports_too_old_without_spawning(monkeypatch, tmp_path):
    """全部古い環境で doctor は app-server を起動せず、理由を分けて報告する。

    「待たない」ことを時間で測るとフレークするので、**副作用の不在**で固定する:
    `_stdio_session` が一度も呼ばれなければ start_timeout は消費されえない。
    """
    from roundtable import doctor

    _fake_path(monkeypatch, tmp_path, {"old": OLD})

    def _must_not_spawn(*a, **kw):
        raise AssertionError("古いと分かっている codex で app-server を起動した")

    monkeypatch.setattr(doctor, "_stdio_session", _must_not_spawn)

    report = doctor.run_doctor(start_timeout=180.0)

    assert report.recommended_tier == 3
    assert report.thread_start == "skipped"
    assert report.codex_binary is None
    assert any("too old" in n for n in report.notes)
    # 「見つからない」と混同されない (原因が別物に見えると調査が逸れる)
    assert "not found" not in report.proxy_connect
    assert "0.130.0" in report.proxy_connect
