"""Codex 席向け Tier1: `codex app-server` stdio JSON-RPC。

CEO 禁止は「CLI で AI を実行する」こと。ここは既存/起動した app-server に turn を
送るだけで、対話履歴はアプリ側スレッドに残る想定 (DESIGN v6 §0 / §4 / spike)。

複数ラウンド: dispatch は 1 回ごとに別プロセスなので、2 ラウンド目の app-server は
前回の thread を持っていない。`seat["thread_ref"]` があれば turn/start の前に
`thread/resume` を撃ち、**同じ席 (= CEO が読んでいるチャット) を使い続ける**。

未送信が確定した失敗は RelayError を上げ、FallbackRelay が Tier3 に縮退する。
turn/start の timeout は受理済みの可能性があるため DeliveryUnknownError とし、自動再送しない。
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
import queue
import subprocess
import threading
import time
from typing import Any

from . import __version__
from .relay import DeliveryUnknownError, RelayError
from .relay_process import ProcessTree


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


@dataclass(frozen=True)
class _Version:
    release: tuple[int, ...]
    prerelease: str = ""

    def __str__(self) -> str:
        return ".".join(map(str, self.release)) + (
            "-" + self.prerelease if self.prerelease else ""
        )


def _version_key(version: _Version | tuple[int, ...]) -> tuple:
    """SemVer 順序。数値識別子は数値比較し、正式版は同番号の prerelease より後。"""
    if isinstance(version, _Version):
        release, pre = version.release, version.prerelease
    else:
        release, pre = version, ""
    release = release + (0,) * max(0, 3 - len(release))
    identifiers = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in pre.split(".") if part
    )
    return release, not bool(pre), identifiers


def _parse_version(text: str) -> _Version | tuple[int, ...]:
    """Codex の版表示を読み、prerelease を保持する。build metadata は順序に影響しない。"""
    for token in text.split():
        match = re.fullmatch(
            r"(\d+\.\d+(?:\.\d+)?)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
            r"(?:\+[0-9A-Za-z.-]+)?", token,
        )
        if match:
            return _Version(tuple(map(int, match[1].split("."))), match[2] or "")
    return ()


def _probe_version(path: str) -> _Version | tuple[int, ...]:
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    return _parse_version((out.stdout or "") + (out.stderr or ""))


class UnsupportedCodexError(RelayError):
    """codex の全候補が実測で MIN_APP_SERVER_VERSION 未満だった。

    RelayError を継承しているので FallbackRelay の既存 `except RelayError` が
    そのまま拾い、Tier3 へ即縮退する (分岐を足さずに済む)。
    """

    def __init__(self, path: str, version: _Version | tuple[int, ...]):
        self.path = path
        self.version = version
        shown = str(version) if isinstance(version, _Version) else ".".join(map(str, version)) or "unknown"
        self.version_text = shown
        need = ".".join(str(x) for x in MIN_APP_SERVER_VERSION)
        super().__init__(
            f"codex {shown} < {need} (app-server が thread/start に応答しない版): "
            f"{path}. 対応する候補が無いので Tier1 は成立しない"
        )


class RpcTimeoutError(RelayError):
    """JSON-RPC の応答期限超過。送信済みかどうかは method ごとに判断する。"""

    def __init__(self, method: str, timeout: float):
        self.method = method
        self.timeout = timeout
        super().__init__(f"{method}: timeout after {timeout}s")


class RpcResponseError(RelayError):
    """app-server が返した構造化 JSON-RPC error。"""

    def __init__(self, method: str, error: object):
        self.method = method
        self.error = error
        super().__init__(f"{method}: {error}")


def resolve_codex_binary(preferred: str | None = None) -> str:
    """app-server が使える codex を選ぶ。

    素の "codex" を信じない: PATH 先頭が古い版だと thread/start が無応答になり、
    Tier1 が「遅い」ではなく「絶対に届かない」状態になる (実測)。
    全候補を調べ、MIN_APP_SERVER_VERSION 以上の最新版を返す。

    候補は PATH に加えて **Desktop アプリ同梱の codex** (`%LOCALAPPDATA%/OpenAI/Codex/
    bin/<hash>/codex.exe`、PATH には載らない) も見る。対応版が複数あれば **最も新しい
    版** を選ぶ。PATH 順で最初の対応版を返すと、`~/.codex` の state を最新版が書き換えた
    後に古い対応版が読めなくなる: 2026-09-09 に Desktop 側 0.154 が legacy→paginated
    移行を走らせ、PATH 先頭の 0.144.6 では `thread/resume` が
    `-32601 paginated_threads is not supported yet` で落ちた (2026-09-12 実測。
    0.154 では同じ thread が resume できた)。state の所有者 = 最新版なので版で選ぶ。

    候補が全部「実測で下回っていた」場合は **UnsupportedCodexError を即上げる**。
    以前は最も新しい古版を返していたが、それだと呼び出し側は
    initialize に成功したあと thread/start の 300s 枠を丸ごと待ってから縮退し、
    doctor も 180s 待ってから既知の非互換を報告する。届かないと分かっている相手を
    待つ理由が無いので、解決の時点で落として縮退と診断の応答性を守る。

    版が**読めなかった**候補は落とさない (`--version` の出力形式が変わっただけの
    可能性があり、古いという積極的な証拠が無い)。読めない候補が 1 本でもあれば
    それを返して実際に喋らせる。
    """
    if preferred:
        return preferred
    candidates: list[str] = []
    seen: set[str] = set()
    for directory in _candidate_dirs():
        for name in ("codex.cmd", "codex.exe", "codex"):
            cand = os.path.join(directory, name)
            if cand in seen or not os.path.isfile(cand):
                continue
            seen.add(cand)
            candidates.append(cand)
    newest_ok: tuple[_Version | tuple[int, ...], str] | None = None
    newest_old: tuple[_Version | tuple[int, ...], str] | None = None
    unknown: str | None = None
    # probe は互いに独立。順序を保つ map で同版時の PATH 優先を維持する。
    # 同時実行を 8 本に制限し、遅い候補ごとの 30 秒直列待ちを避ける。
    with ThreadPoolExecutor(max_workers=8) as executor:
        versions = list(executor.map(_probe_version, candidates))
    for cand, ver in zip(candidates, versions):
        if ver and _version_key(ver) >= _version_key(MIN_APP_SERVER_VERSION):
            # 同版なら先に見つけた方 (PATH 優先) を保つため > で比較する
            if newest_ok is None or _version_key(ver) > _version_key(newest_ok[0]):
                newest_ok = (ver, cand)
        elif ver:
            if newest_old is None or _version_key(ver) > _version_key(newest_old[0]):
                newest_old = (ver, cand)
        elif unknown is None:
            unknown = cand
    if newest_ok:
        return newest_ok[1]
    if unknown:
        return unknown
    if newest_old:
        raise UnsupportedCodexError(newest_old[1], newest_old[0])
    return "codex"  # 候補ゼロ。spawn 時に OSError で速く落ちる


def _candidate_dirs() -> list[str]:
    """codex を探すディレクトリ。PATH の後ろに Desktop アプリの bin (hash 配下) を足す。

    hash ディレクトリ名は更新ごとに変わるので glob で拾う。`%LOCALAPPDATA%` が無い
    環境 (CI の Linux など) では PATH だけになる。
    """
    dirs = [d for d in (os.environ.get("PATH") or "").split(os.pathsep) if d]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        app_bin = os.path.join(local, "OpenAI", "Codex", "bin")
        try:
            entries = sorted(os.listdir(app_bin))
        except OSError:
            entries = []
        dirs.append(app_bin)
        dirs.extend(os.path.join(app_bin, e) for e in entries)
    return dirs


# プロセスツリー回収は Grok 席と共通 (roundtable/relay_process.py へ移設)。
# 旧名 `_ProcessTree` はこのモジュール内の参照 / 既存テストの monkeypatch 経路を
# 維持するための別名で、実体は共有クラス 1 つだけ。
_ProcessTree = ProcessTree


class CodexAppServerRelay:
    tier = 1

    def __init__(self, binary: str | None = None, cwd: str | None = None):
        # 素の "codex" は PATH 先頭の古い版を掴みうる (thread/start 無応答) ため、
        # 版を見て選ぶ。明示指定があればそれを尊重する。
        self._resolution_error: UnsupportedCodexError | None = None
        try:
            self.binary = resolve_codex_binary(binary)
        except UnsupportedCodexError as exc:
            # 未送信エラーは send 内で上げ、FallbackRelay の記録と縮退を通す。
            self.binary = "codex"
            self._resolution_error = exc
        self.cwd = cwd
        self._proc: subprocess.Popen[str] | None = None
        self._q: queue.Queue[dict] = queue.Queue()
        self._id = 0
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._rpc_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._tree = _ProcessTree()
        # このプロセスの app-server が保持している thread id。
        # app-server は再起動のたびに thread を失うので、プロセスの寿命と一致させる。
        self._loaded_threads: set[str] = set()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _ensure(self) -> None:
        if self._resolution_error is not None:
            raise self._resolution_error
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
        self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_reader.start()
        self._rpc(
            "initialize",
            {
                "clientInfo": {
                    "name": "ai-round-table",
                    "title": "ai-round-table",
                    "version": __version__,
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

    def _drain_stderr(self) -> None:
        """stderr PIPE を常時 drain し、バッファ満杯による停止を防ぐ。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for _line in proc.stderr:
            pass

    def _send_raw(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict) -> None:
        self._send_raw({"jsonrpc": "2.0", "method": method, "params": params})

    def _rpc(self, method: str, params: dict, timeout: float = 15) -> dict:
        # 現行 transport は単一 queue。並行 RPC は互いの応答を奪うため直列化する。
        with self._rpc_lock:
            return self._rpc_locked(method, params, timeout)

    def _rpc_locked(self, method: str, params: dict, timeout: float) -> dict:
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
                    raise RpcResponseError(method, msg["error"])
                return msg.get("result") or {}
        raise RpcTimeoutError(method, timeout)

    @staticmethod
    def _is_thread_not_found(exc: RpcResponseError) -> bool:
        return "thread not found" in str(exc.error).lower()

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
        # 同一 relay の二重 spawn / thread 操作競合を構造的に防ぐ。
        with self._send_lock:
            return self._send_locked(seat, text)

    def _send_locked(self, seat: dict, text: str) -> str:
        try:
            self._ensure()
            thread_id = seat.get("thread_ref")
            if thread_id:
                # 2 ラウンド目以降。seats.json に残った thread_ref を同じ席として使い続ける。
                try:
                    self._resume_thread(thread_id)
                except RpcResponseError as exc:
                    if self._is_thread_not_found(exc):
                        # 削除・別 profile の stale 席だけは一度だけ新規作成して自己回復する。
                        seat.pop("thread_ref", None)
                        thread_id = self._start_thread(seat)
                    else:
                        raise DeliveryUnknownError(
                            f"thread/resume continuity unknown; automatic Tier3 fallback disabled: {exc}"
                        ) from exc
                except RelayError as exc:
                    # resume timeout 等では既存席を利用可能か判定できない。新規席や
                    # Tier3へ自動分岐すると、CEOが見る会話履歴を分裂させる。
                    raise DeliveryUnknownError(
                        f"thread/resume continuity unknown; automatic Tier3 fallback disabled: {exc}"
                    ) from exc
            else:
                thread_id = self._start_thread(seat)
            try:
                self._rpc(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": text}],
                    },
                    timeout=TIMEOUT_TURN_START,
                )
            except RpcResponseError:
                # app-server が構造化 error を返した = request の失敗は確定している。
                # 送達不明へ包まず RelayError のまま Tier3 縮退経路へ返す。
                raise
            except Exception as exc:
                # response が無い timeout・transport 切断は、app-server が受理済みかを
                # クライアント側から証明できない。
                raise DeliveryUnknownError(
                    f"turn/start delivery unknown; automatic Tier3 fallback disabled: {exc}"
                ) from exc
            return "tier1-sent"
        except DeliveryUnknownError:
            # turn が動作中かもしれない。CLI の回収待ちが終わるまで process tree を保つ。
            raise
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
        if not tree.managed:
            # Windows Job Object の設定/割当失敗時も子孫を先に回収する。
            tree.kill_tree(proc.pid)
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
