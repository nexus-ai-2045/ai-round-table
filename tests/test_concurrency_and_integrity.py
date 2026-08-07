"""並行書き込みと改ざん検知の敵対テスト (バグ1: journal.json のデータ消失)。

守る性質:
- 同一議題に 2 プロセス相当が走っても、**どちらの invocation も消えない**
- human_actions (軸 A KPI) が上書きで減らない / 二重計上もされない
- round は巻き戻らない
- journal.json / seats.json が dispatcher 以外に書かれたら fail-closed
- hash 証跡は改ざん対象と同じ場所に置かない (議題ディレクトリの外に**配置**する。
  席が実際にそこへ書けないかどうかは未検証 — レビュー H4 / review-backlog 参照)

実装の内部構造ではなく守るべき性質でまとめる (test_v02_invariants.py と同じ方針)。
"""
import json
import threading
import time

import pytest

from roundtable import integrity
from roundtable.cli import main
from roundtable.filelock import FileLock, LockTimeout
from roundtable.integrity import StateTamperedError
from roundtable.journal import Journal
from roundtable.paths import ensure_topic
from roundtable.relay import load_seats, merge_seats, save_seats


# --- バグ1 再現: 2 プロセス相当の交互書き込みで記録が消えない ---


def test_interleaved_processes_keep_both_invocations(tmp_path):
    """実測バグそのままの順序: A が load → B が dispatch/merge → A が後から save。

    旧実装ではここで B の invocation が journal から消滅した
    (minutes.md には merge 済みなのに journal には存在しない、という指紋が出た)。
    """
    tp = ensure_topic(tmp_path, "t1")
    a = Journal.load(tp)  # P1: 起動してロード
    inv_a = a.new_invocation("codex", 1)

    b = Journal.load(tp)  # P2: 並行 dispatch
    inv_b = b.new_invocation("cc", 1)
    b.set_state(inv_b, "delivered")
    b.set_state(inv_b, "output-received")
    b.set_state(inv_b, "validated")
    b.set_state(inv_b, "merged")

    a.set_state(inv_a, "failed", "timeout")  # P1 が 900 秒後に timeout を書く

    disk = Journal.load(tp)
    assert set(disk.data["invocations"]) == {inv_a, inv_b}
    assert disk.data["invocations"][inv_b]["state"] == "merged"
    assert disk.data["invocations"][inv_a]["state"] == "failed"


def test_round_does_not_roll_back(tmp_path):
    """遅れて save したプロセスが round を巻き戻さない。"""
    tp = ensure_topic(tmp_path, "t1")
    a = Journal.load(tp)
    inv_a = a.new_invocation("codex", 1)

    b = Journal.load(tp)
    inv_b = b.new_invocation("codex", 1)
    for s in ("delivered", "output-received", "validated", "merged"):
        b.set_state(inv_b, s)
    b.advance_round_if_complete(["codex"])
    assert b.round_no == 2

    a.set_state(inv_a, "failed", "timeout")  # 古い round=1 を持ったまま書く
    assert Journal.load(tp).round_no == 2


def test_human_actions_not_lost_and_not_double_counted(tmp_path):
    """軸 A KPI: 並行 dispatch で人間操作の記録が減らない・水増しもされない。"""
    tp = ensure_topic(tmp_path, "t1")
    a = Journal.load(tp)
    b = Journal.load(tp)
    a.record_human_action("dispatch", "codex:a")
    b.record_human_action("dispatch", "cc:b")
    a.record_human_action("tier3_paste_required", "a")
    assert Journal.load(tp).human_action_count() == 3


