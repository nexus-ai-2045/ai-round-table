"""Tier1 (codex app-server) の安全性ハードニング検証。

いずれも「実 CLI を起動せずに、危険な既定に戻ったら落ちる」ことを目的にする。
実プロセスを起こさないので CI で安全に回せる。
"""
import json
import subprocess
import threading
from pathlib import Path

import pytest

from roundtable import relay as relay_mod
from roundtable import watcher
from roundtable.cli import main
from roundtable.journal import Journal
from roundtable.paths import ensure_topic
from roundtable.relay import DeliveryUnknownError
from roundtable.relay_codex import (
    CodexAppServerRelay,
    RpcResponseError,
    RpcTimeoutError,
)


class _FakeProc:
    """Popen の最小モック。terminate/kill/wait の呼ばれ方を記録する。"""

    def __init__(self, alive_after_terminate=False):
        self.pid = 4242
        self.stdin = None
        self.stdout = []
        self.calls = []
        self._alive = True
        self._alive_after_terminate = alive_after_terminate

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.calls.append("terminate")
        if not self._alive_after_terminate:
            self._alive = False

    def kill(self):
        self.calls.append("kill")
        self._alive = False

    def wait(self, timeout=None):
        self.calls.append("wait")
        if self._alive:
            raise subprocess.TimeoutExpired("codex", timeout or 0)
        return 0


def test_spawn_forces_utf8(monkeypatch):
    """encoding 未指定だと Windows は cp932 になり日本語 packet が壊れる。"""
    seen = {}

    def fake_popen(argv, **kwargs):
        seen.update(kwargs)
        seen["argv"] = argv
        raise OSError("spawn は行わない (引数だけ検証する)")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    relay = CodexAppServerRelay(cwd="/tmp/topic")
    with pytest.raises(Exception):
        relay._ensure()
    assert seen["encoding"] == "utf-8"
    assert seen["cwd"] == "/tmp/topic"  # spawn 時の cwd も席スコープに寄せる
    # POSIX ではプロセスグループを分離する (木ごと殺せるようにする)
    import os as _os
    assert ("start_new_session" in seen) == (_os.name != "nt")


def test_thread_start_pins_sandbox_and_never_ephemeral(monkeypatch):
    """sandbox / approvalPolicy を明示し、ephemeral を使わない。

    省略するとサーバ既定 (実測で danger-full-access になりうる) に従ってしまい、
    ephemeral にすると会話がアプリに残らず「席を CEO が読める」要件が壊れる。
    """
    sent = []
    relay = CodexAppServerRelay(cwd="/tmp/topic")
    monkeypatch.setattr(relay, "_ensure", lambda: None)

    def fake_rpc(method, params, timeout=15):
        sent.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thr_1"}}
        return {}

    monkeypatch.setattr(relay, "_rpc", fake_rpc)
    relay.send({"topic": "t1"}, "こんにちは")

    start = next(params for method, params in sent if method == "thread/start")
    assert start["sandbox"] == "workspace-write"
    assert start["approvalPolicy"] == "never"
    assert "ephemeral" not in start
    assert start["cwd"] == "/tmp/topic"


def test_close_reaps_tree_even_when_terminate_succeeds(monkeypatch):
    """親が素直に終わっても木を掃除する (TerminateProcess は子孫を殺さない)。"""
    proc = _FakeProc(alive_after_terminate=False)
    reaped = []
    monkeypatch.setattr(
        "roundtable.relay_codex._ProcessTree.close",
        lambda self: reaped.append(True),
    )
    relay = CodexAppServerRelay()
    relay._proc = proc
    relay.close()
    assert reaped, "terminate 成功経路でも木の回収を通ること"


def test_close_kills_process_tree_when_terminate_fails(monkeypatch):
    """terminate で死ななければ子孫ごと kill する (孤児プロセス防止)。"""
    proc = _FakeProc(alive_after_terminate=True)
    killed = {}

    monkeypatch.setattr(
        "roundtable.relay_codex._ProcessTree.kill_tree",
        lambda self, pid: killed.setdefault("pid", pid),
    )
    relay = CodexAppServerRelay()
    relay._proc = proc
    relay.close()

    assert "terminate" in proc.calls
    assert killed["pid"] == proc.pid  # ツリー kill が呼ばれている
    assert "kill" in proc.calls
    assert relay._proc is None


