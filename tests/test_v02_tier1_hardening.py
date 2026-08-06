"""Tier1 (codex app-server) の安全性ハードニング検証。

いずれも「実 CLI を起動せずに、危険な既定に戻ったら落ちる」ことを目的にする。
実プロセスを起こさないので CI で安全に回せる。
"""
import subprocess

import pytest

from roundtable import relay as relay_mod
from roundtable.cli import main
from roundtable.paths import ensure_topic
from roundtable.relay_codex import CodexAppServerRelay


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


def test_close_kills_process_tree_when_terminate_fails(monkeypatch):
    """terminate で死ななければ子孫ごと kill する (孤児プロセス防止)。"""
    proc = _FakeProc(alive_after_terminate=True)
    killed = {}

    monkeypatch.setattr(
        "roundtable.relay_codex._kill_process_tree",
        lambda pid: killed.setdefault("pid", pid),
    )
    relay = CodexAppServerRelay()
    relay._proc = proc
    relay.close()

    assert "terminate" in proc.calls
    assert killed["pid"] == proc.pid  # ツリー kill が呼ばれている
    assert "kill" in proc.calls
    assert relay._proc is None


def test_close_is_idempotent():
    relay = CodexAppServerRelay()
    relay.close()
    relay.close()  # 二度目でも例外を出さない


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

    assert seen["cwd"] == str(ensure_topic(tmp_path, "t1").root)
    assert seen["cwd"] != str(tmp_path)
