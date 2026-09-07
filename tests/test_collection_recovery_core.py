"""回収の再起動・遅着・競合の境界。実 ledger を通して検証する。"""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from roundtable import minutes, watcher
from roundtable.journal import Journal
from roundtable.paths import ensure_topic


def setup(tmp_path):
    tp = ensure_topic(tmp_path, "recovery")
    minutes.create(tp, "Recovery", ["codex"])
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    data = {"invocation_id": inv, "participant": "codex", "opinion": "original",
            "claims": [{"claim": "A", "evidence_type": "argument", "evidence": "B"}]}
    return tp, j, inv, data


def write(tp, inv, data):
    (tp.scratch / f"{inv}.json").write_text(json.dumps(data), encoding="utf-8")


def test_late_response_after_process_restart(tmp_path):
    tp, j, inv, data = setup(tmp_path)
    assert watcher.collect(tp, j, inv, "codex", timeout_s=0)["reason"] == "timeout"
    assert Journal.load(tp).data["invocations"][inv]["state"] == "waiting"
    write(tp, inv, data)
    assert watcher.collect(tp, Journal.load(tp), inv, "codex", timeout_s=0)["ok"]
    assert Journal.load(tp).data["invocations"][inv]["response_sha256"] == minutes.opinion_hash(data)


@pytest.mark.parametrize("detail", ["timeout", "stalled-tmp: incomplete"])
def test_explicit_collect_recovers_legacy_wait_failures(tmp_path, detail):
    tp, j, inv, data = setup(tmp_path)
    j.set_state(inv, "failed", detail)
    write(tp, inv, data)
    assert watcher.collect(tp, j, inv, "codex", timeout_s=0)["ok"]
    assert j.data["invocations"][inv]["wait_failures"] == [detail]


def test_validation_failure_is_terminal(tmp_path):
    tp, j, inv, data = setup(tmp_path)
    j.set_state(inv, "failed", "schema: invalid")
    write(tp, inv, data)
    assert watcher.collect(tp, j, inv, "codex", timeout_s=0)["reason"] == "failed"
    assert "### codex" not in tp.minutes.read_text()


def test_cancelled_late_response_never_merges(tmp_path):
    tp, j, inv, data = setup(tmp_path)
    assert watcher.cancel(tp, inv)["ok"]
    write(tp, inv, data)
    assert watcher.collect(tp, j, inv, "codex", timeout_s=0)["reason"] == "cancelled"
    assert "### codex" not in tp.minutes.read_text()


def test_waiter_releases_lock_for_cancel(tmp_path):
    tp, j, inv, data = setup(tmp_path)
    class Clock:
        def monotonic(self):
            return 0
        def sleep(self, seconds):
            assert watcher.cancel(tp, inv)["ok"]
    assert watcher.collect(tp, j, inv, "codex", timeout_s=1, clock=Clock())["reason"] == "cancelled"


def test_two_collectors_merge_once(tmp_path):
    tp, j, inv, data = setup(tmp_path)
    write(tp, inv, data)
    def collect():
        return watcher.collect(tp, Journal.load(tp), inv, "codex", timeout_s=0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: collect(), range(2)))
    assert all(r["ok"] for r in results)
    assert tp.minutes.read_text().count(f"(invocation: {inv})") == 1


def test_crash_after_minutes_commit_resumes_without_duplicate(tmp_path, monkeypatch):
    tp, j, inv, data = setup(tmp_path)
    write(tp, inv, data)
    original = Journal.set_state
    def crash(self, inv, state, detail=""):
        if state == "merged":
            raise RuntimeError("crash after minutes commit")
        original(self, inv, state, detail)
    with monkeypatch.context() as patch:
        patch.setattr(Journal, "set_state", crash)
        with pytest.raises(RuntimeError, match="crash"):
            watcher.collect(tp, j, inv, "codex", timeout_s=0)
    assert watcher.collect(tp, Journal.load(tp), inv, "codex", timeout_s=0)["ok"]
    assert tp.minutes.read_text().count(f"(invocation: {inv})") == 1


