"""Codex 席向け Tier1: `codex app-server` stdio JSON-RPC。

CEO 禁止は「CLI で AI を実行する」こと。ここは既存/起動した app-server に turn を
送るだけで、対話履歴はアプリ側スレッドに残る想定 (DESIGN v6 §0 / §4 / spike)。

失敗時は RelayError を上げ、呼び出し側 (FallbackRelay) が Tier3 に縮退する。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from typing import Any

from .relay import RelayError


def _kill_process_tree(pid: int) -> None:
    """子孫ごと終了させる (Windows: taskkill /T)。無い環境では親だけ kill に任せる。"""
    if os.name != "nt":
        return
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            check=False, shell=False, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


class CodexAppServerRelay:
    tier = 1

    def __init__(self, binary: str = "codex", cwd: str | None = None):
        self.binary = binary
        self.cwd = cwd
        self._proc: subprocess.Popen[str] | None = None
        self._q: queue.Queue[dict] = queue.Queue()
        self._id = 0
        self._reader: threading.Thread | None = None

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _ensure(self) -> None:
        if self._proc and self._proc.poll() is None:
            return
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
            )
        except OSError as exc:
            raise RelayError(f"spawn app-server failed: {exc}") from exc
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
            timeout=8,
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

    def send(self, seat: dict, text: str) -> str:
        try:
            self._ensure()
            thread_id = seat.get("thread_ref")
            if not thread_id:
                params: dict[str, Any] = {}
                if self.cwd:
                    params["cwd"] = self.cwd
                # sandbox / approvalPolicy は **必ず明示する**。省略するとサーバ既定に
                # 従い、実測では danger-full-access になりうる (= 席が repo 全体を書ける)。
                # 席の仕事は scratch に JSON を 1 本書くことだけなので workspace-write に
                # 絞り、cwd を議題ディレクトリにして書込範囲をそこへ閉じる。
                params["sandbox"] = "workspace-write"
                # 承認要求で無言停止しないため never。実効的な境界は上の sandbox。
                params["approvalPolicy"] = "never"
                # ephemeral は使わない: 一時席にすると会話がアプリ側に残らず、
                # 「CEO が席のチャットを直接読める」要件 (DESIGN v6 §0) を壊す。
                # 応答なし環境でも長待ちしない。失敗は FallbackRelay が Tier3 へ。
                result = self._rpc("thread/start", params, timeout=5)
                thread = result.get("thread") or {}
                thread_id = thread.get("id")
                if not thread_id:
                    raise RelayError(f"thread/start returned no id: {result!r}")
                seat["thread_ref"] = thread_id
                name = seat.get("thread_name") or f"rt-{seat.get('topic', 'topic')}-codex"
                try:
                    self._rpc(
                        "thread/name/set",
                        {"threadId": thread_id, "name": name},
                        timeout=5,
                    )
                except RelayError:
                    # 名前付け失敗は致命ではない
                    pass
            self._rpc(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": text}],
                },
                timeout=20,
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
        self._proc = None
        if proc is None or proc.poll() is not None:
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
            return
        except subprocess.TimeoutExpired:
            pass
        _kill_process_tree(proc.pid)
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
