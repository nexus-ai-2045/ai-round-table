"""relay 層のテスト。

relay_codex は **実 CLI を起動しない**。os.pipe() の疑似 stdio に台本つきの
fake app-server を差し込み、ワイヤー形式・id 相関・handshake・完了判定だけを
検証する (CI で回るようにするため)。
"""
import json
import os
import subprocess

import pytest

from roundtable.relay import ClipboardRelay, Relay, RelayError, get_relay
from roundtable.relay_codex import (
    CodexAppServer,
    CodexAppServerError,
    CodexRelay,
    Transport,
    find_codex_argv,
)


# ---------------------------------------------------------------- fake stdio
class FakeAppServer:
    """Transport の writer として振る舞う疑似 app-server。

    Transport が 1 行書くたびに handler(msg, self) を呼び、handler は返信
    メッセージの list を返す。返信は pipe 経由で Transport の reader に流れる。
    """

    def __init__(self, handler):
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "r", encoding="utf-8")  # Transport が読む側
        self._sink = os.fdopen(write_fd, "w", encoding="utf-8", newline="\n")
        self._handler = handler
        self._buf = ""
        self.received: list[dict] = []
        self.raw_received: list[str] = []

    # --- Transport から見た writer 面 ---
    def write(self, s: str) -> None:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            self.raw_received.append(line)
            msg = json.loads(line)
            self.received.append(msg)
            for reply in self._handler(msg, self) or []:
                self.emit(reply)

    def flush(self) -> None:
        pass

    # --- server 側から通知/レスポンスを流す ---
    def emit(self, obj: dict) -> None:
        self._sink.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._sink.flush()

    def close_stdout(self) -> None:
        """app-server 側が落ちた状況 (stdout EOF) を作る。"""
        self._sink.close()

    def methods(self) -> list[str]:
        return [m.get("method") for m in self.received]

    def by_method(self, method: str) -> dict:
        for m in self.received:
            if m.get("method") == method:
                return m
        raise AssertionError(f"{method} が送られていない: {self.methods()}")


def make_transport(handler) -> tuple[Transport, FakeAppServer]:
    fake = FakeAppServer(handler)
    return Transport(fake, fake.stdout), fake


def scripted_handler(msg, fake):
    """spike 実測どおりに応答する台本 app-server。"""
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        return [{"id": rid, "result": {"codexHome": "C:/Users/x/.codex", "platformOs": "windows"}}]
    if method == "thread/start":
        return [
            {"id": rid, "result": {"thread": {"id": "thr_1"}, "approvalPolicy": "never"}},
            {"method": "thread/started", "params": {"thread": {"id": "thr_1"}}},
        ]
    if method == "thread/name/set":
        return [
            {"id": rid, "result": {}},
            {
                "method": "thread/name/updated",
                "params": {"threadId": "thr_1", "threadName": msg["params"]["name"]},
            },
        ]
    if method == "thread/read":
        return [
            {
                "id": rid,
                "result": {
                    "thread": {
                        "id": msg["params"]["threadId"],
                        "status": "notLoaded",
                        "turns": [] if msg["params"]["includeTurns"] else None,
                    }
                },
            }
        ]
    if method == "thread/archive":
        return [
            {"id": rid, "result": {}},
            {"method": "thread/archived", "params": {"threadId": msg["params"]["threadId"]}},
        ]
    if method == "turn/start":
        return [
            {"id": rid, "result": {"turn": {"id": "turn_1", "status": "inProgress"}}},
            {"method": "turn/started", "params": {"threadId": "thr_1", "turn": {"id": "turn_1"}}},
            {"method": "item/agentMessage/delta", "params": {"delta": "書き"}},
            {"method": "item/agentMessage/delta", "params": {"delta": "ました。"}},
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thr_1",
                    "turn": {"id": "turn_1", "status": "completed", "error": None},
                },
            },
        ]
    return []


def make_server(handler=scripted_handler) -> tuple[CodexAppServer, FakeAppServer]:
    transport, fake = make_transport(handler)
    server = CodexAppServer(transport)
    server.start()
    return server, fake


# ------------------------------------------------------------ Tier3 / factory
def test_clipboard_relay_satisfies_protocol():
    assert isinstance(ClipboardRelay(), Relay)


def test_clipboard_relay_sends_text(monkeypatch):
    sent = []
    monkeypatch.setattr("roundtable.packet.to_clipboard", sent.append)
    relay = get_relay(3, "codex")
    assert isinstance(relay, ClipboardRelay)
    relay.send({"participant": "codex", "topic": "t1"}, "packet 本文")
    relay.close()  # no-op / 冪等
    relay.close()
    assert sent == ["packet 本文"]


def test_clipboard_relay_wraps_failure_as_relay_error(monkeypatch):
    def boom(_text):
        raise subprocess.CalledProcessError(1, "clip.exe")

    monkeypatch.setattr("roundtable.packet.to_clipboard", boom)
    with pytest.raises(RelayError):
        ClipboardRelay().send({}, "x")


