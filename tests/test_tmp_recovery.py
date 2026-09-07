"""`.tmp` 取り残しによる偽陰性 (バグ2) の回帰テスト。

守る性質:
  1. 完成した `.tmp` が残ったまま timeout した場合、失敗ではなく救済して merge する
  2. 書きかけ / 契約違反の `.tmp` は絶対に採用しない (成功を捏造しない)
  3. 救済した時は来歴 (`recovered-from-tmp`) が journal と CEO 出力の両方に残る
  4. `.tmp` が残っている timeout で「未貼り付け?」と誤誘導しない

実測の背景 (2026-08-07, minutes/tier1-final): 席が `aca57e43a4f8.json.tmp` を
**完成した状態で**書いた 13 分後に timeout 記録された。watcher が `.json` しか
見ていなかったため、成功が失敗として記録されていた。
"""
import json

from roundtable import minutes, watcher
from roundtable.cli import main
from roundtable.journal import Journal
from roundtable.paths import ensure_topic


class _Clock:
    """テスト用の擬似時計。sleep でだけ時間が進む (実時間を待たない)。

    on_sleep に副作用を渡せる = 「grace 中に席がファイルを触った」を再現できる。
    """

    def __init__(self, on_sleep=None):
        self.t = 0.0
        self._on_sleep = on_sleep

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        if self._on_sleep is not None:
            self._on_sleep()


