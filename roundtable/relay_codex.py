"""Tier1 relay — codex app-server を自前 spawn し stdio JSONL で席へ届ける。

Node 参照実装 (codex-client) と同じ 2 層構成:

    Transport       行区切り JSON の送受信 / id 相関 / 通知配送のみ (プロセスを知らない)
    CodexAppServer  initialize / thread / turn のラッパ (Transport を知る)
    CodexRelay      relay.Relay 契約の実装 (席と packet を知る)

**2026-08-05 spike で実測できたメソッドを送信経路の中核にする**。実測範囲:

    initialize → initialized (params なしの裸通知) → thread/start
    → thread/name/set → turn/start → turn/completed

実測事実 (spike-report より):
- framing は改行区切り JSON。ワイヤー上に "jsonrpc" フィールドは載せない
- sandbox はリクエストが hyphen 表記 ("workspace-write")、レスポンスが camelCase
- thread/name/updated の params は "name" ではなく **"threadName"**
- approvalPolicy="never" + sandbox="workspace-write" ではサーバー発リクエスト
  (承認要求) は 0 件だった。よって自動承認ロジックは書かない (来たら記録だけする)
- 些細なターンでも durationMs=105388 (105s)。timeout の桁は watcher (900s) に合わせる
- codex.cmd は shell=False で直接 Popen できる (WinError 193 は出ない)
"""
import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path

from .relay import RelayError

# spike で実測した既知パス。PATH と環境変数で見つからない時のみ使う。
_KNOWN_CODEX_PATHS = (
    "C:/Users/yas/AppData/Local/Programs/npm-global/codex.cmd",
    "C:/Users/yas/AppData/Local/Programs/npm-global/node_modules/@openai/codex"
    "/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe",
)

CLIENT_NAME = "ai_roundtable_dispatcher"
CLIENT_VERSION = "0.2.0"


class CodexAppServerError(RelayError):
    """app-server とのやり取りの失敗 (error 応答 / timeout / EOF)。"""


def find_codex_argv() -> list[str]:
    """`codex app-server --stdio` の argv を決める。

    優先順: 環境変数 ROUNDTABLE_CODEX_BIN → PATH の codex → spike 実測の既知パス。
    """
    env = os.environ.get("ROUNDTABLE_CODEX_BIN")
    if env and Path(env).exists():
        return [env, "app-server", "--stdio"]
    found = shutil.which("codex")
    if found:
        return [found, "app-server", "--stdio"]
    for cand in _KNOWN_CODEX_PATHS:
        if Path(cand).exists():
            return [cand, "app-server", "--stdio"]
    raise CodexAppServerError(
        "codex 実行ファイルが見つからない (ROUNDTABLE_CODEX_BIN / PATH / 既知パスすべて不発)"
    )


