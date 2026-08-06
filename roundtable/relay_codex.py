"""Codex 席向け Tier1: `codex app-server` stdio JSON-RPC。

CEO 禁止は「CLI で AI を実行する」こと。ここは既存/起動した app-server に turn を
送るだけで、対話履歴はアプリ側スレッドに残る想定 (DESIGN v6 §0 / §4 / spike)。

複数ラウンド: dispatch は 1 回ごとに別プロセスなので、2 ラウンド目の app-server は
前回の thread を持っていない。`seat["thread_ref"]` があれば turn/start の前に
`thread/resume` を撃ち、**同じ席 (= CEO が読んでいるチャット) を使い続ける**。

失敗時は RelayError を上げ、呼び出し側 (FallbackRelay) が Tier3 に縮退する。
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from typing import Any

from .relay import RelayError


# 実測 (2026-08-06, Windows / codex-cli 0.144.6):
#   initialize   6.66s
#   thread/start 20.9s
# 追加実測: idle 58.4s / 他プロセス並走時は 120s 超。ばらつきが非常に大きいので
# thread/start は 300s 枠にする。main の既定 (5s) では必ず timeout していた。
TIMEOUT_INITIALIZE = 60.0
TIMEOUT_THREAD_START = 300.0
# thread/resume は thread/start と同格の重さと見て同じ枠を取る (spike 実測では
# 数秒で返ったが、start が 20.9s かかる環境で resume だけ軽い保証はない)。
TIMEOUT_THREAD_RESUME = 300.0
TIMEOUT_THREAD_NAME = 30.0
TIMEOUT_TURN_START = 120.0


MIN_APP_SERVER_VERSION = (0, 144)
"""app-server が thread/start に応答する最低版 (実測)。