def test_close_kills_tree_when_windows_job_is_unmanaged():
    """Job Object が成立しない環境では親の正常終了を待つ前に taskkill 相当を通す。"""
    proc = _FakeProc(alive_after_terminate=False)

    class UnmanagedTree:
        managed = False

        def __init__(self):
            self.killed = []
            self.closed = False

        def kill_tree(self, pid):
            self.killed.append(pid)

        def close(self):
            self.closed = True

    tree = UnmanagedTree()
    relay = CodexAppServerRelay()
    relay._proc = proc
    relay._tree = tree
    relay.close()
    assert tree.killed == [proc.pid]
    assert tree.closed


def test_close_is_idempotent():
    relay = CodexAppServerRelay()
    relay.close()
    relay.close()  # 二度目でも例外を出さない


def test_turn_timeout_is_delivery_unknown(monkeypatch):
    relay = CodexAppServerRelay(cwd="/tmp/topic")
    monkeypatch.setattr(relay, "_ensure", lambda: None)

    def fake_rpc(method, params, timeout=15):
        if method == "thread/start":
            return {"thread": {"id": "thr_1"}}
        if method == "turn/start":
            raise RpcTimeoutError(method, timeout)
        return {}

    monkeypatch.setattr(relay, "_rpc", fake_rpc)
    with pytest.raises(DeliveryUnknownError):
        relay.send({"topic": "t1"}, "same invocation")


def test_turn_transport_error_is_delivery_unknown(monkeypatch):
    """request書込中の切断も受理済みを否定できないので自動再送しない。"""
    relay = CodexAppServerRelay(cwd="/tmp/topic")
    monkeypatch.setattr(relay, "_ensure", lambda: None)

    def fake_rpc(method, params, timeout=15):
        if method == "thread/start":
            return {"thread": {"id": "thr_1"}}
        if method == "turn/start":
            raise BrokenPipeError("server closed after read")
        return {}

    monkeypatch.setattr(relay, "_rpc", fake_rpc)
    with pytest.raises(DeliveryUnknownError):
        relay.send({"topic": "t1"}, "same invocation")


def test_turn_rpc_rejection_remains_definitive_relay_error(monkeypatch):
    """明示的なJSON-RPC拒否は送達不明ではなく、Tier3縮退可能な確定失敗。"""
    relay = CodexAppServerRelay(cwd="/tmp/topic")
    monkeypatch.setattr(relay, "_ensure", lambda: None)

    def fake_rpc(method, params, timeout=15):
        if method == "thread/start":
            return {"thread": {"id": "thr_1"}}
        if method == "turn/start":
            raise RpcResponseError(method, {"code": -32602, "message": "invalid params"})
        return {}

    monkeypatch.setattr(relay, "_rpc", fake_rpc)
    with pytest.raises(RpcResponseError):
        relay.send({"topic": "t1"}, "same invocation")


def test_stale_thread_ref_is_recreated_once(monkeypatch):
    relay = CodexAppServerRelay(cwd="/tmp/topic")
    monkeypatch.setattr(relay, "_ensure", lambda: None)
    sent = []

    def fake_rpc(method, params, timeout=15):
        sent.append((method, dict(params)))
        if method == "thread/resume":
            raise RpcResponseError(method, {"message": "thread not found"})
        if method == "thread/start":
            return {"thread": {"id": "thr_new"}}
        return {}

    monkeypatch.setattr(relay, "_rpc", fake_rpc)
    seat = {"topic": "t1", "thread_ref": "thr_dead"}
    assert relay.send(seat, "packet") == "tier1-sent"
    assert seat["thread_ref"] == "thr_new"
    assert [m for m, _ in sent].count("thread/resume") == 1
    assert [m for m, _ in sent].count("thread/start") == 1


def test_resume_timeout_stops_without_starting_new_seat(monkeypatch):
    relay = CodexAppServerRelay(cwd="/tmp/topic")
    monkeypatch.setattr(relay, "_ensure", lambda: None)
    sent = []

    def fake_rpc(method, params, timeout=15):
        sent.append(method)
        if method == "thread/resume":
            raise RpcTimeoutError(method, timeout)
        return {}

    monkeypatch.setattr(relay, "_rpc", fake_rpc)
    seat = {"topic": "t1", "thread_ref": "thr_existing"}
    with pytest.raises(DeliveryUnknownError):
        relay.send(seat, "packet")
    assert seat["thread_ref"] == "thr_existing"
    assert sent == ["thread/resume"]


def test_rpc_and_send_calls_are_serialized():
    relay = CodexAppServerRelay(cwd="/tmp/topic")
    assert isinstance(relay._rpc_lock, type(threading.Lock()))
    assert isinstance(relay._send_lock, type(threading.Lock()))


