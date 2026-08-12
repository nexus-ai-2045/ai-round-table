"""Grok 席向け Tier1: `grok agent --no-leader stdio` (ACP / JSON-RPC 2.0)。

relay_codex.py と同じ 2 層構成 (transport = プロセス spawn + 行区切り JSON + id 相関 /
上位 = 席の開始・再開・送信)。CEO 禁止は「CLI で AI を**実行**する」ことなので、ここは
`grok -p` のような headless 実行ではなく、TUI と同じ session ストアに載る席へ prompt を
届けるだけにする。実測で `grok sessions list` / `grok export` から同じ会話が読めることを
確認済み (DESIGN v6 §0 の「席 = CEO が読めるチャット」)。

**すべて 2026-08-07 の spike 実測に基づく** (grok 1.0.0 (3cd0d0cbce) / Windows 11 /
計 4 プロセス spawn / 5 turn)。実測していない口 (`grok agent serve` の WebSocket、
`grok agent leader` の共有 socket、席の rename) は実装しない。

codex 版との差分 (実測):

| 論点 | codex | grok |
|---|---|---|
| 開始 / 再開 | `thread/start` / `thread/resume` | `session/new` / `session/load` |
| 送信 | `turn/start` (投げっぱなし) | `session/prompt` (**turn 完了までブロック**) |
| 承認 | `approvalPolicy: "never"` を params で | **inbound `session/request_permission` に応答**。params に相当物なし |
| バイナリ選択 | 版番号ゲート | `initialize` が自己申告する capability を実測 |
| 席の命名 | `thread/name/set` | 相当メソッドなし (`rename_seat` は NotImplementedError) |

席 id は **`seat["thread_ref"]` に入れる** (grok の呼び名は `sessionId`)。key 名を
変えないのは `relay.merge_seats` が `thread_ref` だけをディスク側優先で保護しており、
別 key にすると並行 dispatch で席が分裂する (CEO が見ていない別チャットに落ちる) ため。

失敗時は RelayError を上げ、呼び出し側 (FallbackRelay) が Tier3 に縮退する。
"""
from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .relay import RelayError
from .relay_process import ProcessTree


# 実測 (2026-08-07, Windows 11 / grok 1.0.0 (3cd0d0cbce)):
#   initialize    0.42 / 0.44 / 0.48 / 0.57s  (n=4)
#   session/new   2.18 / 2.28 / 2.28s         (n=3)
#   session/load  2.20s                       (n=1)
#   session/prompt  自明な prompt 8.93 / 9.29s (n=2)
#                   **実 packet 53.67 / 64.38s** (n=2)
# codex app-server のような病的なばらつき (initialize 6.66s / start 120s 超) は
# 観測されなかったが、負荷時は未測定なので枠は広く取る。
TIMEOUT_INITIALIZE = 60.0
TIMEOUT_SESSION_NEW = 180.0
# start が軽い環境で load だけ重い保証はない、という codex 版の判断を踏襲して同枠。
TIMEOUT_SESSION_LOAD = 180.0
# 実測 53.67-64.38s (n=2) に対し 900s。自明な prompt の 9s から決めてはならない
# (実 packet とで 6-7 倍開いた)。厚い議事録・並走負荷は未測定なので余裕を大きく取る。
TIMEOUT_PROMPT = 900.0


PROTOCOL_VERSION = 1
"""`initialize` で送る ACP バージョン。実測の応答も `protocolVersion: 1`。"""


AUTO_ALLOW_KINDS: tuple[str, ...] = ("allow_once",)
"""`session/request_permission` で自動選択する option の kind (優先順)。

**応答しないと席が永久に固まる** (2026-08-07 実測: 実 packet を投げた席が PowerShell
実行の許可を求めて停止し、900s まで一切進まなかった)。codex の `approvalPolicy: "never"`
に相当する `session/new` params が ACP に無く、client 側で答える以外の経路が無い。

実測で完走したのは `allow_always` (`optionId: "always-allow"`) を選んだ経路だが、
既定はここを `allow_once` に狭める。理由: `allow_always` の**永続範囲が未実測**で
(session 単位か config への書き込みかが不明。実測でも新規 session で 1 回目に許可した後
さらに 2 回聞かれており挙動が読めない)、承認が席を跨いで残ると書込境界の話が広がる。
応答の形は実測と同一で、席が提示した option 一覧から狭い方を選ぶだけ。
**ただし `allow_once` 経路そのものの実走は未実測**。
"""