2026-08-06 実測: PATH 先頭に古い codex (0.130.0-alpha.5) があり、素の "codex" で
spawn するとそちらを掴む。その版の app-server は initialize には 1.3s で答えるが
**thread/start に一切応答しない** (60s 無応答)。timeout を伸ばしても直らない —
「遅い」のではなく「返さない」ため。バイナリを版で選ぶ必要がある。
"""


def _parse_version(text: str) -> tuple[int, ...]:
    """`codex-cli 0.144.6` → (0, 144, 6)。読めなければ空 tuple。"""
    for token in text.split():
        head = token.split("-", 1)[0]
        parts = head.split(".")
        if len(parts) >= 2 and all(x.isdigit() for x in parts[:2]):
            return tuple(int(x) for x in parts if x.isdigit())
    return ()


def _probe_version(path: str) -> tuple[int, ...]:
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    return _parse_version((out.stdout or "") + (out.stderr or ""))


def resolve_codex_binary(preferred: str | None = None) -> str:
    """app-server が使える codex を選ぶ。

    素の "codex" を信じない: PATH 先頭が古い版だと thread/start が無応答になり、
    Tier1 が「遅い」ではなく「絶対に届かない」状態になる (実測)。
    PATH 上の候補を全部見て、MIN_APP_SERVER_VERSION 以上の最初の 1 本を返す。
    見つからなければ最も新しい候補を返す (呼び出し側が RelayError で縮退できる)。
    """
    if preferred:
        return preferred
    candidates: list[str] = []
    seen: set[str] = set()
    for directory in (os.environ.get("PATH") or "").split(os.pathsep):
        if not directory:
            continue
        for name in ("codex.cmd", "codex.exe", "codex"):
            cand = os.path.join(directory, name)
            if cand in seen or not os.path.isfile(cand):
                continue
            seen.add(cand)
            candidates.append(cand)
    best: tuple[tuple[int, ...], str] | None = None
    for cand in candidates:
        ver = _probe_version(cand)
        if ver >= MIN_APP_SERVER_VERSION:
            return cand
        if ver and (best is None or ver > best[0]):
            best = (ver, cand)
    if best:
        return best[1]
    return candidates[0] if candidates else "codex"


class _ProcessTree:
    """spawn した app-server とその子孫をまとめて回収するための箱。

    親を terminate しても子孫は道連れにならない (Windows の TerminateProcess も
    POSIX の SIGTERM も直接の対象しか殺さない)。app-server は sandbox 内で
    コマンドを実行しうるので、親の終了経路とは別に「木ごと」の回収経路が要る。

    - Windows: Job Object + JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE。
      ハンドルを閉じた時点で木ごと確実に終わる (terminate が成功した経路でも取りこぼさない)。
    - POSIX: start_new_session=True で新しいプロセスグループにし、killpg で木ごと送る。
    """

    def __init__(self) -> None:
        self._job = None

    def spawn_kwargs(self) -> dict:
        """Popen に渡す追加引数 (POSIX のみプロセスグループを分離する)。"""
        return {} if os.name == "nt" else {"start_new_session": True}

    def adopt(self, proc: subprocess.Popen) -> None:
        """spawn 済みプロセスを木の管理下に置く (Windows のみ実体がある)。"""
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return

            class _BasicLimit(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _IoCounters(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in
                            ("ReadOperationCount", "WriteOperationCount",
                             "OtherOperationCount", "ReadTransferCount",
                             "WriteTransferCount", "OtherTransferCount")]

            class _ExtLimit(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _BasicLimit),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = _ExtLimit()
            info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            handle = int(proc._handle)  # type: ignore[attr-defined]
            if k32.AssignProcessToJobObject(job, handle):
                self._job = job
            else:
                k32.CloseHandle(job)
        except Exception:
            self._job = None  # Job が使えなくても kill_tree のフォールバックがある

    def kill_tree(self, pid: int) -> None:
        """まだ生きている子孫を強制終了する (terminate で死ななかった時)。"""
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    check=False, shell=False, capture_output=True, timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)  # POSIX: グループごと
        except (OSError, ProcessLookupError):
            pass

    def close(self) -> None:
        """Job を閉じる = Windows では木ごと確実に終わる。terminate 成功時の取りこぼし対策。"""
        if self._job is None:
            return
        try:
            import ctypes

            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._job)
        except Exception:
            pass
        self._job = None


class CodexAppServerRelay:
    tier = 1

    def __init__(self, binary: str | None = None, cwd: str | None = None):
        # 素の "codex" は PATH 先頭の古い版を掴みうる (thread/start 無応答) ため、
        # 版を見て選ぶ。明示指定があればそれを尊重する。
        self.binary = resolve_codex_binary(binary)
        self.cwd = cwd
        self._proc: subprocess.Popen[str] | None = None
        self._q: queue.Queue[dict] = queue.Queue()
        self._id = 0
        self._reader: threading.Thread | None = None
        self._tree = _ProcessTree()
        # このプロセスの app-server が保持している thread id。
        # app-server は再起動のたびに thread を失うので、プロセスの寿命と一致させる。
        self._loaded_threads: set[str] = set()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _ensure(self) -> None:
        if self._proc and self._proc.poll() is None:
            return
        self._tree = _ProcessTree()
        # 新しいプロセスは過去の thread を何も知らない。
        # ここを消し忘れると 2 プロセス目が resume を飛ばして turn/start を撃ち、
        # `-32600 thread not found` で落ちる (実測の失敗そのもの)。
        self._loaded_threads.clear()
        try:
            self._proc = subprocess.Popen(
                [self.binary, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",   # 未指定だと Windows は cp932 になり日本語 packet が壊れる
                errors="replace",
                bufsize=1,
                cwd=self.cwd,       # spawn 時の作業ディレクトリも席のスコープに寄せる
                **self._tree.spawn_kwargs(),
            )
        except OSError as exc:
            raise RelayError(f"spawn app-server failed: {exc}") from exc
        self._tree.adopt(self._proc)  # 以後 close() で木ごと回収できる
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._rpc(
            "initialize",
            {
                "clientInfo": {
                    "name": "ai-round-table",
                    "title": "ai-round-table",
                    "version": "0.2.0",
                },
                "capabilities": {},
            },
            timeout=TIMEOUT_INITIALIZE,
        )
        self._notify("initialized", {})

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
            self._q.put(msg)

    def _send_raw(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict) -> None:
        self._send_raw({"jsonrpc": "2.0", "method": method, "params": params})

    def _rpc(self, method: str, params: dict, timeout: float = 15) -> dict:
        rid = self._next_id()
        self._send_raw({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RelayError(f"{method}: {msg['error']}")
                return msg.get("result") or {}
        raise RelayError(f"{method}: timeout after {timeout}s")

    def _thread_params(self) -> dict[str, Any]:
        """thread/start と thread/resume で共通の安全パラメータ。

        sandbox / approvalPolicy は **必ず明示する**。省略するとサーバ既定に従い、
        実測では danger-full-access になりうる (= 席が repo 全体を書ける)。席の仕事は
        scratch に JSON を 1 本書くことだけなので workspace-write に絞り、cwd を議題
        ディレクトリにして書込範囲をそこへ閉じる。

        **resume 側でも同じものを送ること**。spike 実測では、read-only で開始した
        thread が sandbox 未指定の resume 後に dangerFullAccess に戻った (cwd は継承
        されるが sandbox は継承されない)。start だけ固めても 2 ラウンド目に穴が開くので、
        両経路をここから組み立てて drift を構造的に防ぐ。
        """
        params: dict[str, Any] = {
            "sandbox": "workspace-write",
            # 承認要求で無言停止しないため never。実効的な境界は上の sandbox。
            "approvalPolicy": "never",
        }
        if self.cwd:
            params["cwd"] = self.cwd
        return params

    def _resume_thread(self, thread_id: str) -> None:
        """既存 thread をこのプロセスの app-server に読み込ませる (複数ラウンド対応)。

        app-server は起動のたびに thread を保持しない。resume を挟まずに turn/start を
        撃つと `-32600 thread not found` で即失敗し、2 ラウンド目以降が丸ごと Tier3 へ
        縮退する (2026-08-06 実測)。プロセスあたり 1 回だけ撃てば足りる。
        """
        if thread_id in self._loaded_threads:
            return
        params = self._thread_params()
        params["threadId"] = thread_id
        self._rpc("thread/resume", params, timeout=TIMEOUT_THREAD_RESUME)
        self._loaded_threads.add(thread_id)

    def _start_thread(self, seat: dict) -> str:
        """新しい thread を作り、seat に thread_ref を書き戻す。"""
        # ephemeral は使わない: 一時席にすると会話がアプリ側に残らず、
        # 「CEO が席のチャットを直接読める」要件 (DESIGN v6 §0) を壊す。
        result = self._rpc("thread/start", self._thread_params(), timeout=TIMEOUT_THREAD_START)
        thread = result.get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise RelayError(f"thread/start returned no id: {result!r}")
        seat["thread_ref"] = thread_id
        self._loaded_threads.add(thread_id)  # 作った直後は load 済み = resume 不要
        name = seat.get("thread_name") or f"rt-{seat.get('topic', 'topic')}-codex"
        try:
            self._rpc(
                "thread/name/set",
                {"threadId": thread_id, "name": name},
                timeout=TIMEOUT_THREAD_NAME,
            )
        except RelayError:
            # 名前付け失敗は致命ではない
            pass
        return thread_id

    def send(self, seat: dict, text: str) -> str:
        try:
            self._ensure()
            thread_id = seat.get("thread_ref")
            if thread_id:
                # 2 ラウンド目以降。seats.json に残った thread_ref を同じ席として使い続ける。
                self._resume_thread(thread_id)
            else:
                thread_id = self._start_thread(seat)
            self._rpc(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": text}],
                },
                timeout=TIMEOUT_TURN_START,
            )
            return "tier1-sent"
        except RelayError:
            self.close()
            raise
        except Exception as exc:  # 予期しない例外も縮退経路へ
            self.close()
            raise RelayError(str(exc)) from exc

    def poll(self, seat: dict) -> str | None:
        # v0.2: 席の成果物は scratch JSON 契約のまま watcher が回収する。
        return None

    def close(self) -> None:
        """stdin close -> terminate -> wait -> プロセスツリー kill。

        app-server は sandbox 内でコマンドを実行しうるので、親だけ kill すると
        孫が孤児として残る。spike の「孤児なし」実測は素朴な 1 往復のみで、
        コマンド実行中の強制 close は検証していない。
        """
        proc = self._proc
        tree = getattr(self, "_tree", None) or _ProcessTree()
        self._proc = None
        # プロセスが死ねば thread は落ちる。次の送付では resume からやり直す。
        self._loaded_threads.clear()
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
            # 親が素直に終わっても子孫は残りうる (TerminateProcess / SIGTERM は
            # 直接の対象しか殺さない)。Job を閉じて木ごと回収する。
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