def test_send_lock_functionally_serializes_calls(monkeypatch):
    relay = CodexAppServerRelay(cwd="/tmp/topic")
    guard = threading.Lock()
    active = 0
    peak = 0

    def fake_send_locked(seat, text):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        threading.Event().wait(0.02)
        with guard:
            active -= 1
        return text

    monkeypatch.setattr(relay, "_send_locked", fake_send_locked)

    start = threading.Barrier(3)
    results = []

    def worker(text):
        start.wait()
        results.append(relay.send({}, text))

    threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == ["0", "1"]
    assert peak == 1


def test_stderr_drain_consumes_large_stream():
    relay = CodexAppServerRelay(cwd="/tmp/topic")

    class Proc:
        stderr = (f"diagnostic {i}\n" for i in range(100_000))

    relay._proc = Proc()
    thread = threading.Thread(target=relay._drain_stderr)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "stderr reader が大量出力をdrainできない"


def test_cli_preserves_thread_ref_for_delivery_unknown(tmp_path, monkeypatch):
    """送達不明の席参照と機械分類を保存し、再実行による席分裂を防ぐ。"""
    class AmbiguousRelay:
        tier = 1

        def send(self, seat, text):
            seat["thread_ref"] = "thr_ambiguous"
            raise DeliveryUnknownError("turn may already be running")

        def close(self):
            pass

    monkeypatch.setattr("roundtable.cli.get_relay", lambda *a, **k: AmbiguousRelay())
    main(["new-topic", "t1", "--topic", "X", "--participants", "codex",
          "--root", str(tmp_path)])
    rc = main(["dispatch", "t1", "--participant", "codex", "--tier", "1",
               "--timeout", "0.1", "--root", str(tmp_path)])

    tp = ensure_topic(tmp_path, "t1")
    result = json.loads(tp.last_result.read_text(encoding="utf-8"))
    seats = relay_mod.load_seats(tp)
    assert rc == 1
    assert result["reason"] == "delivery-unknown"
    assert result["thread_ref"] == "thr_ambiguous"
    assert seats["rt/t1/codex"]["thread_ref"] == "thr_ambiguous"
    assert "delivery-unknown" in json.loads(
        tp.journal.read_text(encoding="utf-8")
    )["invocations"][result["invocation"]]["detail"]


def test_delivery_unknown_can_collect_late_output(tmp_path, monkeypatch):
    """送達不明は再送を止めるが、既送信だった成果物の回収は妨げない。"""
    class AmbiguousRelay:
        tier = 1

        def send(self, seat, text):
            seat["thread_ref"] = "thr_ambiguous"
            raise DeliveryUnknownError("turn may already be running")

        def close(self):
            pass

    monkeypatch.setattr("roundtable.cli.get_relay", lambda *a, **k: AmbiguousRelay())
    main(["new-topic", "t1", "--topic", "X", "--participants", "codex", "--root", str(tmp_path)])
    assert main(["dispatch", "t1", "--participant", "codex", "--tier", "1", "--timeout", "0.1", "--root", str(tmp_path)]) == 1

    tp = ensure_topic(tmp_path, "t1")
    result = json.loads(tp.last_result.read_text(encoding="utf-8"))
    inv = result["invocation"]
    (tp.scratch / f"{inv}.json").write_text(
        json.dumps(
            {
                "invocation_id": inv,
                "participant": "codex",
                "opinion": "後着した成果物",
                "claims": [{"claim": "A", "evidence_type": "argument", "evidence": "B"}],
            }
        ),
        encoding="utf-8",
    )

    assert watcher.collect(tp, Journal.load(tp), inv, "codex", timeout_s=1)["ok"]
    assert Journal.load(tp).is_merged(inv)