def resolve_grok_binary(preferred: str | None = None) -> str:
    """grok 実行ファイルを選ぶ。PATH 先頭を無条件に信じない。

    codex 版 (`resolve_codex_binary`) と同じ問題意識だが、**版番号ゲートは使わない**。
    grok は `initialize` の result で `agentCapabilities.loadSession` /
    `sessionCapabilities.resume` を自己申告するので、版を推測するより実測が確実
    (`_check_capabilities`)。ここでは置き場所の優先順だけ決める。

    実測環境では `where grok` は managed install (`~/.grok/bin/grok.exe`) を返したが、
    同ディレクトリに `grok.exe.old` が同居している。名前を完全一致で見るのはそのため。
    `agent.exe` は grok.exe と同一サイズ・同一 `--version` だが引数解釈は未検証なので
    候補に入れない。
    """
    if preferred:
        return preferred
    names = ("grok.exe", "grok.cmd", "grok")
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(directory: str) -> None:
        if not directory:
            return
        for name in names:
            cand = os.path.join(directory, name)
            if cand in seen:
                continue
            seen.add(cand)
            if os.path.isfile(cand):
                candidates.append(cand)

    home = os.environ.get("GROK_HOME") or str(Path.home() / ".grok")
    _add(os.path.join(home, "bin"))
    for directory in (os.environ.get("PATH") or "").split(os.pathsep):
        _add(directory)
    # 見つからなければ素の名前を返す。spawn が OSError で落ち、RelayError → Tier3 になる。
    return candidates[0] if candidates else "grok"


