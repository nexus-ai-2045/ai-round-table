"""2 席 (codex 相当 + grok 相当) の 1 議題 2 round を模擬席で通す統合テスト。

**実 CLI は起動しない**。codex app-server / grok ACP の口を fake stdio で置き換え、
`roundtable.cli.main` を本物のまま 4 回 dispatch する。ここで見たいのは relay 単体の
protocol ではなく (それは `test_v02_relay_grok.py` / `test_v02_tier1_hardening.py` の
担当)、**2 席が混ざった時に配線・席の同一性・KPI が壊れないこと**:

1. 軸 A KPI — Tier1 席では人間の操作が増えない。dispatch 1 回につき記録される
   human_action は `dispatch` の 1 件だけで、`tier3_paste_required` が付かない。
   同じ手順を Tier3 で回すと貼り付けが 1 件ずつ増える (対照実験) ため、
   「Tier1 だから増えない」ことが差分として出る。
2. 席が round を跨いで分裂しない。2 round 目は codex が `thread/resume`、
   grok が `session/load` を撃ってから送る (新しい席を作らない)。
3. 2 席が同じ議事録に merge され、round が機械的に進む。

模擬席は packet の指示どおりに動く: packet 文から invocation id と出力先を読み、
`<inv>.json.tmp` に書いてから `<inv>.json` へ rename する。dispatcher 側の
「rename 完了 = 完成」という契約を、テストの都合で緩めない。
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
from pathlib import Path

import pytest

from roundtable import relay as relay_mod
from roundtable.cli import main
from roundtable.paths import ensure_topic

# bench が subprocess.Popen を席 mock に差し替えても、ledger の git は実物を使う
_REAL_POPEN = subprocess.Popen

# 実測の initialize result (grok 1.0.0) から、capability ゲートが見る部分だけ。
GROK_CAPABILITIES = {
    "loadSession": True,
    "sessionCapabilities": {"list": {}, "resume": {}, "close": {}},
}

GROK_SESSION_ID = "019fdc03-e591-7b80-9153-b97f61491ee1"
CODEX_THREAD_ID = "019fd900-1111-2222-3333-444455556666"

_INV_RE = re.compile(r"invocation: ([0-9a-f]{12})\]")
_OUT_RE = re.compile(r"一時ファイル (.+?\.json)\.tmp に書き")


def _seat_writes_opinion(packet_text: str, participant: str, note: str) -> str:
    """模擬席の本体 — packet を読んで契約 JSON を scratch へ置く。

    出力先も invocation id も packet から取る (テストが知っている値を使わない)。
    packet が壊れたらここで落ちるので、席から見た指示の可読性も一緒に守られる。
    """
    inv_m = _INV_RE.search(packet_text)
    out_m = _OUT_RE.search(packet_text)
    assert inv_m and out_m, f"packet から invocation / 出力先を読めない: {packet_text[:120]!r}"
    inv = inv_m.group(1)
    out = Path(out_m.group(1))
    tmp = Path(str(out) + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "invocation_id": inv,
                "participant": participant,
                "opinion": f"{participant} の {note}",
                "claims": [
                    {"claim": f"{participant}/{note}", "evidence_type": "argument",
                     "evidence": "模擬席"}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, out)  # 契約どおり rename で確定させる
    return inv


# --- fake stdio (Popen の最小モック) ------------------------------------


class _FakeStdin:
    def __init__(self, agent: "_Agent"):
        self._agent = agent
        self._buf = ""

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
        pass


class _FakeStdout:
    def __init__(self, agent: "_Agent"):
        self._agent = agent

    def __iter__(self):
        while True:
            item = self._agent.out.get()
            if item is None:
                return
            yield item


class _FakeProc:
    def __init__(self, agent: "_Agent", argv):
        self.pid = 4242
        self.argv = argv
        self.stdin = _FakeStdin(agent)
        self.stdout = _FakeStdout(agent)
        self.stderr = None  # drain 経路は None で早期 return する
        self.returncode = None
        self._agent = agent
        self.calls: list[str] = []  # 回収経路 (terminate / kill / wait) の記録

    def poll(self):
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        self.returncode = 0
        self._agent.out.put(None)

    def kill(self):
        self.calls.append("kill")
        self.returncode = -9
        self._agent.out.put(None)

    def wait(self, timeout=None):
        self.calls.append("wait")
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        return self.returncode


class _Agent:
    """1 プロセス分の席。`received` に client → agent の method 列が残る。"""

    def __init__(self, participant: str, note: str):
        self.out: queue.Queue[str | None] = queue.Queue()
        self.received: list[dict] = []
        self.participant = participant
        self.note = note
        self.written: list[str] = []

    def emit(self, obj: dict) -> None:
        self.out.put(json.dumps(obj, ensure_ascii=False) + "\n")

    def reply(self, msg: dict, result: dict) -> None:
        self.emit({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    def methods(self) -> list[str]:
        return [m["method"] for m in self.received]

    def handle(self, line: str) -> None:
        raise NotImplementedError


class CodexAgent(_Agent):
    """`codex app-server` 役 (thread/start·resume·name·turn)。"""

    def handle(self, line: str) -> None:
        msg = json.loads(line)
        self.received.append(msg)
        method = msg.get("method")
        if "id" not in msg:
            return  # `initialized` notification
        if method == "initialize":
            self.reply(msg, {"userAgent": "fake-codex"})
        elif method == "thread/start":
            self.reply(msg, {"thread": {"id": CODEX_THREAD_ID}})
        elif method in ("thread/resume", "thread/name/set"):
            self.reply(msg, {})
        elif method == "turn/start":
            text = msg["params"]["input"][0]["text"]
            self.written.append(_seat_writes_opinion(text, self.participant, self.note))
            self.reply(msg, {})
        else:
            self.emit({"jsonrpc": "2.0", "id": msg["id"],
                       "error": {"code": -32601, "message": method}})


class GrokAgent(_Agent):
    """`grok agent --no-leader stdio` 役 (session/new·load·prompt)。"""

    def handle(self, line: str) -> None:
        msg = json.loads(line)
        if msg.get("method") is None:
            return  # inbound request への client の応答 (この議題では発生しない)
        self.received.append(msg)
        method = msg["method"]
        if "id" not in msg:
            return
        if method == "initialize":
            self.reply(msg, {"protocolVersion": 1, "agentCapabilities": GROK_CAPABILITIES})
        elif method == "session/new":
            self.reply(msg, {"sessionId": GROK_SESSION_ID})
        elif method == "session/load":
            self.reply(msg, {"models": {}})
        elif method == "session/prompt":
            text = msg["params"]["prompt"][0]["text"]
            self.written.append(_seat_writes_opinion(text, self.participant, self.note))
            self.reply(msg, {"stopReason": "end_turn"})
        else:
            self.emit({"jsonrpc": "2.0", "id": msg["id"],
                       "error": {"code": -32601, "message": method}})


class _Bench:
    """dispatch のたびに新しい席プロセスが立つ様子を再現する台。"""

    def __init__(self):
        self.agents: list[_Agent] = []
        self.procs: list[_FakeProc] = []
        self.note = "round1"

    def agents_for(self, participant: str) -> list[_Agent]:
        return [a for a in self.agents if a.participant == participant]

    def popen(self, argv, **kwargs):
        if argv and argv[0] == "git":
            # ledger (D12) の git 呼び出しは席の spawn ではない。実物に素通しする。
            # 席以外で他に許すものは無い — 未知 spawn を落とす検知は維持する。
            return _REAL_POPEN(argv, **kwargs)
        assert kwargs["encoding"] == "utf-8", "cp932 に戻ると日本語 packet が壊れる"
        assert kwargs.get("shell") is not True
        if "app-server" in argv:
            agent: _Agent = CodexAgent("codex", self.note)
        elif "stdio" in argv:
            agent = GrokAgent("grok", self.note)
        else:  # pragma: no cover - 想定外の spawn は即座に落とす
            raise AssertionError(f"未知の席 spawn: {argv}")
        self.agents.append(agent)
        proc = _FakeProc(agent, argv)
        self.procs.append(proc)
        return proc

    def shutdown(self) -> None:
        for proc in self.procs:
            proc.terminate()


@pytest.fixture
def bench(monkeypatch):
    """実 CLI を起こさずに 2 席分の Tier1 経路を通す。"""
    b = _Bench()
    monkeypatch.setattr("roundtable.relay_codex.resolve_codex_binary",
                        lambda preferred=None: "codex.exe")
    monkeypatch.setattr("roundtable.relay_grok.resolve_grok_binary",
                        lambda preferred=None: "grok.exe")
    monkeypatch.setattr("roundtable.relay_process.ProcessTree.adopt",
                        lambda self, proc: None)
    monkeypatch.setattr(subprocess, "Popen", b.popen)
    yield b
    b.shutdown()


# --- helper -------------------------------------------------------------


def _run_topic(root: Path, tier: int, participants=("codex", "grok"), rounds=(1, 2),
               bench: _Bench | None = None) -> None:
    assert main([
        "new-topic", "t", "--topic", "2 席の round",
        "--participants", ",".join(participants), "--root", str(root),
    ]) == 0
    for rnd in rounds:
        if bench is not None:
            bench.note = f"round{rnd}"
        for participant in participants:
            rc = main([
                "dispatch", "t", "--participant", participant,
                "--tier", str(tier), "--timeout", "10", "--root", str(root),
            ])
            assert rc == 0, f"round {rnd} / {participant} の dispatch が失敗した"


def _journal(root: Path) -> dict:
    return json.loads(ensure_topic(root, "t").journal.read_text(encoding="utf-8"))


def _actions(root: Path) -> list[str]:
    return [a["action"] for a in _journal(root)["human_actions"]]


# --- 本体 ---------------------------------------------------------------


def test_two_tier1_seats_run_two_rounds(tmp_path, bench):
    """codex 相当 + grok 相当の 2 席で 1 議題 2 round が最後まで回る。"""
    _run_topic(tmp_path, tier=1, bench=bench)

    tp = ensure_topic(tmp_path, "t")
    text = tp.minutes.read_text(encoding="utf-8")
    for rnd in (1, 2):
        assert f"## Round {rnd}" in text
        assert f"codex の round{rnd}" in text
        assert f"grok の round{rnd}" in text

    journal = _journal(tmp_path)
    states = [v["state"] for v in journal["invocations"].values()]
    assert states == ["merged"] * 4, journal["invocations"]
    assert journal["round"] == 3, "2 round 完了で round は 3 に進む"
    assert journal.get("conflicts", []) == []

    # 席が round を跨いで分裂していない (2 席とも同じ席 id を使い続ける)
    seats = json.loads(tp.seats.read_text(encoding="utf-8"))
    assert seats["rt/t/codex"]["thread_ref"] == CODEX_THREAD_ID
    assert seats["rt/t/grok"]["thread_ref"] == GROK_SESSION_ID
    for key in ("rt/t/codex", "rt/t/grok"):
        assert seats[key]["tier"] == 1, f"{key} が Tier3 に縮退している"
        assert "fallback_reason" not in seats[key]


def test_tier1_seats_do_not_add_human_actions(tmp_path, bench):
    """軸 A KPI — Tier1 席では人間の操作が dispatch 以外に増えない。

    Tier3 は搬出のたびに `tier3_paste_required` (= CEO の貼り付け) を記録する。
    Tier1 でそれが 1 件も付かないことを、席 2 つ × 2 round の全経路で固定する。
    """
    _run_topic(tmp_path, tier=1, bench=bench)

    actions = _actions(tmp_path)
    assert actions == ["new-topic", "dispatch", "dispatch", "dispatch", "dispatch"]
    assert "tier3_paste_required" not in actions
    # 席数 × round 数を増やしても、増えるのは指名 (dispatch) だけ = 貼り付けはゼロ。
    assert len(actions) == 1 + 2 * 2


def test_tier3_control_shows_the_paste_cost(tmp_path, monkeypatch):
    """対照実験 — 同じ手順を Tier3 で回すと貼り付けが席 × round 分だけ増える。

    Tier1 側の 0 件が「そもそも記録されない」ではなく「Tier1 だから発生しない」で
    あることを、同じ CLI 経路の差分で示す。clip.exe は起こさず、貼り付けられた席が
    応答する様子だけを模擬する。
    """
    def paste(self, seat, text):
        _seat_writes_opinion(text, seat["participant"], "tier3")
        return "delivered"

    monkeypatch.setattr("roundtable.relay_tier3.Tier3Relay.send", paste)
    _run_topic(tmp_path, tier=3)

    actions = _actions(tmp_path)
    assert actions.count("tier3_paste_required") == 4  # 2 席 × 2 round
    assert len(actions) == 1 + 2 * 2 + 4
    # Tier1 (5 件) との差 4 件がそのまま「自動化で消えた手数」。
    assert len(actions) - 4 == 5


def test_second_round_reuses_the_same_seat_on_both_relays(tmp_path, bench):
    """2 round 目は codex が thread/resume、grok が session/load を撃つ。

    どちらかが新規作成に落ちると、CEO が見ていない別チャットに席が分裂する。
    """
    _run_topic(tmp_path, tier=1, bench=bench)

    codex1, codex2 = bench.agents_for("codex")
    assert codex1.methods() == [
        "initialize", "initialized", "thread/start", "thread/name/set", "turn/start",
    ]
    assert codex2.methods() == ["initialize", "initialized", "thread/resume", "turn/start"]
    resume = next(m for m in codex2.received if m["method"] == "thread/resume")
    assert resume["params"]["threadId"] == CODEX_THREAD_ID
    # 再開経路でも sandbox を明示する (start だけ固めても 2 round 目に穴が開く)
    assert resume["params"]["sandbox"] == "workspace-write"

    grok1, grok2 = bench.agents_for("grok")
    assert grok1.methods() == ["initialize", "session/new", "session/prompt"]
    assert grok2.methods() == ["initialize", "session/load", "session/prompt"]
    load = next(m for m in grok2.received if m["method"] == "session/load")
    assert load["params"]["sessionId"] == GROK_SESSION_ID


def test_each_seat_is_scoped_to_the_topic_directory(tmp_path, bench):
    """2 席とも cwd = 議題ディレクトリ (他議題の状態ファイルを書かせない)。"""
    _run_topic(tmp_path, tier=1, rounds=(1,), bench=bench)

    expected = str(ensure_topic(tmp_path, "t").root.resolve())
    for proc in bench.procs:
        assert proc.stdin is not None
    for agent in bench.agents:
        opener = next(
            m for m in agent.received
            if m.get("method") in ("thread/start", "session/new")
        )
        assert opener["params"]["cwd"] == expected


def test_one_tier1_seat_failure_does_not_drag_the_other_down(tmp_path, bench, monkeypatch):
    """grok 席が落ちても Tier3 に縮退するだけで、codex 席は Tier1 のまま。

    縮退は席単位であること (と、縮退した席だけ貼り付けが記録されること) を固定する。
    """
    from roundtable.relay import RelayError

    def boom(self, seat, text):
        raise RelayError("grok agent が終了した rc=1")

    monkeypatch.setattr("roundtable.relay_grok.GrokAcpRelay.send", boom)

    def paste(self, seat, text):
        _seat_writes_opinion(text, seat["participant"], "tier3")
        return "delivered"

    monkeypatch.setattr("roundtable.relay_tier3.Tier3Relay.send", paste)
    _run_topic(tmp_path, tier=1, rounds=(1,), bench=bench)

    seats = json.loads(ensure_topic(tmp_path, "t").seats.read_text(encoding="utf-8"))
    assert seats["rt/t/codex"]["tier"] == 1
    assert seats["rt/t/grok"]["tier"] == 3
    assert seats["rt/t/grok"]["fallback_reason"]
    # 縮退した 1 席分だけ貼り付けが増える (codex 側は増えない)
    assert _actions(tmp_path).count("tier3_paste_required") == 1


def test_relay_wiring_picks_one_transport_per_participant():
    """席の割り当てが participant 名だけで決まること (2 席が同じ口を掴まない)。"""
    from roundtable.relay_codex import CodexAppServerRelay
    from roundtable.relay_grok import GrokAcpRelay

    codex = relay_mod.get_relay("codex", tier=1, allow_fallback=False, cwd="/tmp/t")
    grok = relay_mod.get_relay("grok", tier=1, allow_fallback=False, cwd="/tmp/t")
    assert isinstance(codex, CodexAppServerRelay)
    assert isinstance(grok, GrokAcpRelay)


def test_dispatch_closes_the_seat_process_after_collect(tmp_path, bench):
    """dispatch は席のプロセス木を回収してから返る (レビュー H2)。

    `close()` には本番の呼び出し元が無く、Windows の Job Object
    (`KILL_ON_JOB_CLOSE`) が Python 終了時に木ごと落としてくれて偶然助かっていた。
    POSIX は `killpg` が一度も走らず、CLI 終了後に席のプロセス木 (実測 1 席 10
    プロセス) が孤児として残る。

    `rc == 0` (= collect が成功している) と同時に回収済みであることを見るので、
    「collect の前に閉じて席を殺す」実装に変えるとこの議題は回らなくなる。
    """
    _run_topic(tmp_path, tier=1, rounds=(1,), bench=bench)

    assert bench.procs, "席が spawn されていない"
    for proc in bench.procs:
        assert "terminate" in proc.calls, f"{proc.argv} が回収されていない"


def test_async_dispatch_keeps_the_seat_running(tmp_path, bench):
    """`--async` は搬出だけして席を動かしたまま返る契約なので閉じない。"""
    assert main([
        "new-topic", "t", "--topic", "async", "--participants", "grok",
        "--root", str(tmp_path),
    ]) == 0
    assert main([
        "dispatch", "t", "--participant", "grok", "--tier", "1",
        "--async", "--root", str(tmp_path),
    ]) == 0

    assert bench.procs, "席が spawn されていない"
    assert all("terminate" not in p.calls for p in bench.procs), \
        "--async なのに席を閉じている (回収は collect 側の責務)"


def test_grok_tier1_dispatch_has_no_integrity_warning(tmp_path, bench, capsys):
    """grok 席の「検知不成立」警告は D12 で消えたまま復活しない。

    旧 witness 検知は「席が証跡に届かない」前提で、grok 席が PowerShell を
    任意実行できる (2026-08-07 spike 実測) ため成立せず、警告を出していた。
    git 検知 (D12) は席がどこに書けても成立するので、警告が再び出るなら
    「検知が席の sandbox に依存する設計」への退行を意味する。
    """
    _run_topic(tmp_path, tier=1, rounds=(1,), bench=bench)
    out = capsys.readouterr().out
    assert "改ざん検知は成立しない" not in out


def test_status_surfaces_the_seat_tiers(tmp_path, bench, capsys):
    """status から席の tier が読める (dispatch を見逃しても分かる)。

    grok 警告の assert は D12 (git 一本化) で撤去した — 検知が席の sandbox に
    依存しなくなったため、警告自体が存在しない。
    """
    _run_topic(tmp_path, tier=1, rounds=(1,), bench=bench)
    capsys.readouterr()

    assert main(["status", "t", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "rt/t/codex  tier=1" in out
    assert "rt/t/grok  tier=1" in out
    assert "改ざん検知は成立しない" not in out


def test_seat_threads_do_not_leak_between_participants(tmp_path, bench):
    """codex の thread_ref と grok の sessionId が混ざらない (同じ key 名を共有するため)。"""
    _run_topic(tmp_path, tier=1, rounds=(1,), bench=bench)
    seats = json.loads(ensure_topic(tmp_path, "t").seats.read_text(encoding="utf-8"))
    assert seats["rt/t/codex"]["thread_ref"] != seats["rt/t/grok"]["thread_ref"]
    assert threading.active_count() >= 1  # reader thread は daemon (join を要求しない)