@pytest.mark.parametrize("async_dispatch", [False, True])
def test_delivery_unknown_waits_for_late_output_before_closing(
    tmp_path, monkeypatch, async_dispatch
):
    """送達不明ならCLIを維持し、後着成果物を回収してから席を閉じる。"""
    main([
        "new-topic", "t1", "--topic", "X", "--participants", "codex",
        "--root", str(tmp_path),
    ])
    tp = ensure_topic(tmp_path, "t1")

    class AmbiguousRelay:
        tier = 1

        def __init__(self):
            self.timer = None
            self.closed_after_output = False

        def send(self, seat, text):
            seat["thread_ref"] = "thr_ambiguous"
            inv = next(iter(Journal.load(tp).data["invocations"]))

            def emit_output():
                (tp.scratch / f"{inv}.json").write_text(
                    json.dumps(
                        {
                            "invocation_id": inv,
                            "participant": "codex",
                            "opinion": "送達不明後に完了した成果物",
                            "claims": [
                                {
                                    "claim": "A",
                                    "evidence_type": "argument",
                                    "evidence": "B",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            self.timer = threading.Timer(0.05, emit_output)
            self.timer.start()
            raise DeliveryUnknownError("turn may already be running")

        def close(self):
            self.closed_after_output = any(tp.scratch.glob("*.json"))
            if self.timer is not None:
                self.timer.cancel()

    relay = AmbiguousRelay()
    monkeypatch.setattr("roundtable.cli.get_relay", lambda *a, **k: relay)
    args = [
        "dispatch", "t1", "--participant", "codex", "--tier", "1",
        "--timeout", "1", "--root", str(tmp_path),
    ]
    if async_dispatch:
        args.append("--async")

    assert main(args) == 0
    assert relay.closed_after_output
    assert Journal.load(tp).is_merged(next(iter(Journal.load(tp).data["invocations"])))


def test_protocol_lists_delivery_unknown_as_machine_reason():
    protocol = (Path(__file__).parents[1] / "docs" / "PROTOCOL.md").read_text(
        encoding="utf-8"
    )
    assert "| `delivery-unknown` |" in protocol
    assert "`delivery-unknown`" in protocol.split("### CLI の exit code", 1)[1]


def test_get_relay_passes_cwd_to_tier1(monkeypatch):
    captured = {}

    class _Spy:
        def __init__(self, cwd=None):
            captured["cwd"] = cwd

        def send(self, seat, text):
            return "spy"

        def close(self):
            pass

    monkeypatch.setattr("roundtable.relay_codex.CodexAppServerRelay", _Spy)
    relay_mod.get_relay("codex", tier=1, allow_fallback=False, cwd="/tmp/topic")
    assert captured["cwd"] == "/tmp/topic"


def test_cli_scopes_cwd_to_topic_dir(tmp_path, monkeypatch):
    """root ではなく議題ディレクトリを渡す (他議題の journal を書かせない)。"""
    seen = {}
    real_get_relay = relay_mod.get_relay

    def spy_get_relay(participant, tier=3, allow_fallback=True, cwd=None):
        seen["cwd"] = cwd
        return real_get_relay(participant, tier=3, allow_fallback=allow_fallback)

    monkeypatch.setattr("roundtable.cli.get_relay", spy_get_relay)
    monkeypatch.setattr("roundtable.relay_tier3.Tier3Relay.send", lambda self, s, t: "tier3")

    main(["new-topic", "t1", "--topic", "X", "--participants", "codex",
          "--root", str(tmp_path)])
    main(["dispatch", "t1", "--participant", "codex", "--timeout", "0.1",
          "--root", str(tmp_path)])

    assert seen["cwd"] == str(ensure_topic(tmp_path, "t1").root.resolve())
    assert seen["cwd"] != str(tmp_path)


def test_cli_cwd_is_absolute_for_relative_root(tmp_path, monkeypatch):
    """相対 --root でも絶対パスを渡す。spawn 先で二重解決になるのを防ぐ (P2)。"""
    import os

    seen = {}
    real_get_relay = relay_mod.get_relay

    def spy_get_relay(participant, tier=3, allow_fallback=True, cwd=None):
        seen["cwd"] = cwd
        return real_get_relay(participant, tier=3, allow_fallback=allow_fallback)

    monkeypatch.setattr("roundtable.cli.get_relay", spy_get_relay)
    monkeypatch.setattr("roundtable.relay_tier3.Tier3Relay.send", lambda self, s, t: "tier3")
    monkeypatch.chdir(tmp_path)

    main(["new-topic", "t1", "--topic", "X", "--participants", "codex", "--root", "."])
    main(["dispatch", "t1", "--participant", "codex", "--timeout", "0.1", "--root", "."])

    assert os.path.isabs(seen["cwd"]), f"相対パスが漏れている: {seen['cwd']}"


def test_timeouts_have_margin_over_measured_latency():
    """実測値より十分大きい timeout を保つ (実往復スモークで踏んだ回帰の再発防止)。

    2026-08-06 実測 (Windows / codex-cli 0.144.6): initialize 6.66s / thread/start 20.9s。
    元の既定は initialize=8s / thread/start=5s で、**thread/start が必ず timeout し
    Tier1 は 100% Tier3 に縮退していた**。fake stdio のユニットテストは即答するため
    この穴を検出できない — 数値そのものを不変条件として固定する。
    """
    from roundtable import relay_codex as rc

    assert rc.TIMEOUT_INITIALIZE >= 30, "実測 6.66s に対し余裕がない"
    assert rc.TIMEOUT_THREAD_START >= 240, "実測 20.9-58.4s + 負荷時 120s 超に対し余裕がない"
    assert rc.TIMEOUT_TURN_START >= 60
