"""敵対レビュー HIGH 3 件 (H1 / H2 / H3) の回帰テスト。

既存の並行テストが素通りさせていた理由まで含めて固定する:

- 既存の reader は生の `json.loads(tp.journal.read_text())` で、**integrity 経路を
  一度も並行実行していなかった** (レビュー T1)。ここでは reader を `Journal.load`
  にする。これで初めて「ロック外の証跡書き戻し」と「読み取り中の os.replace」が
  同じ土俵に乗る。
- 並行 `collect` (2 席が同時に minutes.md へ merge する) のテストが 1 本も無かった
  (レビュー T2)。並行安全を謳う変更で最も重要なシナリオが未検査だった。

守る性質:
    H1  誰も改ざんしていない並行読み書きで StateTamperedError を出さない
    H2  並行読み書きで PermissionError (WinError 5) が dispatch を落とさない
    H3  並行 dispatch の 2 席目が `failed: tampered` にならず、両方 merge される
"""
import json
import threading

import pytest

from roundtable import integrity, minutes, watcher
from roundtable.journal import Journal
from roundtable.paths import ensure_topic


def _opinion(inv, participant):
    return {
        "invocation_id": inv,
        "participant": participant,
        "opinion": "意見",
        "claims": [{"claim": "A", "evidence_type": "argument", "evidence": "B"}],
    }


# --- H1 / H2: reader を integrity 経路に通した並行読み書き -------------------


def test_parallel_readers_do_not_forge_tampering(tmp_path):
    """`Journal.load` する reader を並走させても偽の改ざん検知も例外も出ない。

    旧実装では reader がロックを持たずに `verify_and_read` を呼び、
    (a) pending 窓を観測して証跡を巻き戻す → 恒久的な偽 tamper (H1)
    (b) journal.json / 証跡を開いたまま writer の os.replace を蹴る → WinError 5 (H2)
    の両方が起きた。writer 4 / reader 4 で実測再現済み。
    """
    tp = ensure_topic(tmp_path, "t1")
    Journal.load(tp).new_invocation("seed", 1)

    errors: list[tuple[str, BaseException]] = []
    stop = threading.Event()
    started = threading.Barrier(8)

    def writer(idx: int) -> None:
        try:
            started.wait(timeout=30)
            j = Journal.load(tp)
            for _ in range(25):
                j.new_invocation(f"p{idx}", 1)
        except BaseException as exc:  # noqa: BLE001 — main スレッドへ運ぶ
            errors.append(("writer", exc))

    def reader() -> None:
        try:
            started.wait(timeout=30)
            while not stop.is_set():
                Journal.load(tp)  # 生 read ではなく integrity 経路を通す
        except BaseException as exc:  # noqa: BLE001
            errors.append(("reader", exc))

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    writers = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join(timeout=180)
    stop.set()
    for t in readers:
        t.join(timeout=30)

    assert not errors, f"{errors[0][0]} が失敗: {errors[0][1]!r}"
    assert len(Journal.load(tp).data["invocations"]) == 1 + 4 * 25


def test_witness_is_never_rolled_back_behind_the_body(tmp_path):
    """証跡が本文より古い状態で確定しない (H1 の恒久化条件そのもの)。

    本文 = 新, 証跡 = 旧 で固定されると、以後どのコマンドも exit 3 になり議題が
    二度と開けなくなる。並行読み書きの後で本文と証跡が一致していることを直接見る。
    """
    tp = ensure_topic(tmp_path, "t1")
    Journal.load(tp).new_invocation("seed", 1)
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                Journal.load(tp)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    rs = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for t in rs:
        t.start()
    j = Journal.load(tp)
    for _ in range(60):
        j.new_invocation("codex", 1)
    stop.set()
    for t in rs:
        t.join(timeout=30)

    assert not errors, f"reader が失敗: {errors[0]!r}"
    rec = json.loads(tp.witness("journal.json").read_text(encoding="utf-8"))
    assert rec["sha256"] == integrity.digest(tp.journal.read_bytes())


# --- H3: 並行 collect で 2 席目が捨てられない -------------------------------