class Transport:
    """行区切り JSON (JSONL) の生トランスポート。

    プロセス管理を持たない — writer (書き込み可能な text stream) と
    reader (行 iterable な text stream) だけを受け取る。テストは実 CLI を
    起動せずに pipe を差し込める。

    メッセージ分類は Node 参照実装 handleLine と同じ 3 分岐:
        id + (result|error) → レスポンス (id で待ち合わせ中の caller に配送)
        method + id         → サーバー発リクエスト (on_message へ。自動応答はしない)
        method のみ         → 通知 (on_message へ)
    """

    def __init__(self, writer, reader, on_message=None, on_raw=None):
        self._writer = writer
        self._reader = reader
        self.on_message = on_message  # reader thread 上で呼ばれる (実装側で lock すること)
        self.on_raw = on_raw  # 証跡 hook: on_raw(direction, line)
        self._pending: dict[str, queue.SimpleQueue] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._eof = threading.Event()
        self._thread: threading.Thread | None = None

    # --- 受信側 ---------------------------------------------------------
    def start(self) -> None:
        """reader thread を起動する。on_message を設定し終えてから呼ぶこと。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._read_loop, name="codex-appserver-reader", daemon=True
        )
        self._thread.start()

    def _read_loop(self) -> None:
        try:
            for line in self._reader:
                line = line.strip()
                if not line:
                    continue
                if self.on_raw:
                    self.on_raw("recv", line)
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 契約外の行 (起動ログ等) は捨てる
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except (OSError, ValueError):
            pass  # pipe が閉じられた / 閉じ済み stream を読んだ
        finally:
            self._eof.set()
            self._wake_pending()

    def _dispatch(self, msg: dict) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            with self._lock:
                waiter = self._pending.pop(str(msg["id"]), None)
            if waiter is not None:
                waiter.put(msg)
            return
        if isinstance(msg.get("method"), str) and self.on_message:
            self.on_message(msg)

    def _wake_pending(self) -> None:
        """EOF 時に待ち中の全 caller を起こす (Node 実装 rejectAll 相当)。"""
        with self._lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for w in waiters:
            w.put(None)

    # --- 送信側 ---------------------------------------------------------
    def _write(self, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False)
        if self.on_raw:
            self.on_raw("send", raw)
        with self._write_lock:
            try:
                self._writer.write(raw + "\n")
                self._writer.flush()
            except (OSError, ValueError) as e:
                raise CodexAppServerError(f"app-server への書き込みに失敗: {e!r}") from e

    def notify(self, method: str, params=None) -> None:
        """通知を送る (id なし・応答を待たない)。"""
        msg: dict = {"method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def request(self, method: str, params=None, timeout: float = 120.0) -> dict:
        """リクエストを送り、同じ id のレスポンスが来るまで待って result を返す。"""
        with self._lock:
            rid = str(self._next_id)
            self._next_id += 1
            waiter: queue.SimpleQueue = queue.SimpleQueue()
            self._pending[rid] = waiter
        msg: dict = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._write(msg)
        except CodexAppServerError:
            with self._lock:
                self._pending.pop(rid, None)
            raise
        try:
            resp = waiter.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(rid, None)
            raise CodexAppServerError(f"{method}: 応答が {timeout}s 以内に来なかった") from None
        if resp is None:
            raise CodexAppServerError(f"{method}: 応答前に app-server の stdout が EOF")
        if "error" in resp:
            raise CodexAppServerError(f"{method}: app-server が error 応答: {resp['error']!r}")
        result = resp.get("result")
        return result if isinstance(result, dict) else {}


class CodexAppServer:
    """Transport の上の thread/turn ラッパ。

    terminate は「プロセス撤収を行う callable」。テストでは None を渡せる
    (Transport だけを pipe で差し替えて thread/turn の契約を検証できる)。
    """

    def __init__(self, transport: Transport, terminate=None):
        self._t = transport
        self._terminate = terminate
        self._lock = threading.Lock()
        self._turns: dict[str, dict] = {}  # turn_id -> turn/completed の turn オブジェクト
        self._turn_events: dict[str, threading.Event] = {}
        self._agent_deltas: list[str] = []
        self.server_requests: list[str] = []  # サーバー発リクエストの method (自動応答しない)
        self.stderr_tail: list[str] = []  # spawn 経由の時だけ埋まる
        self.initialized = False
        transport.on_message = self._on_message

    # --- 通知ハンドラ (reader thread 上で動く) ---------------------------
    def _on_message(self, msg: dict) -> None:
        method = msg["method"]
        if "id" in msg:
            # 承認要求などのサーバー発リクエスト。spike (approvalPolicy=never) では 0 件。
            # 自動承認は判断であって配達ではないため、この層では記録に留める。
            with self._lock:
                self.server_requests.append(method)
            return
        params = msg.get("params") or {}
        if method == "turn/completed":
            turn = params.get("turn") or {}
            turn_id = turn.get("id")
            if turn_id is None:
                return
            with self._lock:
                self._turns[turn_id] = turn
                ev = self._turn_events.get(turn_id)
                if ev is None:
                    ev = self._turn_events[turn_id] = threading.Event()
            ev.set()
        elif method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str):
                with self._lock:
                    self._agent_deltas.append(delta)

    # --- ライフサイクル ---------------------------------------------------
    @classmethod
    def spawn(cls, cwd, argv: list[str] | None = None, on_raw=None) -> "CodexAppServer":
        """codex app-server プロセスを起こして接続する。"""
        argv = argv or find_codex_argv()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
            )
        except OSError as e:
            raise CodexAppServerError(f"app-server の spawn に失敗 ({argv[0]}): {e!r}") from e

        # stderr は起動時に大量に出る (models cache / MCP auth の ERROR ログ = 致命ではない)。
        # drain しないと pipe が詰まってプロセスが止まるため専用スレッドで捨てる。
        stderr_tail: list[str] = []

        def _drain_stderr() -> None:
            try:
                for line in proc.stderr:
                    line = line.rstrip("\r\n")
                    if line:
                        stderr_tail.append(line)
                        del stderr_tail[:-50]
            except (OSError, ValueError):
                pass

        threading.Thread(target=_drain_stderr, name="codex-appserver-stderr", daemon=True).start()

        transport = Transport(proc.stdin, proc.stdout, on_raw=on_raw)
        server = cls(transport, terminate=lambda: _terminate_process(proc))
        server.stderr_tail = stderr_tail  # drain thread と同じ list 実体を共有する
        server.start()
        return server

    def start(self) -> None:
        self._t.start()

    def close(self) -> None:
        """プロセスを撤収する。冪等。"""
        terminate, self._terminate = self._terminate, None
        if terminate is not None:
            terminate()

    # --- プロトコル (spike 実測済みのメソッドのみ) -------------------------
    def initialize(self, timeout: float = 60.0) -> dict:
        """initialize → initialized。この接続で他メソッドを呼ぶ前に 1 回だけ。

        initialized は params を持たない裸の通知として受理される (spike 実測)。
        """
        result = self._t.request(
            "initialize",
            {
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "title": "ai-roundtable Tier1 relay",
                    "version": CLIENT_VERSION,
                }
            },
            timeout=timeout,
        )
        self._t.notify("initialized")
        self.initialized = True
        return result

    def start_thread(
        self,
        cwd,
        name: str | None = None,
        sandbox: str = "workspace-write",
        approval_policy: str = "never",
        timeout: float = 180.0,
    ) -> str:
        """thread/start (+ name があれば thread/name/set) して thread id を返す。

        sandbox / approvalPolicy はリクエスト側が hyphen 表記 (spike 実測。
        レスポンスは camelCase で返るが、送る文字列は hyphen が正)。
        approvalPolicy="never" は承認割り込みを構造的に発生させないための既定。
        """
        result = self._t.request(
            "thread/start",
            {
                "cwd": str(cwd).replace("\\", "/"),
                "sandbox": sandbox,
                "approvalPolicy": approval_policy,
                "sessionStartSource": "startup",
            },
            timeout=timeout,
        )
        thread = result.get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise CodexAppServerError(f"thread/start が thread.id を返さなかった: {result!r}")
        if name:
            self._t.request(
                "thread/name/set", {"threadId": thread_id, "name": name}, timeout=timeout
            )
        return thread_id

    def start_turn(self, thread_id: str, text: str, timeout: float = 120.0) -> str:
        """turn/start して turn id を返す。応答は即座に返る (status: inProgress)。"""
        with self._lock:
            self._agent_deltas.clear()
        result = self._t.request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": text}]},
            timeout=timeout,
        )
        turn = result.get("turn") or {}
        turn_id = turn.get("id")
        if not turn_id:
            raise CodexAppServerError(f"turn/start が turn.id を返さなかった: {result!r}")
        return turn_id

    def read_thread(
        self, thread_id: str, *, include_turns: bool = False, timeout: float = 120.0
    ) -> dict:
        """保存済み thread を再開せずに読む。

        closeout の判定材料を得る read-only API。README の stable protocol に従い、
        thread 自体が欠けた応答は成功扱いにしない。
        """
        result = self._t.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
            timeout=timeout,
        )
        thread = result.get("thread") or {}
        if thread.get("id") != thread_id:
            raise CodexAppServerError(
                f"thread/read が対象 thread を返さなかった: {result!r}"
            )
        return thread

    def archive_thread(self, thread_id: str, timeout: float = 120.0) -> None:
        """明示された thread を archive する。

        呼び出し側が成果物・残務・外部境界を検証した後だけ呼ぶ。失敗は例外として
        伝播させ、archive 済みと推測しない。
        """
        self._t.request(
            "thread/archive", {"threadId": thread_id}, timeout=timeout
        )

    def wait_turn(self, turn_id: str, timeout: float = 900.0) -> dict | None:
        """turn/completed を待って turn オブジェクトを返す。timeout なら None。

        turn.status は completed / interrupted / failed のいずれか。どれであっても
        「そのターンは終わった」の意味 (README + spike)。error 通知だけで終端扱いは
        しない — 実測していない振る舞いに賭けない。
        """
        with self._lock:
            done = self._turns.get(turn_id)
            if done is not None:
                return done
            ev = self._turn_events.get(turn_id)
            if ev is None:
                ev = self._turn_events[turn_id] = threading.Event()
        if not ev.wait(timeout):
            return None
        with self._lock:
            return self._turns.get(turn_id)

    def agent_message(self) -> str:
        """item/agentMessage/delta を連結した最終テキスト (診断用)。

        commentary と final_answer が連結される点に注意 (spike 実測)。回収の
        正本ではない — 成果物は scratch の JSON ファイルであり watcher が読む。
        """
        with self._lock:
            return "".join(self._agent_deltas)


def _terminate_process(proc: subprocess.Popen) -> int | None:
    """stdin close → terminate → wait(10s) → kill。孤児を残さない (spike 実測)。"""
    try:
        if proc.stdin:
            proc.stdin.close()
    except (OSError, ValueError):
        pass
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        return proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=10)


class CodexRelay:
    """Tier1 relay 実装 (relay.Relay 契約)。

    send() は turn/start が受理された時点で返る。turn/completed は待たない —
    成果物 (scratch の JSON) は完了より前に書かれるため、回収は watcher の
    ファイル polling に任せるのが正しい (2026-08-05 spike 実測)。

    thread id は seat を書き換えずに `self.thread_ref` で公開する。
    seats.json への保存は呼び出し側 (cli) の責務。
    """

    tier = 1

    def __init__(
        self,
        cwd=None,
        argv: list[str] | None = None,
        server_factory=None,
        close_grace_s: float = 60.0,
        request_timeout_s: float = 120.0,
        thread_timeout_s: float = 180.0,
        on_raw=None,
    ):
        self._cwd = Path(cwd) if cwd else Path.cwd()
        self._argv = argv
        self._server_factory = server_factory  # テスト用注入 (実 CLI を起こさない)
        self._close_grace_s = close_grace_s
        self._request_timeout_s = request_timeout_s
        self._thread_timeout_s = thread_timeout_s
        self._on_raw = on_raw
        self._server: CodexAppServer | None = None
        self._pending_turn: str | None = None
        self._thread_id: str | None = None  # 生存中の server で再利用する thread
        self.thread_ref: str | None = None  # seats.json 記録用 (close 後も残す)
        self.last_turn: dict | None = None

    def send(self, seat: dict, text: str) -> None:
        participant = seat.get("participant")
        if participant not in (None, "codex"):
            raise RelayError(
                f"CodexRelay は codex 席専用 (seat.participant={participant!r})。"
                "他席の Tier1 経路は未 spike"
            )
        server = self._ensure_server()
        thread_id = self._ensure_thread(seat, server)
        self._pending_turn = server.start_turn(thread_id, text, timeout=self._request_timeout_s)

    def close(self) -> None:
        """進行中ターンに猶予を与えてからプロセスを撤収する。冪等。

        turn 進行中に terminate しても孤児は残らないが (spike 実測)、席の
        作業を途中で切るため close_grace_s だけ turn/completed を待つ。
        """
        server, self._server = self._server, None
        turn_id, self._pending_turn = self._pending_turn, None
        self._thread_id = None  # thread_ref (公開値) は seats.json 用に残す
        if server is None:
            return
        try:
            if turn_id is not None:
                self.last_turn = server.wait_turn(turn_id, timeout=self._close_grace_s)
        finally:
            server.close()

    # --- 内部 ------------------------------------------------------------
    def _ensure_server(self) -> CodexAppServer:
        if self._server is not None:
            return self._server
        if self._server_factory is not None:
            server = self._server_factory()
        else:
            server = CodexAppServer.spawn(self._cwd, argv=self._argv, on_raw=self._on_raw)
        try:
            if not server.initialized:
                server.initialize(timeout=self._request_timeout_s)
        except BaseException:
            server.close()
            raise
        self._server = server
        return server

    def _ensure_thread(self, seat: dict, server: CodexAppServer) -> str:
        ref = seat.get("thread_ref")
        if self._thread_id is not None:
            if ref and ref != self._thread_id:
                raise NotImplementedError(
                    f"別 thread ({ref!r}) への切り替えは thread/resume が要る。"
                    "2026-08-05 spike で未実測のため実装しない — 席ごとに Relay を分けること"
                )
            return self._thread_id
        if ref:
            # 既存 thread への再接続 = thread/resume。README に記載はあるが spike で
            # 一度も投げていない。推測実装は「届いたつもり」を作るので明示的に落とす。
            raise NotImplementedError(
                f"既存 thread ({ref!r}) への再接続 (thread/resume) は 2026-08-05 spike 未実測。"
                "新しい thread で開始するか Tier3 に縮退すること"
            )
        topic = seat.get("topic") or "topic"
        name = f"rt-{topic}-{seat.get('participant') or 'codex'}"  # DESIGN v6 §3 の席名規約
        self._thread_id = server.start_thread(
            seat.get("cwd") or self._cwd, name=name, timeout=self._thread_timeout_s
        )
        self.thread_ref = self._thread_id
        return self._thread_id