def _setup(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    minutes.create(tp, "X", ["codex"])
    minutes.make_snapshot(tp)
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    return tp, j, inv


def _opinion(inv, participant="codex"):
    return {
        "invocation_id": inv,
        "participant": participant,
        "opinion": "tmp に書かれた意見",
        "claims": [{"claim": "A", "evidence_type": "observed", "evidence": "B"}],
    }


def _write_tmp(tp, inv, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    (tp.scratch / f"{inv}.json.tmp").write_text(text, encoding="utf-8")


# --- 1. 完成 .tmp の救済 ------------------------------------------------------


def test_complete_tmp_is_recovered_instead_of_timeout(tmp_path):
    """rename されなかっただけの完成 .tmp は timeout ではなく救済される。"""
    tp, j, inv = _setup(tmp_path)
    _write_tmp(tp, inv, _opinion(inv))

    r = watcher.collect(tp, j, inv, "codex", timeout_s=0, clock=_Clock(), tmp_stable_s=0.01)

    assert r["ok"] is True
    assert r["recovered"] == "tmp"
    assert j.is_merged(inv)
    assert "tmp に書かれた意見" in tp.minutes.read_text(encoding="utf-8")


def test_timeout_stays_timeout_when_no_tmp(tmp_path):
    """.tmp が無い純粋な無応答は、従来どおり timeout のまま (分類を混ぜない)。"""
    tp, j, inv = _setup(tmp_path)

    r = watcher.collect(tp, j, inv, "codex", timeout_s=0, clock=_Clock())

    assert r == {"ok": False, "reason": "timeout"}
    assert j.data["invocations"][inv]["state"] == "waiting"


def test_tmp_renamed_during_grace_uses_normal_path(tmp_path):
    """grace 中に席が rename を完了したら、救済ではなく通常経路で処理する。"""
    tp, j, inv = _setup(tmp_path)
    _write_tmp(tp, inv, _opinion(inv))

    def _rename():
        src = tp.scratch / f"{inv}.json.tmp"
        if src.exists():
            src.replace(tp.scratch / f"{inv}.json")

    r = watcher.collect(
        tp, j, inv, "codex", timeout_s=0, clock=_Clock(on_sleep=_rename), tmp_stable_s=0.01
    )

    assert r == {"ok": True}  # recovered キーが付かない = 通常経路
    assert j.data["invocations"][inv]["detail"] == ""


# --- 2. 書きかけ .tmp を採用しない --------------------------------------------


def test_partial_tmp_is_not_adopted(tmp_path):
    """JSON として不完全な書きかけは採用しない (成功を捏造しない)。"""
    tp, j, inv = _setup(tmp_path)
    _write_tmp(tp, inv, '{"invocation_id": "' + inv + '", "participant": "codex", "opin')

    r = watcher.collect(tp, j, inv, "codex", timeout_s=0, clock=_Clock(), tmp_stable_s=0.01)

    assert r["ok"] is False
    assert r["reason"] == "stalled-tmp"
    assert not j.is_merged(inv)
    assert "### codex" not in tp.minutes.read_text(encoding="utf-8")
    assert j.data["invocations"][inv]["state"] == "waiting"


def test_tmp_still_growing_is_not_adopted(tmp_path):
    """grace 中に内容が変化する = まだ書き込み中。JSON として妥当でも採用しない。"""
    tp, j, inv = _setup(tmp_path)
    _write_tmp(tp, inv, _opinion(inv))

    def _keep_writing():
        payload = _opinion(inv)
        payload["opinion"] = "書き足された意見"
        _write_tmp(tp, inv, payload)

    r = watcher.collect(
        tp, j, inv, "codex", timeout_s=0, clock=_Clock(on_sleep=_keep_writing), tmp_stable_s=0.01
    )

    assert r["reason"] == "stalled-tmp"
    assert "まだ書き込み中" in r["detail"]
    assert not j.is_merged(inv)


def test_schema_violating_tmp_is_not_adopted(tmp_path):
    """parse できても契約違反 (claims 空) なら採用しない。"""
    tp, j, inv = _setup(tmp_path)
    payload = _opinion(inv)
    payload["claims"] = []
    _write_tmp(tp, inv, payload)

    r = watcher.collect(tp, j, inv, "codex", timeout_s=0, clock=_Clock(), tmp_stable_s=0.01)

    assert r["reason"] == "stalled-tmp"
    assert "schema 違反" in r["detail"]
    assert not j.is_merged(inv)


def test_id_mismatched_tmp_is_not_adopted(tmp_path):
    """他の invocation 向けの .tmp を拾わない。"""
    tp, j, inv = _setup(tmp_path)
    _write_tmp(tp, inv, _opinion("deadbeef0000"))

    r = watcher.collect(tp, j, inv, "codex", timeout_s=0, clock=_Clock(), tmp_stable_s=0.01)

    assert r["reason"] == "stalled-tmp"
    assert not j.is_merged(inv)


# --- 3. 救済時に来歴が残る ----------------------------------------------------


def test_recovery_leaves_provenance_in_journal(tmp_path):
    """来歴は journal に永続化され、再ロードしても残る (黙って成功にしない)。"""
    tp, j, inv = _setup(tmp_path)
    _write_tmp(tp, inv, _opinion(inv))

    watcher.collect(tp, j, inv, "codex", timeout_s=0, clock=_Clock(), tmp_stable_s=0.01)

    reloaded = Journal.load(tp).data["invocations"][inv]
    assert reloaded["state"] == "merged"
    assert reloaded["detail"].startswith(watcher.RECOVERED_FROM_TMP)
    assert f"{inv}.json.tmp" in reloaded["detail"]


def test_recovery_keeps_the_tmp_as_evidence(tmp_path):
    """採用した .tmp は消さない (何を merge したかの物証を残す)。"""
    tp, j, inv = _setup(tmp_path)
    _write_tmp(tp, inv, _opinion(inv))

    watcher.collect(tp, j, inv, "codex", timeout_s=0, clock=_Clock(), tmp_stable_s=0.01)

    assert (tp.scratch / f"{inv}.json.tmp").exists()


def test_normal_path_has_no_recovery_provenance(tmp_path):
    """通常経路に来歴が混ざらない (救済かどうかを機械的に区別できる)。"""
    tp, j, inv = _setup(tmp_path)
    (tp.scratch / f"{inv}.json").write_text(
        json.dumps(_opinion(inv), ensure_ascii=False), encoding="utf-8"
    )

    r = watcher.collect(tp, j, inv, "codex", timeout_s=1, clock=_Clock(), tmp_stable_s=0.01)

    assert r == {"ok": True}
    assert Journal.load(tp).data["invocations"][inv]["detail"] == ""


# --- 4. CEO 向け出力 ----------------------------------------------------------


def _dispatch_async(tmp_path) -> tuple:
    main([
        "new-topic", "t1", "--topic", "X", "--participants", "codex",
        "--root", str(tmp_path),
    ])
    main([
        "dispatch", "t1", "--participant", "codex", "--no-clipboard", "--async",
        "--root", str(tmp_path),
    ])
    tp = ensure_topic(tmp_path, "t1")
    inv = next(iter(json.loads(tp.journal.read_text(encoding="utf-8"))["invocations"]))
    return tp, inv


def test_cli_reports_recovery_provenance(tmp_path, capsys):
    """救済成功を「普通の成功」として黙って出さない。"""
    tp, inv = _dispatch_async(tmp_path)
    _write_tmp(tp, inv, _opinion(inv))
    capsys.readouterr()

    rc = main(["collect", "t1", "--invocation", inv, "--timeout", "0", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "[recovered-from-tmp]" in out
    assert "確定 (rename) していない" in out
    last = json.loads(tp.last_result.read_text(encoding="utf-8"))
    assert last["recovered"] == "tmp"


def test_cli_reports_stalled_tmp_instead_of_paste_hint(tmp_path, capsys):
    """書きかけ .tmp は timeout ではなく stalled-tmp として案内する。"""
    tp, inv = _dispatch_async(tmp_path)
    _write_tmp(tp, inv, "{壊れた")
    capsys.readouterr()

    rc = main(["collect", "t1", "--invocation", inv, "--timeout", "0", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 1
    assert "[stalled-tmp]" in out
    assert "(未貼り付け?)" not in out  # 席は応答済みなので、この誤誘導文言を出さない
    last = json.loads(tp.last_result.read_text(encoding="utf-8"))
    assert last["reason"] == "stalled-tmp"


def test_cli_reports_leftover_tmp_on_timeout(tmp_path, capsys):
    """当該 invocation 以外の .tmp 残存も timeout 時に必ず出す。"""
    tp, inv = _dispatch_async(tmp_path)
    (tp.scratch / "orphan.json.tmp").write_text("{}", encoding="utf-8")
    capsys.readouterr()

    rc = main(["collect", "t1", "--invocation", inv, "--timeout", "0", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 1
    assert "[tmp 残存]" in out
    assert "orphan.json.tmp" in out
    # 「貼り付けたか確認してください (未貼り付け?)」と断定する誤誘導は出さない
    assert "(未貼り付け?)" not in out