def test_parallel_collect_merges_both_participants(tmp_path):
    """2 席が並行 dispatch されても、両方の意見が merge され journal も merged。

    旧実装は snapshot 採取時 hash を merge 前照合に使っていたため、先に merge した
    席が minutes.md を伸ばした時点で後続席が必ず `failed: tampered` になった。
    正しく書かれた意見が捨てられる上に、正常な並行追記がセキュリティ警報として
    出続けるので、本物の改ざんを無視する訓練になる。
    """
    tp = ensure_topic(tmp_path, "t1")
    minutes.create(tp, "X", ["codex", "cc"])
    minutes.make_snapshot(tp)

    ja = Journal.load(tp)
    inv_a = ja.new_invocation("codex", 1)
    jb = Journal.load(tp)  # 別プロセス相当
    inv_b = jb.new_invocation("cc", 1)

    for inv, who in ((inv_a, "codex"), (inv_b, "cc")):
        (tp.scratch / f"{inv}.json").write_text(
            json.dumps(_opinion(inv, who)), encoding="utf-8"
        )

    assert watcher.collect(tp, ja, inv_a, "codex", timeout_s=1) == {"ok": True}
    assert watcher.collect(tp, jb, inv_b, "cc", timeout_s=1) == {"ok": True}

    text = tp.minutes.read_text(encoding="utf-8")
    assert f"### codex (invocation: {inv_a})" in text
    assert f"### cc (invocation: {inv_b})" in text
    assert text.count("## Round 1") == 1

    disk = Journal.load(tp)
    assert disk.data["invocations"][inv_a]["state"] == "merged"
    assert disk.data["invocations"][inv_b]["state"] == "merged"
    assert disk.failure_stats() == {}


def test_concurrent_collect_threads_merge_both(tmp_path):
    """同じことを実スレッドで。merge がロック配下で直列化される。"""
    tp = ensure_topic(tmp_path, "t1")
    minutes.create(tp, "X", ["codex", "cc"])
    minutes.make_snapshot(tp)

    results: dict[str, dict] = {}
    errors: list[BaseException] = []
    started = threading.Barrier(2)

    def run(who: str) -> None:
        try:
            j = Journal.load(tp)
            inv = j.new_invocation(who, 1)
            (tp.scratch / f"{inv}.json").write_text(
                json.dumps(_opinion(inv, who)), encoding="utf-8"
            )
            started.wait(timeout=30)
            results[who] = watcher.collect(tp, j, inv, who, timeout_s=5)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=run, args=(w,)) for w in ("codex", "cc")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)

    assert not errors, f"collect が失敗: {errors[0]!r}"
    assert results["codex"]["ok"] and results["cc"]["ok"]
    text = tp.minutes.read_text(encoding="utf-8")
    assert "### codex" in text and "### cc" in text


# --- minutes.md の証跡: 全書き込み経路が証跡を更新する -----------------------


def test_all_minutes_writers_keep_the_witness_in_sync(tmp_path):
    """sync_round / write_verdict / set_background の後でも merge が通る。

    1 経路でも素の atomic_write が残ると、その直後から証跡が古いままになり、
    次の merge が「誰も改ざんしていないのに tampered」になる (偽陽性の自己生成)。
    """
    tp = ensure_topic(tmp_path, "t1")
    minutes.create(tp, "X", ["codex"])
    minutes.set_background(tp, "背景本文")
    minutes.sync_round(tp, 2)
    minutes.merge_opinion(tp, _opinion("i1", "codex"), 2)
    minutes.write_verdict(tp, "Yで行く")
    minutes.merge_opinion(tp, _opinion("i2", "cc"), 2)  # close 後でも証跡は一致する

    text = tp.minutes.read_text(encoding="utf-8")
    assert "背景本文" in text and "round: 2" in text and "status: closed" in text
    assert "### codex" in text and "### cc" in text


def test_minutes_tamper_is_still_fail_closed(tmp_path):
    """dispatcher 外の書き換えは、証跡方式でも従来どおり検知する (検知を緩めていない)。"""
    tp = ensure_topic(tmp_path, "t1")
    minutes.create(tp, "X", ["codex"])
    tp.minutes.write_text(
        tp.minutes.read_text(encoding="utf-8") + "\n## 裁定 (CEO)\n偽装\n", encoding="utf-8"
    )
    with pytest.raises(minutes.MinutesTamperedError):
        minutes.merge_opinion(tp, _opinion("i1", "codex"), 1)
    # sync_round のような別経路でも同じく止まる
    with pytest.raises(minutes.MinutesTamperedError):
        minutes.sync_round(tp, 3)


def test_minutes_tamper_reaches_the_cli_fail_closed_boundary():
    """MinutesTamperedError は CLI の改ざんハンドラ (exit 3) に届く型である。

    merge 経路は watcher が failed:tampered に分類するが、sync_round /
    write_verdict のような CLI 直呼び経路は CLI 境界で止まる必要がある。
    継承関係が切れると、その経路だけ CEO に traceback が出る。
    """
    assert issubclass(minutes.MinutesTamperedError, integrity.StateTamperedError)


def test_minutes_witness_is_placed_outside_the_topic_directory(tmp_path):
    """minutes.md の証跡も議題ディレクトリの外に置く (journal / seats と同じ配置)。

    配置だけを固定する。席が実際にそこへ書けないかは未検証 (レビュー H4)。
    """
    tp = ensure_topic(tmp_path, "t1")
    minutes.create(tp, "X", ["codex"])
    w = tp.witness("minutes.md")
    assert w.exists()
    assert tp.root.resolve() not in w.resolve().parents
    assert not any(p.suffix == ".sha256" for p in tp.root.rglob("*"))