class GrokAcpRelay:
    """`grok agent --no-leader stdio` に packet を届ける Tier1 relay。

    `--no-leader` を明示するのは、CEO の TUI が使う leader backend
    (`~/.grok/leader.sock`) に相乗りしないため。実測時点で TUI が 4 セッション
    同時稼働しており、共有 backend に載ると席の独立性と障害分離を失う。
    """

    tier = 1

    def __init__(
        self,
        binary: str | None = None,
        cwd: str | None = None,
        auto_allow_kinds: tuple[str, ...] = AUTO_ALLOW_KINDS,
    ):
        self.binary = resolve_grok_binary(binary)
        self.cwd = cwd
        self.auto_allow_kinds = tuple(auto_allow_kinds)
        self._proc: subprocess.Popen[str] | None = None
        self._q: queue.Queue[dict] = queue.Queue()
        self._id = 0
        self._wlock = threading.Lock()  # reader thread の応答書き込みと混線させない
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        # stderr の直近数行 (診断用)。**判定には使わない** — 下記 _drain_stderr 参照。
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        self._tree = ProcessTree()
        # このプロセスが load 済みの session id。プロセスの寿命と一致させる
        # (別プロセスは過去の session を何も知らない)。
        self._loaded_sessions: set[str] = set()
        # 直近 send() 中の許可要求の記録。CEO 提示用の detector で、判定には使わない。
        self.permission_log: list[dict] = []

    # ---- transport ---------------------------------------------------

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _ensure(self) -> None:
        if self._proc and self._proc.poll() is None:
            return
        self._tree = ProcessTree()
        # 新しいプロセスは過去の session を何も知らない。消し忘れると 2 プロセス目が
        # session/load を飛ばして prompt を撃つ (codex 版で実際に踏んだ失敗と同型)。
        self._loaded_sessions.clear()
        try:
            self._proc = subprocess.Popen(
                [self.binary, "agent", "--no-leader", "stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",   # 未指定だと Windows は cp932 になり日本語 packet が壊れる
                errors="replace",
                bufsize=1,
                cwd=self.cwd,       # 席の作業ディレクトリ = 議題ディレクトリ
                shell=False,
                **self._tree.spawn_kwargs(),
            )
        except OSError as exc:
            raise RelayError(f"spawn grok agent stdio failed: {exc}") from exc
        self._tree.adopt(self._proc)  # 以後 close() で木ごと回収できる
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_reader.start()
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "ai-round-table", "version": "0.2.0"},
            },
            timeout=TIMEOUT_INITIALIZE,
        )
        self._check_capabilities(result)
        # codex と違い `initialized` notification は不要 (実測: 送らずに session/new が通る)。

    def _check_capabilities(self, result: dict) -> None:
        """席の再開に必要な capability を実測して満たさなければ落とす。

        codex では版番号ゲート (`MIN_APP_SERVER_VERSION`) でこれをやっていた。grok は
        `initialize` が `agentCapabilities.loadSession` と
        `agentCapabilities.sessionCapabilities.resume` を自己申告するので、推測より確実。
        満たさない相手に席を作ると、2 ラウンド目に resume できず席が分裂する。
        """
        caps = result.get("agentCapabilities") or {}
        if not caps.get("loadSession"):
            raise RelayError(
                f"grok agent が loadSession を申告しない (席の再開ができない): {caps!r}"
            )
        session_caps = caps.get("sessionCapabilities") or {}
        if "resume" not in session_caps:
            raise RelayError(
                f"grok agent が sessionCapabilities.resume を申告しない: {session_caps!r}"
            )

    def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") is not None and msg.get("method"):
                # **agent → client の要求**。id を持つが応答ではない。
                # ここで振り分けないと、agent 側 id (実測: 0 から採番) が client 側 id と
                # 衝突して `_rpc` が許可要求を応答と誤読する。
                self._handle_request(msg)
                continue
            if msg.get("method"):
                continue  # notification (session/update / _x.ai/*) は捨てる
            self._q.put(msg)

    def _drain_stderr(self) -> None:
        """stderr を読み捨てる (最後の数行だけ残す)。

        読まないとパイプバッファが埋まった時点で席が書き込みでブロックする。実測でも
        stderr は必ず出るので、spike と同じく drain する構成にしておく。

        **RelayError の判定に stderr を使ってはいけない**: 成功した 4 spawn すべてで
        `ERROR worker quit with fatal: Transport channel closed, when
        Auth(AuthorizationRequired)` が出た (実測)。致命的に見えるが handshake も
        session も prompt も全部成功している。
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                self.stderr_tail.append(line.rstrip())
        except (OSError, ValueError):
            pass

    def _send_raw(self, obj: dict) -> None:
        """1 行送る。席が既に落ちていれば RelayError (黙って捨てない)。"""
        with self._wlock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                raise RelayError("grok agent の stdin が閉じている (送信できない)")
            proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            proc.stdin.flush()

    def _rpc(self, method: str, params: dict, timeout: float) -> dict:
        rid = self._next_id()
        self._send_raw({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self._q.get(timeout=0.2)
            except queue.Empty:
                proc = self._proc
                if proc is not None and proc.poll() is not None:
                    raise RelayError(f"{method}: grok agent が終了した rc={proc.returncode}")
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RelayError(f"{method}: {msg['error']}")
                return msg.get("result") or {}
        raise RelayError(f"{method}: timeout after {timeout}s")

    # ---- inbound request (承認) ---------------------------------------

    def _handle_request(self, msg: dict) -> None:
        """agent → client の JSON-RPC request に必ず答える。

        無視すると席が永久に止まる (実測)。未知の method もエラーで返し、
        「黙って捨てる」経路を作らない。

        呼び出し元は reader thread なので、書き込みが失敗しても例外を投げない
        (席が既に落ちている場合しか起きず、本線の `_rpc` が同じ失敗を検出する)。
        """
        method = msg.get("method")
        rid = msg.get("id")
        if method == "session/request_permission":
            reply = {
                "jsonrpc": "2.0", "id": rid,
                "result": {"outcome": self._decide_permission(msg.get("params") or {})},
            }
        else:
            self.permission_log.append(
                {"tool": None, "method": method, "decision": "unimplemented"}
            )
            reply = {
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"method not implemented: {method}"},
            }
        try:
            self._send_raw(reply)
        except (RelayError, OSError, ValueError):
            pass

    def _decide_permission(self, params: dict) -> dict:
        """許可要求に対する outcome を決める。

        判断材料は席が提示した `options` のみ。ここで独自の option を作らない
        (`optionId` は必ず要求に入っていたものを返す)。

        **書込境界についての注意**: これは OS サンドボックスではない。Windows では
        grok の sandbox は Platform Support 表に載っておらず (一次 docs)、実測でも席は
        `run_terminal_command` で PowerShell を任意実行できた。つまり許可を出した時点で
        席は議題ディレクトリの外も書ける。改ざん検知がこの事実に耐えるのは D12
        (git 一本化) 以降: 席がどこに書いても、dispatcher 所有ファイルへの書込は
        次操作の clean 検査で dirty として出る (ローカル git 履歴ごと書き換える相手への
        最終証跡は origin へ push した履歴)。
        許可ポリシーの設計 (execute を拒否するか / rawInput を検査するか) は未決で、
        ここでは「狭い方を選び、全件記録する」までに留める。
        """
        tool = (params.get("toolCall") or {}).get("kind")
        options = params.get("options") or []
        for kind in self.auto_allow_kinds:
            for option in options:
                if option.get("kind") == kind and option.get("optionId"):
                    self.permission_log.append(
                        {"tool": tool, "decision": option["optionId"], "kind": kind}
                    )
                    return {"outcome": "selected", "optionId": option["optionId"]}
        # 該当なし = 許可しない。turn は打ち切られるか席が別手段を探す。
        # cancelled は ACP の拒否形だが **この経路は未実測**。
        self.permission_log.append({"tool": tool, "decision": "cancelled", "kind": None})
        return {"outcome": "cancelled"}

    # ---- 席 ------------------------------------------------------------

    def _session_params(self) -> dict[str, Any]:
        """`session/new` / `session/load` 共通のパラメータ。

        codex の `sandbox` / `approvalPolicy` に相当するものは ACP に無い
        (一次 docs が挙げる `_meta` は rules / systemPromptOverride / agentProfile /
        yoloMode / autoMode のみ)。**cwd を議題ディレクトリに絞ることだけが効く境界**で、
        それも OS 強制ではない (`_decide_permission` の注意書きを参照)。
        """
        return {"cwd": self.cwd or os.getcwd(), "mcpServers": []}

    def _load_session(self, session_id: str) -> None:
        """既存 session をこのプロセスに読み込ませる (2 ラウンド目以降)。

        実測: 別プロセスで load 後に「直前に書いたパスを答えよ」と聞いたら round1 の
        パスが返り、会話が継続していることを `grok export` の全文でも確認した。
        プロセスあたり 1 回だけ撃てば足りる。
        """
        if session_id in self._loaded_sessions:
            return
        params = self._session_params()
        params["sessionId"] = session_id
        self._rpc("session/load", params, timeout=TIMEOUT_SESSION_LOAD)
        self._loaded_sessions.add(session_id)

    def _new_session(self, seat: dict) -> str:
        """新しい席を作り、seat に `thread_ref` (= grok の sessionId) を書き戻す。"""
        result = self._rpc("session/new", self._session_params(), timeout=TIMEOUT_SESSION_NEW)
        session_id = result.get("sessionId")
        if not session_id:
            raise RelayError(f"session/new returned no sessionId: {result!r}")
        seat["thread_ref"] = session_id
        self._loaded_sessions.add(session_id)  # 作った直後は load 済み
        return session_id

    def rename_seat(self, session_id: str, name: str) -> None:
        """席に名前を付ける — **未実装**。

        codex の `thread/name/set` に相当する ACP method / x.ai 拡張が、`initialize` の
        advertise にも一次 docs にも見当たらない (2026-08-07 実測)。推測でメソッドを
        撃つと席が壊れる可能性があるので実装しない。

        実測での代替: title は自動生成され (`Roundtable Grok Contrarian Opinion JSON Write`
        等)、`grok sessions list` は cwd スコープなので **議題ディレクトリが実質の
        グルーピング**になる。手動改名が要るなら TUI の `/rename`。
        """
        raise NotImplementedError(
            "grok ACP に席の rename 経路が実測できていない。"
            "席の識別は cwd (議題ディレクトリ) + 自動生成 title で行う。"
        )

    # ---- Relay 契約 -----------------------------------------------------

    def send(self, seat: dict, text: str) -> str:
        """packet を席へ届ける。

        codex (`turn/start` は投げっぱなし) と違い **`session/prompt` は turn 完了まで
        ブロックして返る** (実測 53.67-64.38s)。返った時点で席の成果物 (scratch の契約
        JSON) は書き終わっている。その分 `relay.save_seats` の競合窓が広がる点は
        `relay.py` の save_seats docstring と同じ話。

        許可の記録 (`seat["permission_log"]`) は **成功・失敗を問わず必ず書く**
        (`finally`)。成功時だけ書いていた旧実装には 2 方向の破綻があった
        (2026-08-07 レビュー H1 / 再現あり):

        - 前ラウンドの記録が残り続け、Tier3 に縮退した席に「今回も execute を許可した」
          という現実と無関係な記録が付く。
        - 逆に、**この round で実際に出した許可** (= 席が PowerShell を走らせた事実) が
          記録から消える。`_decide_permission` の docstring どおり記録が唯一の証跡
          なので、失敗時に消えると監査の意味を失う。

        失敗時は turn が完走していないので `permission_log_partial` を立てて区別する。
        """
        self.permission_log = []
        completed = False
        try:
            self._ensure()
            session_id = seat.get("thread_ref")
            if session_id:
                self._load_session(session_id)
            else:
                session_id = self._new_session(seat)
            result = self._rpc(
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
                timeout=TIMEOUT_PROMPT,
            )
            stop = result.get("stopReason") or "unknown"
            completed = True
            # stopReason は分類せずラベルに残す。実測できたのは "end_turn" だけで、
            # 他の値を失敗と決めつけると成功を Tier3 に落としかねない。
            return f"tier1-prompt:{stop}"
        except RelayError:
            self.close()
            raise
        except Exception as exc:  # 予期しない例外も縮退経路へ
            self.close()
            raise RelayError(str(exc)) from exc
        finally:
            # 何を許可したかを席メタに残す (CEO が後から読める形にする)。
            # **空でも書く**: 書かないと前ラウンドの記録が残り続ける。
            seat["permission_log"] = list(self.permission_log)
            if completed:
                seat.pop("permission_log_partial", None)
            else:
                # turn 未完のまま抜けた記録。「全部の許可要求を見た」とは言えない。
                seat["permission_log_partial"] = True

    def poll(self, seat: dict) -> str | None:
        # v0.2: 席の成果物は scratch JSON 契約のまま watcher が回収する。
        return None

    def close(self) -> None:
        """stdin close -> terminate -> wait -> プロセスツリー kill (codex 版と同型)。

        実測: deadlock した席を `taskkill /T /F` した時に 10 プロセスが連鎖終了した。
        親だけ殺すと孫が孤児として残る。
        """
        proc = self._proc
        tree = getattr(self, "_tree", None) or ProcessTree()
        self._proc = None
        # プロセスが死ねば session は落ちる。次の送付は load からやり直す。
        self._loaded_sessions.clear()
        if proc is None or proc.poll() is not None:
            tree.close()
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
            # 親が素直に終わっても子孫は残りうる。Job を閉じて木ごと回収する。
            tree.close()
            return
        except subprocess.TimeoutExpired:
            pass
        tree.kill_tree(proc.pid)
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        tree.close()
