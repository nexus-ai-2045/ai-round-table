"""tmp → os.replace の atomic 書き込み 1 本だけを持つ最下層 module。

もとは witness 層 (旧 integrity.py) との import 循環を切るために minutes.py から
切り出した (2026-08-07 / レビュー H3)。witness 層は D12 で git に置き換えられて
消えたが、最下層に atomic_write を置く構造は ledger.write_state / minutes /
snapshot が共有しており、そのまま残す。`minutes.atomic_write` は re-export で
残してある (既存の呼び出し側 API を壊さない)。
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

_REPLACE_ATTEMPTS = 8
"""os.replace を諦めるまでの試行回数。

Windows では **読み取り中のファイルへの os.replace が WinError 5 (アクセス拒否)**
になる。dispatcher 側の読みはロック配下に寄せた (レビュー H1/H2) が、席・AV・
エディタなど外部プロセスの読みは制御できないので、待って再試行する余地を残す。
旧値は 5 回 / 累計 1.0 秒で、並行 dispatch の実測 (repro3) では使い切って
`PermissionError` が dispatch ごと落としていた。
"""

_REPLACE_BACKOFF_S = 0.05
"""再試行の初期待ち。以降 2 倍ずつ増やし _REPLACE_BACKOFF_MAX_S で頭打ちにする。"""

_REPLACE_BACKOFF_MAX_S = 0.5


def atomic_write(path: Path, text: str) -> None:
    """tmp に書いて os.replace。PermissionError は backoff 付きで再試行する。

    改行は `newline="\\n"` 固定 = 無変換。改行変換が入ると「書いた内容」と
    「ディスクの内容」がズレて、git の clean 検査 (D12) が書いた本人の直後に
    dirty を報告する偽陽性になる (旧 witness 時代の hash 不一致と同型)。
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        wait = _REPLACE_BACKOFF_S
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(wait)
                wait = min(wait * 2, _REPLACE_BACKOFF_MAX_S)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
