"""送信確定と並行回収・取消の境界を検証する。"""
import json
import threading

import pytest

from roundtable import cli, handoff, minutes, watcher
from roundtable.journal import Journal
from roundtable.paths import ensure_topic
from roundtable.relay import DeliveryUnknownError


def topic(root):
    tp = ensure_topic(root, "t1")
    minutes.create(tp, "並行回収", ["codex"])
    return tp


def reply(tp, inv):
    (tp.scratch / f"{inv}.json").write_text(json.dumps({
        "invocation_id": inv, "participant": "codex", "opinion": "回答",
        "claims": [{"claim": "A", "evidence_type": "argument", "evidence": "B"}],
    }), encoding="utf-8")


@pytest.mark.parametrize("outcome", ["delivered", "unknown", "failed"])
def test_collector_waits_until_send_state_is_committed(tmp_path, monkeypatch, outcome):
    tp = topic(tmp_path)
    attempted = threading.Event()
    finished = threading.Event()
    threads = []
    results = []
    original_lock = watcher._invocation_lock

    def lock(paths, inv):
        if threading.current_thread().name == "collector":
            attempted.set()
        return original_lock(paths, inv)

    monkeypatch.setattr(watcher, "_invocation_lock", lock)

    def collect():
        try:
            inv = next(iter(Journal.load(tp).data["invocations"]))
            results.append(cli._collect_one(tp, Journal.load(tp), inv, "codex", 0, True))
        finally:
            finished.set()

    class Relay:
        tier = 1
        closed = False

        def send(self, seat, text):
            inv = next(iter(Journal.load(tp).data["invocations"]))
            reply(tp, inv)
            thread = threading.Thread(target=collect, name="collector")
            threads.append(thread)
            thread.start()
            assert attempted.wait(5)
            assert not finished.wait(0.15), "送信状態確定前にcollectorが完了した"
            assert Journal.load(tp).data["invocations"][inv]["state"] == "prepared"
            if outcome == "unknown":
                raise DeliveryUnknownError("受理結果不明")
            if outcome == "failed":
                raise RuntimeError("送信失敗")
            return "test-relay"

        def close(self):
            self.closed = True

    relay = Relay()
    monkeypatch.setattr(cli, "get_relay", lambda *a, **kw: relay)
    try:
        rc = cli.main(["dispatch", "t1", "--participant", "codex", "--tier", "1",
                       "--timeout", "0", "--root", str(tmp_path)])
    finally:
        for thread in threads:
            thread.join(10)
    assert finished.is_set()
    assert relay.closed
    assert rc == (1 if outcome == "failed" else 0)
    assert results == [1 if outcome == "failed" else 0]
    rec = next(iter(Journal.load(tp).data["invocations"].values()))
    assert rec["state"] == ("failed" if outcome == "failed" else "merged")


def test_cancel_before_send_prevents_relay_creation(tmp_path, monkeypatch):
    tp = topic(tmp_path)
    capture = handoff.capture_request

    def capture_and_cancel(paths, inv, text):
        result = capture(paths, inv, text)
        assert watcher.cancel(paths, inv)["ok"]
        return result

    monkeypatch.setattr(handoff, "capture_request", capture_and_cancel)
    monkeypatch.setattr(cli, "get_relay", lambda *a, **kw: pytest.fail("取消後の送信"))
    assert cli.main(["dispatch", "t1", "--participant", "codex", "--tier", "1",
                     "--root", str(tmp_path)]) == 1
    assert json.loads(tp.last_result.read_text())["reason"] == "cancelled"


@pytest.mark.parametrize("inv", ["../outside", "bad/id", ""])
def test_invalid_cancel_id_returns_structured_failure(tmp_path, capsys, inv):
    tp = topic(tmp_path)
    assert cli.main(["cancel", "t1", "--invocation", inv, "--root", str(tmp_path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "invalid-invocation"
    assert result == json.loads(tp.last_result.read_text())


def test_stdout_only_reply_still_collects(tmp_path, monkeypatch):
    tp = topic(tmp_path)
    capture = handoff.capture_request

    def capture_with_reply(paths, inv, text):
        result = capture(paths, inv, text)
        reply(paths, inv)
        return result

    monkeypatch.setattr(handoff, "capture_request", capture_with_reply)
    assert cli.main(["dispatch", "t1", "--participant", "codex", "--no-clipboard",
                     "--timeout", "0", "--root", str(tmp_path)]) == 0
    assert next(iter(Journal.load(tp).data["invocations"].values()))["state"] == "merged"


def test_pending_recovers_committed_reply_when_scratch_is_missing(tmp_path, monkeypatch):
    tp = topic(tmp_path)
    journal = Journal.load(tp)
    inv = journal.new_invocation("codex", 1)
    reply(tp, inv)
    original = Journal.set_state

    def crash(self, inv_id, state, detail=""):
        if state == "merged":
            raise RuntimeError("採用直後の停止")
        return original(self, inv_id, state, detail)

    with monkeypatch.context() as patch:
        patch.setattr(Journal, "set_state", crash)
        with pytest.raises(RuntimeError, match="採用直後"):
            watcher.collect(tp, journal, inv, "codex", timeout_s=0)
    (tp.scratch / f"{inv}.json").unlink()
    assert cli.main(["collect-pending", "t1", "--root", str(tmp_path)]) == 0
    assert Journal.load(tp).is_merged(inv)
    assert json.loads(tp.last_result.read_text())["merged"] == [inv]


def test_pending_skips_busy_invocation_and_collects_other_ready_reply(tmp_path):
    tp = topic(tmp_path)
    journal = Journal.load(tp)
    busy = journal.new_invocation("codex", 1)
    ready = journal.new_invocation("codex", 1)
    reply(tp, busy)
    reply(tp, ready)
    finished = threading.Event()
    results = []

    def scan():
        try:
            results.append(cli.main(["collect-pending", "t1", "--root", str(tmp_path)]))
        finally:
            finished.set()

    with watcher._invocation_lock(tp, busy):
        thread = threading.Thread(target=scan)
        thread.start()
        completed_while_busy = finished.wait(10)
    thread.join(10)
    assert completed_while_busy, "busy席がtimeout=0のscanをブロックした"
    assert results == [2]
    receipt = json.loads(tp.last_result.read_text())
    assert receipt["pending"] == [busy]
    assert receipt["merged"] == [ready]
    assert busy not in receipt["attempted"]
    assert cli.main(["collect-pending", "t1", "--root", str(tmp_path)]) == 0
    assert Journal.load(tp).is_merged(busy)