def test_get_relay_tier1_codex_does_not_spawn():
    relay = get_relay(1, "codex")  # __init__ でプロセスを起こさないこと
    assert isinstance(relay, CodexRelay)
    assert isinstance(relay, Relay)
    relay.close()  # server 未確保でも冪等


def test_get_relay_rejects_unmeasured_paths():
    with pytest.raises(NotImplementedError):
        get_relay(2, "codex")  # Tier2 は CEO 明示承認が要る
    with pytest.raises(NotImplementedError):
        get_relay(1, "grok")  # 未 spike の席
    with pytest.raises(ValueError):
        get_relay(9, "codex")


# ------------------------------------------------------------------ Transport
def test_transport_framing_has_no_jsonrpc_field():
    transport, fake = make_transport(scripted_handler)
    transport.start()
    transport.request("initialize", {"clientInfo": {"name": "x"}}, timeout=5)
    transport.notify("initialized")
    assert all("jsonrpc" not in m for m in fake.received)
    assert fake.received[-1] == {"method": "initialized"}  # 裸通知 (params なし)


def test_transport_correlates_response_by_id():
    def handler(msg, fake):
        if msg.get("method") == "slow":
            return []  # 後で手動応答する
        return [{"id": msg["id"], "result": {"echo": msg["method"]}}]

    transport, fake = make_transport(handler)
    transport.start()
    # 無関係な通知が割り込んでもレスポンスの取り違えが起きないこと
    fake.emit({"method": "warning", "params": {"message": "noise"}})
    assert transport.request("ping", timeout=5) == {"echo": "ping"}
    assert transport.request("pong", timeout=5) == {"echo": "pong"}


def test_transport_request_raises_on_error_response():
    def handler(msg, fake):
        return [{"id": msg["id"], "error": {"code": -32001, "message": "Server overloaded"}}]

    transport, _ = make_transport(handler)
    transport.start()
    with pytest.raises(CodexAppServerError, match="Server overloaded"):
        transport.request("thread/start", timeout=5)


def test_transport_request_times_out():
    transport, _ = make_transport(lambda msg, fake: [])
    transport.start()
    with pytest.raises(CodexAppServerError, match="0.2s"):
        transport.request("thread/start", timeout=0.2)


def test_transport_wakes_pending_on_eof():
    def handler(msg, fake):
        fake.close_stdout()  # app-server が応答前に落ちた
        return []

    transport, _ = make_transport(handler)
    transport.start()
    with pytest.raises(CodexAppServerError, match="EOF"):
        transport.request("turn/start", timeout=5)


def test_transport_ignores_non_json_lines():
    def handler(msg, fake):
        fake._sink.write("起動ログ (JSON ではない)\n")
        fake._sink.flush()
        return [{"id": msg["id"], "result": {"ok": True}}]

    transport, _ = make_transport(handler)
    transport.start()
    assert transport.request("initialize", timeout=5) == {"ok": True}


# ------------------------------------------------------------- CodexAppServer
def test_initialize_sends_bare_initialized_notification():
    server, fake = make_server()
    result = server.initialize(timeout=5)
    assert result["platformOs"] == "windows"
    assert server.initialized
    assert fake.methods() == ["initialize", "initialized"]
    assert "params" not in fake.received[1]


def test_start_thread_sends_hyphen_sandbox_and_names_thread():
    server, fake = make_server()
    server.initialize(timeout=5)
    thread_id = server.start_thread("C:/repo", name="rt-spike-codex", timeout=5)
    assert thread_id == "thr_1"
    params = fake.by_method("thread/start")["params"]
    assert params["sandbox"] == "workspace-write"  # hyphen 表記で送る (spike 実測)
    assert params["approvalPolicy"] == "never"
    assert params["cwd"] == "C:/repo"
    assert fake.by_method("thread/name/set")["params"] == {
        "threadId": "thr_1",
        "name": "rt-spike-codex",
    }


def test_start_thread_without_id_raises():
    def handler(msg, fake):
        return [{"id": msg["id"], "result": {"thread": {}}}]

    server, _ = make_server(handler)
    with pytest.raises(CodexAppServerError, match="thread.id"):
        server.start_thread("C:/repo", timeout=5)


def test_turn_start_and_completion_signal():
    server, fake = make_server()
    server.initialize(timeout=5)
    server.start_thread("C:/repo", timeout=5)
    turn_id = server.start_turn("thr_1", "packet 本文", timeout=5)
    assert turn_id == "turn_1"
    assert fake.by_method("turn/start")["params"]["input"] == [
        {"type": "text", "text": "packet 本文"}
    ]
    turn = server.wait_turn(turn_id, timeout=5)
    assert turn["status"] == "completed"
    assert server.agent_message() == "書きました。"


def test_read_then_archive_thread_uses_exact_target():
    server, fake = make_server()
    server.initialize(timeout=5)
    thread = server.read_thread("thr_1", include_turns=True, timeout=5)
    assert thread == {"id": "thr_1", "status": "notLoaded", "turns": []}
    server.archive_thread("thr_1", timeout=5)
    assert fake.by_method("thread/read")["params"] == {
        "threadId": "thr_1",
        "includeTurns": True,
    }
    assert fake.by_method("thread/archive")["params"] == {"threadId": "thr_1"}


