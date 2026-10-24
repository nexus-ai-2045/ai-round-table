"""Tier1 relay 共通のプロセスツリー回収。

`relay_codex` で作った `_ProcessTree` を、Grok 席 (`relay_grok`) と共有するために
切り出したもの。実装は移設のみで挙動は変えていない (PR #5 P1-b / P1-c の対応内容)。

共有する理由は実測にある: deadlock した grok 席を `taskkill /T /F` した時に
**10 プロセス** (本体 + 子孫 9) が連鎖終了した (2026-08-07 spike)。席が tool で
子プロセスを起こすのは codex も grok も同じで、孤児回収の必要性も同じ。
"""
from __future__ import annotations

import os
import signal
import subprocess


class ProcessTree:
    """spawn した席プロセスとその子孫をまとめて回収するための箱。

    親を terminate しても子孫は道連れにならない (Windows の TerminateProcess も
    POSIX の SIGTERM も直接の対象しか殺さない)。席はサンドボックス内で
    コマンドを実行しうるので、親の終了経路とは別に「木ごと」の回収経路が要る。

    - Windows: Job Object + JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE。
      ハンドルを閉じた時点で木ごと確実に終わる (terminate が成功した経路でも取りこぼさない)。
    - POSIX: start_new_session=True で新しいプロセスグループにし、killpg で木ごと送る。
    """

    def __init__(self) -> None:
        self._job = None

    @property
    def managed(self) -> bool:
        """子孫を構造的に回収できる管理境界が成立しているか。"""
        return os.name != "nt" or self._job is not None

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
            if not k32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)
            ):
                k32.CloseHandle(job)
                return
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
            except Exception:
                # 回収は best effort。taskkill 自体やテスト用 Popen shim の例外で
                # 親プロセスの terminate 経路まで飛ばしてはいけない。
                pass
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)  # POSIX: グループごと
        except Exception:
            # killpg が失敗しても呼び出し元は親の terminate/kill を続ける。
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
