"""Grok Tier1 (`grok agent --no-leader stdio` / ACP) の検証。

実 CLI を起動しない: fake stdio (agent 役のスクリプト) を Popen の差し替えで挿す。
守るのは「実測で確認した protocol の形」と「実測で踏んだ事故が再発しないこと」:

- 許可要求 (`session/request_permission`) に答えないと席が永久に固まる
- agent 側 id (実測 0 起点) が client 側 id と衝突しても応答と取り違えない
- 2 プロセス目は `session/load` してから prompt する (席が分裂しない)
- timeout が実測レンジより十分大きい (fake は即答するのでここは数値で固定するしかない)
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time

import pytest

from roundtable import relay as relay_mod
from roundtable import relay_grok
from roundtable.relay import FallbackRelay, RelayError
from roundtable.relay_grok import GrokAcpRelay
from roundtable.relay_tier3 import Tier3Relay


CAPABILITIES = {
    # 実測の initialize result (raw-grok-p1.jsonl) から必要部分だけ抜いたもの
    "loadSession": True,
    "sessionCapabilities": {"list": {}, "resume": {}, "close": {}},
}

PERMISSION_OPTIONS = [
    {"optionId": "always-allow", "name": "Yes, and don't ask again", "kind": "allow_always"},
    {"optionId": "allow-once", "name": "Yes, proceed", "kind": "allow_once"},
    {"optionId": "reject-once", "name": "No", "kind": "reject_once"},
    {"optionId": "reject-always", "name": "No, and don't run bash", "kind": "reject_always"},
]


class FakeAgent:
    """ACP agent 役。client から来た行を解釈し、stdout キューへ行を積む。"""

    def __init__(
        self,
        *,
        capabilities: dict | None = None,
        permission_options: list | None = None,
        permission_request_id: int = 0,
        session_load_error: dict | None = None,
        extra_inbound: dict | None = None,
        prompt_error: dict | None = None,
    ):
        self.out: queue.Queue[str | None] = queue.Queue()
        self.received: list[dict] = []          # client -> agent
        self.client_responses: list[dict] = []  # client -> agent の応答 (inbound への返事)
        self.capabilities = CAPABILITIES if capabilities is None else capabilities
        self.permission_options = permission_options
        self.permission_request_id = permission_request_id
        self.session_load_error = session_load_error
        self.extra_inbound = extra_inbound
        # 許可に答えた後で turn が失敗する席 (縮退の直前に許可だけ出した状態) を作る
        self.prompt_error = prompt_error
        self._pending_prompt: dict | None = None

    # --- 出力 ---
    def emit(self, obj: dict) -> None:
        self.out.put(json.dumps(obj, ensure_ascii=False) + "\n")

    def _reply(self, msg: dict, result: dict) -> None:
        self.emit({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    # --- 入力 ---
    def handle(self, line: str) -> None:
        msg = json.loads(line)
        if msg.get("method") is None:
            # inbound request への client の応答
            self.client_responses.append(msg)
            if self._pending_prompt is not None:
                pending, self._pending_prompt = self._pending_prompt, None
                if self.prompt_error is not None:
                    self.emit({"jsonrpc": "2.0", "id": pending["id"],
                               "error": self.prompt_error})
                else:
                    self._reply(pending, {"stopReason": "end_turn"})
            return
        self.received.append(msg)
        method = msg["method"]
        if "id" not in msg:
            return  # notification
        if method == "initialize":
            self._reply(msg, {"protocolVersion": 1, "agentCapabilities": self.capabilities})
        elif method == "session/new":
            self._reply(msg, {"sessionId": "019fdc03-e591-7b80-9153-b97f61491ee1"})
        elif method == "session/load":
            if self.session_load_error:
                self.emit({"jsonrpc": "2.0", "id": msg["id"], "error": self.session_load_error})
            else:
                self._reply(msg, {"models": {}})
        elif method == "session/prompt":
            if self.extra_inbound is not None:
                self.emit(self.extra_inbound)
            if self.permission_options is None:
                self._reply(msg, {"stopReason": "end_turn"})
                return
            # 席が許可を求めて停止する。client が答えるまで prompt は返らない
            # (実測の deadlock と同じ形)。
            self._pending_prompt = msg
            self.emit({
                "jsonrpc": "2.0",
                "id": self.permission_request_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "019fdc03-e591-7b80-9153-b97f61491ee1",
                    "toolCall": {"toolCallId": "call-1", "kind": "execute",
                                 "title": "Execute `$tmp = ...`"},
                    "options": self.permission_options,
                },
            })
        else:
            self.emit({"jsonrpc": "2.0", "id": msg["id"],
                       "error": {"code": -32601, "message": method}})


class _FakeStdin:
    def __init__(self, agent: FakeAgent):
        self._agent = agent
        self._buf = ""
        self.closed = False

    def write(self, data: str) -> int:
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._agent.handle(line)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeStdout:
    def __init__(self, agent: FakeAgent):
        self._agent = agent

    def __iter__(self):
        while True:
            item = self._agent.out.get()
            if item is None:
                return
            yield item


class FakeProc:
    """Popen の最小モック (行区切り JSON の往復ができるところまで)。"""

    def __init__(self, agent: FakeAgent, argv=None, kwargs=None, stderr_lines=None):
        self.pid = 4242
        self.agent = agent
        self.argv = argv
        self.kwargs = kwargs or {}
        self.stdin = _FakeStdin(agent)
        self.stdout = _FakeStdout(agent)
        self.stderr = iter(stderr_lines) if stderr_lines else None
        self.returncode = None
        self.calls: list[str] = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        self.returncode = 0
        self.agent.out.put(None)

    def kill(self):
        self.calls.append("kill")
        self.returncode = -9
        self.agent.out.put(None)

    def wait(self, timeout=None):
        self.calls.append("wait")
        if self.returncode is None:
            raise subprocess.TimeoutExpired("grok", timeout or 0)
        return self.returncode


@pytest.fixture
def fake_grok(monkeypatch):
    """Popen を fake に差し替え、Job Object への adopt は無効化する。"""
    monkeypatch.setattr("roundtable.relay_process.ProcessTree.adopt", lambda self, proc: None)
    created: list[FakeProc] = []

    def make(agent: FakeAgent, stderr_lines=None) -> None:
        def fake_popen(argv, **kwargs):
            proc = FakeProc(agent, argv=argv, kwargs=kwargs, stderr_lines=stderr_lines)
            created.append(proc)
            return proc

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

    make.created = created  # type: ignore[attr-defined]
    return make


def _relay(cwd="/tmp/topic", **kw) -> GrokAcpRelay:
    return GrokAcpRelay(binary="grok.exe", cwd=cwd, **kw)


# --- transport ---------------------------------------------------------


def test_spawn_argv_and_encoding(fake_grok):
    """`agent --no-leader stdio` を UTF-8 / shell=False で起こす。

    --no-leader は CEO の TUI が使う共有 backend に相乗りしないため (実測時点で
    TUI が 4 セッション稼働中)。encoding 未指定は Windows で cp932 になり
    日本語 packet が壊れる。
    """
    agent = FakeAgent()
    fake_grok(agent)
    relay = _relay()
    seat: dict = {}
    relay.send(seat, "こんにちは")
    proc = fake_grok.created[0]

    assert proc.argv == ["grok.exe", "agent", "--no-leader", "stdio"]
    assert proc.kwargs["encoding"] == "utf-8"
    assert proc.kwargs["shell"] is False
    assert proc.kwargs["cwd"] == "/tmp/topic"
    import os as _os
    assert ("start_new_session" in proc.kwargs) == (_os.name != "nt")
    relay.close()


def test_japanese_packet_survives_the_wire(fake_grok):
    agent = FakeAgent()
    fake_grok(agent)
    relay = _relay()
    relay.send({}, "議事録スナップショットを読む")
    prompt = next(m for m in agent.received if m["method"] == "session/prompt")
    assert prompt["params"]["prompt"][0]["text"] == "議事録スナップショットを読む"
    relay.close()


def test_stderr_is_drained_and_never_used_as_a_verdict(fake_grok):
    """成功した 4 spawn すべてで fatal に見える ERROR 行が出た (実測)。

    読み捨てないとパイプが埋まって席が止まる。読んだ内容で失敗と判定してもいけない。
    """
    fatal = ("ERROR worker quit with fatal: Transport channel closed, "
             "when Auth(AuthorizationRequired)\n")
    agent = FakeAgent()
    fake_grok(agent, stderr_lines=[fatal])
    relay = _relay()
    label = relay.send({}, "packet")
    assert label.startswith("tier1-prompt:end_turn")  # stderr は判定に効かない
    for _ in range(100):
        if relay.stderr_tail:
            break
        time.sleep(0.01)
    assert relay.stderr_tail and "AuthorizationRequired" in relay.stderr_tail[-1]
    relay.close()


# --- 席の開始 / 再開 ----------------------------------------------------


def test_new_session_stores_session_id_in_thread_ref(fake_grok):
    """grok の sessionId は `thread_ref` に入れる (merge_seats の保護対象が 1 つで済む)。"""
    agent = FakeAgent()
    fake_grok(agent)
    relay = _relay()
    seat: dict = {"participant": "grok"}
    label = relay.send(seat, "packet")

    methods = [m["method"] for m in agent.received]
    assert methods == ["initialize", "session/new", "session/prompt"]
    new = next(m for m in agent.received if m["method"] == "session/new")
    assert new["params"] == {"cwd": "/tmp/topic", "mcpServers": []}
    assert seat["thread_ref"] == "019fdc03-e591-7b80-9153-b97f61491ee1"
    assert label.startswith("tier1-prompt:end_turn")
    relay.close()


def test_thread_ref_is_protected_by_merge_seats():
    """席 id を `thread_ref` に置く判断が、既存の分裂防止と噛み合っていること。"""
    disk = {"rt/t/grok": {"participant": "grok", "thread_ref": "sid-1"}}
    local = {"rt/t/grok": {"participant": "grok", "tier": 1}}
    merged = relay_mod.merge_seats(disk, local)
    assert merged["rt/t/grok"]["thread_ref"] == "sid-1"


def test_second_process_loads_the_existing_session(fake_grok):
    """2 ラウンド目は session/load してから prompt する (新しい席を作らない)。"""
    agent = FakeAgent()
    fake_grok(agent)
    relay = _relay()
    seat = {"participant": "grok", "thread_ref": "sid-existing"}
    relay.send(seat, "packet 2")

    methods = [m["method"] for m in agent.received]
    assert methods == ["initialize", "session/load", "session/prompt"]
    load = next(m for m in agent.received if m["method"] == "session/load")
    assert load["params"] == {
        "cwd": "/tmp/topic", "mcpServers": [], "sessionId": "sid-existing",
    }
    assert seat["thread_ref"] == "sid-existing"
    relay.close()


def test_session_load_is_sent_once_per_process(fake_grok):
    agent = FakeAgent()
    fake_grok(agent)
    relay = _relay()
    seat = {"thread_ref": "sid-existing"}
    relay.send(seat, "a")
    relay.send(seat, "b")
    assert [m["method"] for m in agent.received].count("session/load") == 1
    relay.close()


def test_session_load_error_becomes_relay_error(fake_grok):
    agent = FakeAgent(session_load_error={"code": -32602, "message": "session not found"})
    fake_grok(agent)
    relay = _relay()
    with pytest.raises(RelayError):
        relay.send({"thread_ref": "sid-gone"}, "packet")


# --- capability ゲート --------------------------------------------------


def test_missing_load_session_capability_is_refused(fake_grok):
    """resume できない相手に席を作らない (2 ラウンド目に席が分裂するため)。

    codex の版番号ゲートに相当する判定を、grok では自己申告 capability の実測で行う。
    """
    agent = FakeAgent(capabilities={"sessionCapabilities": {"list": {}}})
    fake_grok(agent)
    relay = _relay()
    with pytest.raises(RelayError, match="loadSession"):
        relay.send({}, "packet")
    assert not [m for m in agent.received if m["method"] == "session/new"]


def test_missing_resume_capability_is_refused(fake_grok):
    agent = FakeAgent(capabilities={"loadSession": True, "sessionCapabilities": {"list": {}}})
    fake_grok(agent)
    relay = _relay()
    with pytest.raises(RelayError, match="resume"):
        relay.send({}, "packet")


# --- 許可要求 (実測の deadlock 対策) ------------------------------------


def test_permission_request_is_answered_so_the_turn_completes(fake_grok):
    """応答しないと席が永久に固まる (2026-08-07 実測: 900s まで一切進まなかった)。"""
    agent = FakeAgent(permission_options=PERMISSION_OPTIONS)
    fake_grok(agent)
    relay = _relay()
    seat: dict = {}
    label = relay.send(seat, "packet")

    assert agent.client_responses, "許可要求に応答していない (席が固まる)"
    assert agent.client_responses[0]["result"] == {
        "outcome": {"outcome": "selected", "optionId": "allow-once"}
    }
    assert label.startswith("tier1-prompt:end_turn")
    relay.close()


def test_permission_never_auto_selects_allow_always(fake_grok):
    """`allow_always` は永続範囲が未実測なので自動選択しない (承認が席を跨がないように)。"""
    agent = FakeAgent(permission_options=PERMISSION_OPTIONS)
    fake_grok(agent)
    relay = _relay()
    relay.send({}, "packet")
    chosen = agent.client_responses[0]["result"]["outcome"]["optionId"]
    assert chosen != "always-allow"
    assert relay_grok.AUTO_ALLOW_KINDS == ("allow_once",)
    relay.close()


def test_permission_decisions_are_recorded_on_the_seat(fake_grok):
    """何を許可したかを席メタに残す (書込境界が OS 強制でない以上、記録が唯一の証跡)。"""
    agent = FakeAgent(permission_options=PERMISSION_OPTIONS)
    fake_grok(agent)
    relay = _relay()
    seat: dict = {}
    relay.send(seat, "packet")
    assert seat["permission_log"] == [
        {"tool": "execute", "decision": "allow-once", "kind": "allow_once"}
    ]
    relay.close()


def test_permission_log_is_reset_per_send(fake_grok):
    """許可要求が 0 件の送信では空になる (前ラウンドの記録を残さない)。

    実測: 2 ラウンド目 (session/load 後) は許可要求が 1 件も来なかった。前回の記録を
    残したままだと「今回も execute を許可した」と誤読される。
    """
    agent = FakeAgent(permission_options=PERMISSION_OPTIONS)
    fake_grok(agent)
    relay = _relay()
    seat: dict = {}
    relay.send(seat, "packet 1")
    assert seat["permission_log"]

    agent.permission_options = None  # 2 回目は許可要求なしで完了する
    relay.send(seat, "packet 2")
    assert seat["permission_log"] == []
    relay.close()


def test_permission_log_is_written_even_when_the_turn_fails(fake_grok):
    """失敗した round でも「今回出した許可」を席メタに残す (レビュー H1)。

    旧実装は `session/prompt` 成功後にしか `seat["permission_log"]` を書かなかった。
    再現 (scratchpad `repro_h1_before.py`): 許可を 1 件出した直後に席が落ちると

        relay.permission_log = [{"tool": "execute", ...}]   ← 実際に出した許可
        seat["permission_log"] = [{"tool": "write", ...}]   ← 前 round の記録

    となり、(a) 縮退した席に現実と無関係な記録が残り、(b) この round で席が
    PowerShell を走らせた事実が消える。監査記録としてどちらも致命的。
    """
    agent = FakeAgent(
        permission_options=PERMISSION_OPTIONS,
        prompt_error={"code": -32000, "message": "turn aborted"},
    )
    fake_grok(agent)
    relay = _relay()
    seat: dict = {
        "participant": "grok", "thread_ref": "sid-1",
        # 前 round に別の tool を許可した記録
        "permission_log": [{"tool": "write", "decision": "allow-once", "kind": "allow_once"}],
    }
    with pytest.raises(RelayError):
        relay.send(seat, "packet")

    assert seat["permission_log"] == [
        {"tool": "execute", "decision": "allow-once", "kind": "allow_once"}
    ], "前 round の記録が残っている (今 round の許可が消えている)"
    assert seat["permission_log_partial"] is True, "turn 未完であることが区別できない"


def test_permission_log_partial_flag_is_cleared_on_success(fake_grok):
    """成功した round では partial マークを持ち越さない。"""
    agent = FakeAgent()
    fake_grok(agent)
    relay = _relay()
    seat: dict = {"thread_ref": "sid-1", "permission_log_partial": True}
    relay.send(seat, "packet")
    assert "permission_log_partial" not in seat
    assert seat["permission_log"] == []
    relay.close()


def test_permission_without_allowed_option_is_cancelled(fake_grok):
    """許可できる option が無ければ拒否する。無応答で固めない。"""
    agent = FakeAgent(permission_options=[
        {"optionId": "reject-once", "kind": "reject_once"},
    ])
    fake_grok(agent)
    relay = _relay()
    seat: dict = {}
    relay.send(seat, "packet")
    assert agent.client_responses[0]["result"] == {"outcome": {"outcome": "cancelled"}}
    assert seat["permission_log"][0]["decision"] == "cancelled"
    relay.close()


def test_permission_policy_is_configurable(fake_grok):
    agent = FakeAgent(permission_options=PERMISSION_OPTIONS)
    fake_grok(agent)
    relay = _relay(auto_allow_kinds=("allow_always", "allow_once"))
    relay.send({}, "packet")
    assert agent.client_responses[0]["result"]["outcome"]["optionId"] == "always-allow"
    relay.close()


def test_unknown_inbound_request_gets_an_error_response(fake_grok):
    """未知の request も必ず答える。黙って捨てると席が待ち続ける。"""
    agent = FakeAgent(extra_inbound={
        "jsonrpc": "2.0", "id": 7, "method": "session/unknown_thing", "params": {},
    })
    fake_grok(agent)
    relay = _relay()
    relay.send({}, "packet")
    err = next(m for m in agent.client_responses if m.get("id") == 7)
    assert err["error"]["code"] == -32601
    relay.close()


def test_inbound_request_is_not_mistaken_for_an_rpc_response(fake_grok):
    """agent 側 id (実測 0 起点) が client 側 id と衝突しても応答と取り違えない。

    `id` を持つメッセージを一律に応答扱いすると、許可要求が
    `session/prompt` の結果として返り、席は許可を待ったまま dispatch だけ先に進む。
    """
    agent = FakeAgent(permission_options=PERMISSION_OPTIONS, permission_request_id=3)
    fake_grok(agent)
    relay = _relay()
    # client の id は initialize=1 / session/new=2 / session/prompt=3 と進むので、
    # 許可要求の id 3 は prompt の応答と同じ番号になる。
    label = relay.send({}, "packet")
    assert label.startswith("tier1-prompt:end_turn")
    assert agent.client_responses[0]["id"] == 3
    relay.close()


# --- バイナリ解決 -------------------------------------------------------


def test_explicit_binary_wins(monkeypatch):
    assert relay_grok.resolve_grok_binary(r"C:\custom\grok.exe") == r"C:\custom\grok.exe"


def test_managed_install_beats_path(monkeypatch, tmp_path):
    """PATH 先頭を信じない (`~/.grok/bin` に `grok.exe.old` が同居する環境がある)。"""
    home = tmp_path / "grokhome"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "grok.exe").write_text("x", encoding="utf-8")
    (home / "bin" / "grok.exe.old").write_text("x", encoding="utf-8")
    other = tmp_path / "path-dir"
    other.mkdir()
    (other / "grok.exe").write_text("x", encoding="utf-8")

    monkeypatch.setenv("GROK_HOME", str(home))
    monkeypatch.setenv("PATH", str(other))
    resolved = relay_grok.resolve_grok_binary()
    assert resolved == str(home / "bin" / "grok.exe")
    assert not resolved.endswith(".old")


def test_resolve_falls_back_to_bare_name(monkeypatch, tmp_path):
    """見つからなければ素の名前。spawn が OSError になり Tier3 へ縮退できる。"""
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "nope"))
    monkeypatch.setenv("PATH", str(tmp_path / "also-nope"))
    assert relay_grok.resolve_grok_binary() == "grok"


def test_spawn_failure_becomes_relay_error(monkeypatch):
    def boom(argv, **kwargs):
        raise OSError("not found")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(RelayError, match="spawn grok"):
        _relay().send({}, "packet")


# --- 未実測の口 ---------------------------------------------------------


def test_rename_seat_is_not_implemented():
    """席の rename 経路は実測できていない。推測でメソッドを撃たない。"""
    with pytest.raises(NotImplementedError):
        _relay(cwd=None).rename_seat("sid", "rt-topic-grok")


# --- get_relay 配線 -----------------------------------------------------


def test_get_relay_grok_tier1_wraps_with_fallback(monkeypatch):
    captured = {}

    class _Spy:
        tier = 1

        def __init__(self, cwd=None):
            captured["cwd"] = cwd

        def send(self, seat, text):
            raise RelayError("grok down")

        def poll(self, seat):
            return None

    monkeypatch.setattr("roundtable.relay_grok.GrokAcpRelay", _Spy)
    relay = relay_mod.get_relay("grok", tier=1, allow_fallback=True, cwd="/tmp/topic")
    assert isinstance(relay, FallbackRelay)
    assert captured["cwd"] == "/tmp/topic"
    assert relay.tier == 1


def test_grok_tier1_failure_falls_back_to_tier3(monkeypatch):
    """Tier1 が落ちたら Tier3 (クリップボード)。Tier2 へは昇格しない。"""
    sent = []

    class _Boom:
        tier = 1

        def __init__(self, cwd=None):
            pass

        def send(self, seat, text):
            raise RelayError("grok agent が終了した rc=1")

        def poll(self, seat):
            return None

    monkeypatch.setattr("roundtable.relay_grok.GrokAcpRelay", _Boom)
    monkeypatch.setattr(Tier3Relay, "send",
                        lambda self, seat, text: sent.append(text) or "delivered")
    relay = relay_mod.get_relay("grok", tier=1, cwd="/tmp/topic")
    seat: dict = {}
    label = relay.send(seat, "packet")
    assert label.startswith("fallback-tier3:")
    assert relay.tier == 3
    assert seat["tier"] == 3 and seat["fallback_reason"]
    assert sent == ["packet"]


def test_fallback_relay_closes_both_transports():
    """縮退した round でも preferred のプロセス木を回収する (レビュー H2)。

    `_active` (= Tier3) だけ閉じても意味がない。プロセスを起こしたのは失敗した
    preferred の方で、そちらが回収対象。close を持たない相手は素通りする。
    """
    closed: list[str] = []

    class _WithClose:
        tier = 1

        def send(self, seat, text):
            raise RelayError("down")

        def poll(self, seat):
            return None

        def close(self):
            closed.append("preferred")

    class _WithoutClose:
        tier = 3

        def send(self, seat, text):
            return "delivered"

        def poll(self, seat):
            return None

    relay = FallbackRelay(_WithClose(), _WithoutClose())
    relay.send({}, "packet")
    relay.close()  # close 非装備の fallback があっても落ちない
    assert closed == ["preferred"]


def test_grok_without_tier1_is_tier3():
    assert relay_mod.get_relay("grok", tier=3).tier == 3
    assert relay_mod.get_relay("grok", tier=2).tier == 3  # 自動昇格しない
    assert isinstance(relay_mod.get_relay("gemini", tier=1), Tier3Relay)  # 未対応席


def test_codex_tier1_wiring_is_unchanged(monkeypatch):
    """grok を足しても codex の経路が変わっていないこと。"""
    captured = {}

    class _Spy:
        tier = 1

        def __init__(self, cwd=None):
            captured["cwd"] = cwd

        def send(self, seat, text):
            return "spy"

        def poll(self, seat):
            return None

    monkeypatch.setattr("roundtable.relay_codex.CodexAppServerRelay", _Spy)
    relay = relay_mod.get_relay("codex", tier=1, allow_fallback=False, cwd="/tmp/topic")
    assert isinstance(relay, _Spy)
    assert captured["cwd"] == "/tmp/topic"


# --- 後始末 -------------------------------------------------------------


def test_close_is_idempotent():
    relay = _relay()
    relay.close()
    relay.close()


def test_close_kills_process_tree_when_terminate_fails(fake_grok, monkeypatch):
    agent = FakeAgent()
    fake_grok(agent)
    relay = _relay()
    relay.send({}, "packet")
    proc = fake_grok.created[0]

    killed = {}
    monkeypatch.setattr("roundtable.relay_process.ProcessTree.kill_tree",
                        lambda self, pid: killed.setdefault("pid", pid))
    monkeypatch.setattr(FakeProc, "terminate", lambda self: self.calls.append("terminate"))
    relay.close()
    assert killed["pid"] == proc.pid
    assert "kill" in proc.calls
    assert relay._proc is None


def test_close_reaps_tree_even_when_terminate_succeeds(fake_grok, monkeypatch):
    agent = FakeAgent()
    fake_grok(agent)
    relay = _relay()
    relay.send({}, "packet")
    reaped = []
    monkeypatch.setattr("roundtable.relay_process.ProcessTree.close",
                        lambda self: reaped.append(True))
    relay.close()
    assert reaped


def test_process_tree_is_shared_with_codex():
    """木の回収は 2 relay で 1 実装 (重複実装を作らない)。"""
    from roundtable import relay_codex
    from roundtable.relay_process import ProcessTree

    assert relay_codex._ProcessTree is ProcessTree


# --- timeout (fake は即答するので数値で固定するしかない) ----------------


def test_timeouts_have_margin_over_measured_latency():
    """実測 (2026-08-07 / grok 1.0.0):

    initialize 0.42-0.57s / session/new 2.18-2.28s / session/load 2.20s /
    **実 packet の session/prompt 53.67-64.38s** (自明な prompt は 8.93-9.29s)。
    codex 版で「既定 5s のせいで Tier1 が 100% 失敗していた」事故を踏んでいるので、
    数値そのものを不変条件として固定する。
    """
    assert relay_grok.TIMEOUT_INITIALIZE >= 30
    assert relay_grok.TIMEOUT_SESSION_NEW >= 120
    assert relay_grok.TIMEOUT_SESSION_LOAD >= 120
    assert relay_grok.TIMEOUT_PROMPT >= 600, "実 packet 53.67-64.38s に対し余裕がない"


def test_prompt_blocks_until_turn_completion(fake_grok):
    """codex (`turn/start` は投げっぱなし) との差。返った時点で成果物は書き終わっている。

    ここでは「許可要求を挟んでも send() が turn 完了まで戻らない」ことを、
    prompt の応答が許可応答より後に来る順序で担保する。
    """
    agent = FakeAgent(permission_options=PERMISSION_OPTIONS)
    fake_grok(agent)
    relay = _relay()
    done = threading.Event()

    def run():
        relay.send({}, "packet")
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=10)
    assert done.is_set(), "send() が返らない (許可応答が届いていない可能性)"
    assert agent.client_responses, "許可応答なしに turn が完了している"
    relay.close()
