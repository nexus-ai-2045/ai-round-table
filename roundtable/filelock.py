"""状態ファイル書き換え用の短時間排他ロック (O_CREAT|O_EXCL + stale 回収)。

**なぜロックだけでは足りず、なぜそれでもロックを置くのか**

診断 (2026-08-07) の fix_options では C (排他ロック) を単独では非推奨としている。
dispatch の寿命 (最大 900 秒) を通してロックを握るのは不可能で、握れない以上
「ロード時のスナップショットが古い」問題は残るからである。したがって本 module の
ロックは *save の read-modify-write を不可分にする* ためだけに使う。保持時間は
「読み直し → 重ね合わせ → 書き戻し → hash 記録」の数ミリ秒に限る。
失われた更新を防ぐ本体は journal 側の重ね合わせ (fix_options A) であり、ここは
その critical section を守る補助に徹する。

**既存の短時間 FileLock が msvcrt.locking / fcntl.flock でない理由**

Windows と POSIX で API と意味論が割れる上、「ロックを握ったままプロセスが死んだ」
時の回収挙動が OS 実装依存になる。O_CREAT|O_EXCL は両 OS で同じ意味を持ち、
残骸の回収条件 (mtime による stale 判定) を自分のコードに書ける。この repo の思想は
「防止でなく検知」なので、回収条件が読める方を採る。

長い内側ロック待機を含む invocation には、この時刻前提を適用しない。
下の AdvisoryFileLock が OS の所有権・死亡時解放で別に扱う。
"""
from __future__ import annotations

import errno
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

LOCK_TIMEOUT_S = 120.0
"""取得を諦めるまでの秒数。STALE_AFTER_S より長くする。

短くすると「残骸を回収する前に諦める」ため、一度死んだプロセスの残骸で
以後の全 dispatch が止まる。回収窓を必ず含む長さにしておく。

2026-08-10 (D12) に 60 → 120 へ拡大: 保持区間に git 呼び出し (clean 検査 +
commit、Windows 実測 ~0.1-0.3s) が入ったため、高並列 (8 writer 級) では
直列合計が 60s を超えうる。正常保持は依然 1s 未満で、STALE_AFTER_S (30s) の
「正常保持と残骸を取り違えない」余裕は変わらない。
"""

STALE_AFTER_S = 30.0
"""この秒数より古いロックは残骸とみなして回収する。

正常な保持時間はミリ秒 (read-modify-write 1 回分) なので、30 秒は
「正常な保持と残骸を取り違えない」ための十分な余裕。
"""


class LockTimeout(RuntimeError):
    """ロックを取得できなかった。呼び出し側は失敗として記録すること (黙って書かない)。"""


class FileLock:
    """with 文で使う排他ロック。取得できなければ LockTimeout を上げる。"""

    def __init__(
        self,
        path: Path,
        timeout_s: float = LOCK_TIMEOUT_S,
        stale_after_s: float = STALE_AFTER_S,
        clock=time,
    ):
        self.path = Path(path)
        self.timeout_s = timeout_s
        self.stale_after_s = stale_after_s
        self._clock = clock
        self._held = False

    def _reclaim_if_stale(self) -> bool:
        """残骸ロックを消す。消したら True。

        判定は mtime のみ。pid 生存確認はしない: pid は再利用されるので
        「生きている pid = そのロックの持ち主」は成り立たず、確認したつもりで
        誤った安心を作る方が危ない。
        """
        try:
            age = self._clock.time() - self.path.stat().st_mtime
        except OSError:
            return False
        if age < self.stale_after_s:
            return False
        try:
            os.unlink(self.path)
            return True
        except OSError:
            return False  # 別プロセスが先に回収した / 権限なし

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = self._clock.monotonic() + self.timeout_s
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                self._reclaim_if_stale()
                if self._clock.monotonic() >= deadline:
                    raise LockTimeout(
                        f"lock 取得に失敗 ({self.timeout_s}s): {self.path}"
                    )
                self._clock.sleep(0.02)
                continue
            except OSError as exc:  # 共有違反 (Windows) 等も待って再試行
                if self._clock.monotonic() >= deadline:
                    raise LockTimeout(f"lock 取得に失敗: {self.path} ({exc})") from exc
                self._clock.sleep(0.02)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(
                    {"pid": os.getpid(), "at": datetime.now(timezone.utc).isoformat()},
                    f,
                    ensure_ascii=False,
                )
            self._held = True
            return self

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            os.unlink(self.path)
        except OSError:
            pass  # 既に回収済み (stale 判定された) — 書き込み自体は完了している

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()


class AdvisoryFileLock:
    """invocation 用 OS 排他。生存 holder を時刻だけで失効させない。

    invocation の区間は内側の minutes/journal ロック待機を含み、従来の
    FileLock の短時間前提を満たさない。POSIX flock / Windows byte lock は
    descriptor close・プロセス死亡で OS が解放する。PID 推測を使わない。
    ファイルを unlink すると同じ名前の別 inode を同時にロックできるため、
    release 後も空のロックファイルを残す。ローカル filesystem を対象とする。
    """

    def __init__(self, path: Path, timeout_s: float = LOCK_TIMEOUT_S, clock=time):
        self.path = Path(path)
        self.timeout_s = timeout_s
        self._clock = clock
        self._fd: int | None = None

    @staticmethod
    def _try_lock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def acquire(self) -> "AdvisoryFileLock":
        if self._fd is not None:
            raise RuntimeError("lock already held by this instance")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Never truncate/unlink: all contenders must lock the same inode/byte.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = self._clock.monotonic() + self.timeout_s
        try:
            # Windows byte-range locking needs a byte at offset zero.
            if os.fstat(fd).st_size == 0:
                try:
                    os.write(fd, b"\0")
                except PermissionError:
                    # Another Windows opener may have initialized and locked it
                    # since fstat; the normal lock attempt below arbitrates.
                    pass
            while True:
                try:
                    self._try_lock(fd)
                    self._fd = fd
                    return self
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if self._clock.monotonic() >= deadline:
                        raise LockTimeout(f"invocation lock 取得に失敗: {self.path} ({exc})") from exc
                    self._clock.sleep(0.02)
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)

    def __enter__(self) -> "AdvisoryFileLock":
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()