def test_identical_actions_at_same_timestamp_both_survive(tmp_path, monkeypatch):
    """内容も時刻も同一の操作が 2 回あれば 2 回残る (集合和にすると 1 回に化ける)。

    時計を固定して「同時刻・同内容」を強制する。id が無ければ区別できない条件。
    """
    class _FrozenDT:
        @staticmethod
        def now(_tz=None):
            from datetime import datetime as _d, timezone as _tzmod

            return _d(2026, 8, 7, 0, 0, 0, tzinfo=_tzmod.utc)

    monkeypatch.setattr("roundtable.journal.datetime", _FrozenDT)
    tp = ensure_topic(tmp_path, "t1")
    a = Journal.load(tp)
    b = Journal.load(tp)
    a.record_human_action("dispatch", "codex")
    b.record_human_action("dispatch", "codex")
    actions = Journal.load(tp).data["human_actions"]
    assert len(actions) == 2
    assert len({x["id"] for x in actions}) == 2


def test_legacy_actions_without_id_are_deduped_not_doubled(tmp_path):
    """id を持たない旧記録は内容で畳む。二重計上より過少計上を選ぶ (限界の明示)。

    id 以前に書かれた journal を読み直しても、自分が書いた分が水増しされない。
    同一内容の操作が本当に 2 回あった場合は区別できない — 観測していない過去の限界。
    """
    tp = ensure_topic(tmp_path, "t1")
    legacy = {"action": "dispatch", "detail": "codex", "at": "2026-08-07T00:00:00+00:00"}
    a = Journal.load(tp)
    a.data["human_actions"].append(dict(legacy))
    a.save()
    b = Journal.load(tp)  # 旧記録をディスクから読み直したプロセス
    b.save()
    assert Journal.load(tp).human_action_count() == 1


def test_parallel_threads_lose_no_invocation(tmp_path):
    """8 スレッド x 10 invocation を同時に走らせて 1 件も落ちない (総当り)。

    各スレッドが独立に Journal.load する = 別プロセス相当。ファイルロックは
    O_EXCL なのでスレッド間でも同じ経路で効く。
    """
    tp = ensure_topic(tmp_path, "t1")
    threads_n, per_thread = 8, 10
    created: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(threads_n)

    def worker(idx: int) -> None:
        try:
            start.wait(timeout=10)
            j = Journal.load(tp)
            for k in range(per_thread):
                inv = j.new_invocation(f"p{idx}", 1)
                j.set_state(inv, "delivered", f"tier3:{k}")
                with lock:
                    created.append(inv)
        except BaseException as exc:  # noqa: BLE001 — main スレッドへ運ぶ
            errors.append(exc)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=120)
    assert not errors, f"worker 失敗: {errors[0]!r}"
    assert len(created) == threads_n * per_thread

    disk = Journal.load(tp)
    assert set(disk.data["invocations"]) == set(created)
    assert all(v["state"] == "delivered" for v in disk.data["invocations"].values())