def test_read_thread_rejects_mismatched_response():
    def handler(msg, _fake):
        return [{"id": msg["id"], "result": {"thread": {"id": "other"}}}]

    server, _ = make_server(handler)
    with pytest.raises(CodexAppServerError, match="対象 thread"):
        server.read_thread("thr_1", timeout=5)


def test_wait_turn_returns_none_on_timeout():
    def handler(msg, fake):
        return [{"id": msg["id"], "result": {"turn": {"id": "turn_x"}}}]

    server, _ = make_server(handler)
    turn_id = server.start_turn("thr_1", "x", timeout=5)
    assert server.wait_turn(turn_id, timeout=0.2) is None


def test_server_originated_request_is_recorded_not_auto_answered():
    server, fake = make_server()
    server.initialize(timeout=5)
    fake.emit({"id": 99, "method": "item/commandExecution/requestApproval", "params": {}})
    server.start_thread("C:/repo", timeout=5)  # 同期点: これが返る頃には配送済み
    assert server.server_requests == ["item/commandExecution/requestApproval"]
    # 自動承認しない = クライアントから応答を返していないこと
    assert all("result" not in m for m in fake.received)


def test_close_is_idempotent_and_calls_terminate():
    calls = []
    transport, _ = make_transport(scripted_handler)
    server = CodexAppServer(transport, terminate=lambda: calls.append("t"))
    server.start()
    server.close()
    server.close()
    assert calls == ["t"]


# ------------------------------------------------------------------ CodexRelay
def test_codex_relay_send_runs_handshake_thread_and_turn():
    server, fake = make_server()
    relay = CodexRelay(server_factory=lambda: server)
    relay.send({"participant": "codex", "topic": "spike", "cwd": "C:/repo"}, "packet 本文")
    assert fake.methods()[:5] == [
        "initialize",
        "initialized",
        "thread/start",
        "thread/name/set",
        "turn/start",
    ]
    assert fake.by_method("thread/name/set")["params"]["name"] == "rt-spike-codex"
    assert relay.thread_ref == "thr_1"
    relay.close()
    assert relay.last_turn["status"] == "completed"


def test_codex_relay_reuses_thread_and_server_across_sends():
    server, fake = make_server()
    relay = CodexRelay(server_factory=lambda: server)
    seat = {"participant": "codex", "topic": "spike"}
    relay.send(seat, "1 通目")
    relay.send(seat, "2 通目")
    assert fake.methods().count("thread/start") == 1
    assert fake.methods().count("initialize") == 1
    assert fake.methods().count("turn/start") == 2
    relay.close()


def test_codex_relay_keeps_thread_ref_after_close():
    server, _ = make_server()
    relay = CodexRelay(server_factory=lambda: server)
    relay.send({"participant": "codex", "topic": "spike"}, "x")
    relay.close()
    assert relay.thread_ref == "thr_1"  # seats.json 記録用に残す


def test_codex_relay_rejects_other_participants():
    relay = CodexRelay(server_factory=lambda: pytest.fail("spawn してはいけない"))
    with pytest.raises(RelayError):
        relay.send({"participant": "grok", "topic": "spike"}, "x")


def test_codex_relay_resume_is_not_implemented():
    server, _ = make_server()
    relay = CodexRelay(server_factory=lambda: server)
    seat = {"participant": "codex", "topic": "spike", "thread_ref": "thr_old"}
    with pytest.raises(NotImplementedError, match="thread/resume"):
        relay.send(seat, "x")
    relay.close()


def test_codex_relay_closes_server_when_initialize_fails():
    closed = []

    def handler(msg, fake):
        return [{"id": msg["id"], "error": {"message": "Not initialized"}}]

    server, _ = make_server(handler)
    server.close = lambda: closed.append("closed")
    relay = CodexRelay(server_factory=lambda: server)
    with pytest.raises(CodexAppServerError):
        relay.send({"participant": "codex", "topic": "spike"}, "x")
    assert closed == ["closed"]  # 半端に生きたプロセスを残さない


def test_find_codex_argv_honours_env_override(tmp_path, monkeypatch):
    fake_bin = tmp_path / "codex.cmd"
    fake_bin.write_text("", encoding="utf-8")
    monkeypatch.setenv("ROUNDTABLE_CODEX_BIN", str(fake_bin))
    assert find_codex_argv() == [str(fake_bin), "app-server", "--stdio"]


def test_find_codex_argv_raises_when_nothing_found(monkeypatch):
    monkeypatch.delenv("ROUNDTABLE_CODEX_BIN", raising=False)
    monkeypatch.setattr("roundtable.relay_codex.shutil.which", lambda _n: None)
    monkeypatch.setattr("roundtable.relay_codex._KNOWN_CODEX_PATHS", ())
    with pytest.raises(CodexAppServerError, match="codex"):
        find_codex_argv()


def test_codex_relay_close_without_send_is_safe():
    relay = CodexRelay(server_factory=lambda: pytest.fail("spawn してはいけない"))
    relay.close()
    relay.close()
