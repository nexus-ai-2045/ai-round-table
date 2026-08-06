"""環境診断 — Tier1 が使えるかを短時間で判定する (軸 A の前提 detector)。

設計方針 (MPC / FDE):
  - Desktop 接続・thread/start 修復はここではやらない
  - 観測可能な事実だけを返す: binary / control socket / initialize / list / start
  - 推奨 tier を機械的に出す (自己申告にしない)
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class DoctorReport:
    codex_binary: str | None
    desktop_control_socket: str | None
    desktop_socket_exists: bool
    proxy_connect: str  # ok | missing | error:<msg>
    spawn_initialize: str  # ok | error:<msg>
    thread_list: str  # ok:<n> | error:<msg> | skipped
    thread_start: str  # ok | timeout | error:<msg> | skipped
    recommended_tier: int
    real_seat_path: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def default_control_socket() -> Path:
    home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(home) / "app-server-control" / "app-server-control.sock"


def _which_codex(binary: str | None = "codex") -> str | None:
    """app-server が使える codex を選ぶ。

    素の shutil.which("codex") は PATH 先頭を返すが、そこに古い版 (0.130 系) が
    あると **thread/start に永久に応答しない** (2026-08-06 実測)。doctor がその
    バイナリで診断すると、環境が正常でも必ず「Tier1 不可・Tier3 推奨」と誤答する。
    版を見て選ぶ resolve_codex_binary に委譲する。
    """
    from .relay_codex import resolve_codex_binary

    if binary and binary != "codex":
        return shutil.which(binary) or binary  # 明示指定は尊重する
    resolved = resolve_codex_binary()
    return resolved if Path(resolved).exists() else shutil.which(resolved)


def _probe_proxy(binary: str, sock: Path, timeout: float = 2.0) -> str:
    if not sock.exists():
        return "missing"
    try:
        proc = subprocess.Popen(
            [binary, "app-server", "proxy", "--sock", str(sock)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return f"error:{exc}"
    try:
        # proxy は websocket で、接続失敗時は即 exit する (2026-08-07 実測)
        try:
            _, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return "ok"  # 接続して生き続けた = ソケットは開いている可能性
        if proc.returncode == 0:
            return "ok"
        msg = (err or "").strip().splitlines()
        return f"error:{msg[0] if msg else f'exit {proc.returncode}'}"
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


def _stdio_session(binary: str, cwd: str | None = None):
    """短い stdio app-server セッション。caller が必ず close すること。"""
    proc = subprocess.Popen(
        [binary, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=cwd,
    )
    q: queue.Queue[dict] = queue.Queue()

    def reader() -> None:
        assert proc.stdout
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                q.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    threading.Thread(target=reader, daemon=True).start()
    return proc, q


def _rpc(proc, q: queue.Queue, rid: int, method: str, params: dict, timeout: float) -> dict | None:
    assert proc.stdin
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
    proc.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg = q.get(timeout=0.2)
        except queue.Empty:
            continue
        if msg.get("id") == rid:
            return msg
    return None


def _notify(proc, method: str, params: dict) -> None:
    assert proc.stdin
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
    proc.stdin.flush()


def run_doctor(
    binary: str | None = None,
    *,
    probe_start: bool = True,
    # 実測 (2026-08-06): thread/start は idle 20.9-58.4s、負荷時 120s 超。
    # 5.0 では必ず timeout し、環境が正常でも Tier1 を no-go と誤判定する。
    start_timeout: float = 180.0,
    cwd: str | None = None,
) -> DoctorReport:
    notes: list[str] = []
    path = _which_codex(binary)
    sock = default_control_socket()
    sock_exists = sock.exists()
    proxy = _probe_proxy(path, sock) if path else "error:codex not found"

    spawn_init = "skipped"
    thread_list = "skipped"
    thread_start = "skipped"

    if path:
        proc = None
        try:
            proc, q = _stdio_session(path, cwd=cwd)  # 解決済みの実体を使う (binary は要求値)
            r1 = _rpc(
                proc,
                q,
                1,
                "initialize",
                {
                    "clientInfo": {
                        "name": "ai-round-table-doctor",
                        "title": "doctor",
                        "version": "0.2.2",
                    },
                    "capabilities": {},
                },
                timeout=8,
            )
            if r1 and "result" in r1:
                spawn_init = "ok"
                _notify(proc, "initialized", {})
                r2 = _rpc(proc, q, 2, "thread/list", {}, timeout=8)
                if r2 and "result" in r2:
                    data = (r2.get("result") or {}).get("data") or []
                    thread_list = f"ok:{len(data)}"
                elif r2 and "error" in r2:
                    thread_list = f"error:{r2['error']}"
                else:
                    thread_list = "error:timeout"
                if probe_start:
                    params: dict = {"approvalPolicy": "never", "sandbox": "workspace-write"}
                    if cwd:
                        params["cwd"] = cwd
                    r3 = _rpc(proc, q, 3, "thread/start", params, timeout=start_timeout)
                    if r3 and "result" in r3:
                        thread_start = "ok"
                    elif r3 and "error" in r3:
                        thread_start = f"error:{r3['error']}"
                    else:
                        thread_start = "timeout"
                        notes.append(
                            "thread/start が無応答 (stdio spawn)。Desktop control socket も無い/使えない場合は Tier3 本線。"
                        )
            else:
                spawn_init = "error:timeout or no result"
        except OSError as exc:
            spawn_init = f"error:{exc}"
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
    else:
        notes.append("codex binary が PATH に無い")

    if not sock_exists:
        notes.append("Desktop app-server control socket が無い (Windows で未作成が観測済み)")
    if proxy.startswith("error:"):
        notes.append(f"proxy 接続失敗: {proxy}")

    # 推奨: start が ok のときだけ tier1。それ以外は tier3。
    if thread_start == "ok":
        recommended = 1
        real_seat = "tier1-app-server"
    else:
        recommended = 3
        real_seat = "tier3-clipboard-paste"

    return DoctorReport(
        codex_binary=path,
        desktop_control_socket=str(sock),
        desktop_socket_exists=sock_exists,
        proxy_connect=proxy,
        spawn_initialize=spawn_init,
        thread_list=thread_list,
        thread_start=thread_start,
        recommended_tier=recommended,
        real_seat_path=real_seat,
        notes=notes,
    )


def format_report(report: DoctorReport) -> str:
    lines = [
        f"codex_binary: {report.codex_binary or '(not found)'}",
        f"desktop_control_socket: {report.desktop_control_socket}",
        f"desktop_socket_exists: {report.desktop_socket_exists}",
        f"proxy_connect: {report.proxy_connect}",
        f"spawn_initialize: {report.spawn_initialize}",
        f"thread_list: {report.thread_list}",
        f"thread_start: {report.thread_start}",
        f"recommended_tier: {report.recommended_tier}",
        f"real_seat_path: {report.real_seat_path}",
    ]
    if report.notes:
        lines.append("notes:")
        for n in report.notes:
            lines.append(f"  - {n}")
    return "\n".join(lines)