def test_journal_json_stays_parseable_under_parallel_writes(tmp_path):
    """並行書き込み中に読んでも壊れた JSON が観測されない (atomic + lock)。"""
    tp = ensure_topic(tmp_path, "t1")
    stop = threading.Event()
    bad: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                json.loads(tp.journal.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                bad.append(str(exc))
            except OSError:
                pass  # os.replace 中の一時的な読み取り不能は想定内
            time.sleep(0.001)

    def writer(idx: int) -> None:
        j = Journal.load(tp)
        for _ in range(15):
            j.new_invocation(f"p{idx}", 1)

    r = threading.Thread(target=reader, daemon=True)
    r.start()
    ws = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in ws:
        t.start()
    for t in ws:
        t.join(timeout=60)
    stop.set()
    r.join(timeout=5)
    assert not bad, f"壊れた JSON を観測: {bad[0]}"
    assert len(Journal.load(tp).data["invocations"]) == 60


def test_terminal_conflict_is_recorded_not_silently_dropped(tmp_path):
    """merged と failed が衝突したら、捨てた方を conflicts に残す (黙って消さない)。"""
    tp = ensure_topic(tmp_path, "t1")
    a = Journal.load(tp)
    inv = a.new_invocation("codex", 1)
    b = Journal.load(tp)
    for s in ("delivered", "output-received", "validated", "merged"):
        b.set_state(inv, s)

    # A は古い像を持ったまま failed を書こうとする。ディスクは既に merged (終端)。
    with pytest.raises(ValueError):
        a.set_state(inv, "failed", "timeout")
    # 検証を通さず data を直接壊した場合でも、save の重ね合わせで拾って記録する。
    a.data["invocations"][inv]["state"] = "failed"
    a.save()
    disk = Journal.load(tp)
    assert disk.data["invocations"][inv]["state"] == "merged"  # 先に書いた方を維持
    assert disk.conflicts() and disk.conflicts()[0]["dropped"]["state"] == "failed"


# --- 改ざん検知 (journal.json / seats.json) ---


def test_journal_tamper_is_fail_closed(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    data = json.loads(tp.journal.read_text(encoding="utf-8"))
    data["invocations"][inv]["state"] = "merged"  # 席が自分を merged に書き換える
    tp.journal.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateTamperedError):
        Journal.load(tp)


def test_journal_deletion_is_detected(tmp_path):
    """消去も改ざん。「記録が無い」を「最初から無かった」と読ませない。"""
    tp = ensure_topic(tmp_path, "t1")
    Journal.load(tp).new_invocation("codex", 1)
    tp.journal.unlink()
    with pytest.raises(StateTamperedError):
        Journal.load(tp)


def test_seats_tamper_is_fail_closed(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    save_seats(tp, {"rt/t1/codex": {"participant": "codex", "tier": 3}})
    tp.seats.write_text(
        json.dumps({"rt/t1/codex": {"participant": "codex", "tier": 1}}),
        encoding="utf-8",
    )
    with pytest.raises(StateTamperedError):
        load_seats(tp)


def test_tampered_error_is_valueerror():
    """既存の except ValueError 経路を壊さない。"""
    assert issubclass(StateTamperedError, ValueError)


def test_witness_is_placed_outside_the_topic_directory(tmp_path):
    """証跡を議題ディレクトリ (= 席の cwd) の外に置く、という **配置** を固定する。

    同じ場所に置くと、対象を書き換えられる相手が証跡も書き換えられ検知が消える。

    **このテストが確かめているのは path の入れ子関係だけ**である (レビュー H4)。
    「席が実際にそこへ書けない」= sandbox の実効書込範囲は一度も観測していない。
    それは実 CLI を起動する probe でしか閉じられない (docs/review-backlog.md)。
    テスト名から `seat_writable_area` を外したのは、検証していない範囲を名前で
    主張しないため。
    """
    tp = ensure_topic(tmp_path, "t1")
    Journal.load(tp).new_invocation("codex", 1)
    w = tp.witness("journal.json")
    assert w.exists()
    assert tp.root.resolve() not in w.resolve().parents
    assert not any(p.suffix == ".sha256" for p in tp.root.rglob("*"))


def test_witness_forgery_inside_topic_dir_does_not_help(tmp_path):
    """議題ディレクトリ内に偽証跡を置いても照合先は変わらない。"""
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    j.new_invocation("codex", 1)
    forged = {"round": 99, "invocations": {}, "human_actions": [], "conflicts": []}
    text = json.dumps(forged, ensure_ascii=False, indent=1)
    tp.journal.write_text(text, encoding="utf-8", newline="\n")
    (tp.root / "journal.json.sha256").write_text(
        json.dumps({"sha256": integrity.digest(text.encode("utf-8"))}), encoding="utf-8"
    )
    with pytest.raises(StateTamperedError):
        Journal.load(tp)


def test_pre_existing_journal_is_adopted_and_marked(tmp_path):
    """証跡より前から在る journal は採用する。ただし『観測ではない』と証跡に残す。"""
    tp = ensure_topic(tmp_path, "t1")
    tp.journal.write_text(
        json.dumps({"round": 1, "invocations": {}, "human_actions": []}),
        encoding="utf-8",
    )
    j = Journal.load(tp)  # 例外にしない (観測していない過去は検証できない)
    assert j.round_no == 1
    rec = json.loads(tp.witness("journal.json").read_text(encoding="utf-8"))
    assert rec["adopted"] is True


def test_interrupted_write_is_not_called_tampering(tmp_path):
    """本文書き込みが届かなかった窓を改ざん扱いにしない (自滅的 fail-closed の回避)。"""
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    j.new_invocation("codex", 1)
    before = tp.journal.read_bytes()
    w = tp.witness("journal.json")
    rec = json.loads(w.read_text(encoding="utf-8"))
    # 「予告だけ書いて落ちた」状態を再現する
    rec = {
        "sha256": "0" * 64,
        "pending_from": integrity.digest(before),
        "adopted": False,
        "at": rec["at"],
    }
    w.write_text(json.dumps(rec), encoding="utf-8")
    assert Journal.load(tp).data["invocations"]  # 読める
    fixed = json.loads(w.read_text(encoding="utf-8"))
    assert fixed["sha256"] == integrity.digest(before)  # 証跡は実物へ巻き戻る


# --- seats.json の並行安全 ---


def test_parallel_seat_writes_keep_both_seats(tmp_path):
    """他席のエントリを消さない (実測では一方の tier/fallback_reason ごと消えた)。"""
    tp = ensure_topic(tmp_path, "t1")
    a = load_seats(tp)
    b = load_seats(tp)
    a["rt/t1/codex"] = {"participant": "codex", "tier": 3, "fallback_reason": "timeout"}
    b["rt/t1/cc"] = {"participant": "cc", "tier": 3}
    save_seats(tp, b)
    save_seats(tp, a)  # 古い像を持ったまま後から書く
    disk = load_seats(tp)
    assert set(disk) == {"rt/t1/codex", "rt/t1/cc"}
    assert disk["rt/t1/codex"]["fallback_reason"] == "timeout"


def test_thread_ref_is_not_lost_by_a_stale_writer(tmp_path):
    """席の同一性 (thread_ref) を落とすと CEO が見ていない別チャットに席が分裂する。"""
    disk = {"rt/t1/codex": {"participant": "codex", "tier": 1, "thread_ref": "th-1"}}
    stale = {"rt/t1/codex": {"participant": "codex", "tier": 1}}
    assert merge_seats(disk, stale)["rt/t1/codex"]["thread_ref"] == "th-1"


def test_stale_fallback_reason_is_not_resurrected(tmp_path):
    """逆に、解決済みの失敗痕跡を復活させない (成功を失敗に見せない)。"""
    disk = {"rt/t1/codex": {"participant": "codex", "tier": 3, "fallback_reason": "x"}}
    fresh = {"rt/t1/codex": {"participant": "codex", "tier": 1}}
    assert "fallback_reason" not in merge_seats(disk, fresh)["rt/t1/codex"]


# --- ロックの残骸回収 (Windows で確実に動く経路) ---


def test_stale_lock_is_reclaimed(tmp_path):
    """ロックを握ったままプロセスが死んでも、次の dispatch が永久に止まらない。"""
    path = tmp_path / "x.lock"
    path.write_text("{}", encoding="utf-8")
    import os

    old = time.time() - 3600
    os.utime(path, (old, old))
    with FileLock(path, timeout_s=5.0, stale_after_s=30.0):
        assert path.exists()
    assert not path.exists()


def test_live_lock_blocks_and_times_out(tmp_path):
    """生きているロックは奪わない (奪ったら重ね合わせが不可分でなくなる)。"""
    path = tmp_path / "x.lock"
    with FileLock(path, timeout_s=5.0):
        with pytest.raises(LockTimeout):
            FileLock(path, timeout_s=0.3, stale_after_s=30.0).acquire()


# --- CLI 境界: 改ざんは CEO に提示して止まる ---


def test_cli_reports_tamper_and_stops(tmp_path, capsys):
    main(["new-topic", "t1", "--topic", "X", "--participants", "codex",
          "--root", str(tmp_path)])
    tp = ensure_topic(tmp_path, "t1")
    tp.journal.write_text(
        json.dumps({"round": 9, "invocations": {}, "human_actions": []}),
        encoding="utf-8",
    )
    capsys.readouterr()
    rc = main(["status", "t1", "--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 3
    assert "改ざん" in err and "journal.json" in err
