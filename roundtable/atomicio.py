"""tmp → os.replace の atomic 書き込み 1 本だけを持つ最下層 module。

**なぜ minutes.py から切り出したか** (2026-08-07 / レビュー H3): minutes.md を
journal.json と同じ hash 証跡 (integrity.py) で守るには minutes → integrity の
依存が要る。ところが integrity は atomic 書き込みのために minutes を import して
いたので循環になる。両者が共通で必要とするのは atomic_write 1 本だけなので、
それを最下層へ落とす。`minutes.atomic_write` は re-export で残してある
(既存の呼び出し側 API を壊さない)。
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

    改行は `newline="\\n"` 固定 = 無変換。hash 証跡 (integrity) は
    `text.encode("utf-8")` の digest を記録するので、ここで改行変換が入ると
    書いた本人の hash が合わなくなる。
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