def test_changed_response_after_validation_fails_closed(tmp_path):
    tp, j, inv, data = setup(tmp_path)
    j.data["invocations"][inv]["response_sha256"] = minutes.opinion_hash(data)
    j.set_state(inv, "validated")
    data["opinion"] = "changed"
    write(tp, inv, data)
    assert watcher.collect(tp, j, inv, "codex", timeout_s=0)["reason"] == "response-changed"
    assert "### codex" not in tp.minutes.read_text()


def test_minutes_receipt_rejects_conflicting_invocation(tmp_path):
    tp, j, inv, data = setup(tmp_path)
    minutes.merge_opinion(tp, data, 1)
    data["opinion"] = "changed"
    with pytest.raises(minutes.OpinionConflictError):
        minutes.merge_opinion(tp, data, 1)
    assert "changed" not in tp.minutes.read_text()


def test_late_round_stays_in_original_section(tmp_path):
    tp, j, inv, data = setup(tmp_path)
    first = dict(data, invocation_id="first")
    second = dict(data, invocation_id="second")
    minutes.merge_opinion(tp, first, 1)
    minutes.merge_opinion(tp, second, 2)
    j.data["round"] = 3
    j.save()
    write(tp, inv, data)
    assert watcher.collect(tp, j, inv, "codex", timeout_s=0)["ok"]
    text = tp.minutes.read_text()
    assert text.index(f"(invocation: {inv})") < text.index("## Round 2")


def test_cancel_collect_race_has_single_winner(tmp_path, monkeypatch):
    tp, j, inv, data = setup(tmp_path)
    write(tp, inv, data)
    entered, release = Event(), Event()
    original = minutes.merge_opinion
    def merge(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(minutes, "merge_opinion", merge)
    with ThreadPoolExecutor(max_workers=2) as pool:
        collection = pool.submit(watcher.collect, tp, j, inv, "codex", 0)
        assert entered.wait(10)
        cancellation = pool.submit(watcher.cancel, tp, inv)
        release.set()
        assert collection.result()["ok"]
        assert cancellation.result() == {"ok": False, "reason": "merged"}
    assert Journal.load(tp).data["invocations"][inv]["state"] == "merged"


def test_stale_journal_cannot_restore_reopened_failure(tmp_path):
    tp, j, inv, data = setup(tmp_path)
    j.set_state(inv, "failed", "timeout")
    stale = Journal.load(tp)
    assert watcher.collect(tp, j, inv, "codex", timeout_s=0)["reason"] == "timeout"
    stale.record_human_action("unrelated")
    assert Journal.load(tp).data["invocations"][inv]["state"] == "waiting"


def test_cancel_after_minutes_commit_recovers_merged_state(tmp_path):
    tp, j, inv, data = setup(tmp_path)
    j.data["invocations"][inv]["response_sha256"] = minutes.opinion_hash(data)
    j.set_state(inv, "validated")
    minutes.merge_opinion(tp, data, 1)
    assert watcher.cancel(tp, inv) == {"ok": False, "reason": "merged"}
    assert Journal.load(tp).data["invocations"][inv]["state"] == "merged"


def test_cancel_cannot_steal_aged_invocation_lock(tmp_path, monkeypatch):
    """内側処理待機が長くても取消がholderを横取りしない。"""
    import os
    from roundtable.filelock import AdvisoryFileLock, LockTimeout

    tp, j, inv, data = setup(tmp_path)
    write(tp, inv, data)
    entered, release = Event(), Event()
    original = minutes.merge_opinion
    def merge(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(minutes, "merge_opinion", merge)
    with ThreadPoolExecutor(max_workers=1) as pool:
        collection = pool.submit(watcher.collect, tp, j, inv, "codex", 0)
        try:
            assert entered.wait(10)
            lock_path = tp.lock(f"collect-{inv}")
            os.utime(lock_path, (1, 1))
            monkeypatch.setattr(watcher, "_invocation_lock",
                                lambda tp, inv: AdvisoryFileLock(lock_path, timeout_s=0))
            with pytest.raises(LockTimeout):
                watcher.cancel(tp, inv)
        finally:
            release.set()
        assert collection.result()["ok"]
    assert Journal.load(tp).data["invocations"][inv]["state"] == "merged"
