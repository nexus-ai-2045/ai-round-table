"""生存 invocation lock とプロセス死亡回復。OS 実ロックを使う。"""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from roundtable.filelock import AdvisoryFileLock, LockTimeout


def test_live_holder_is_not_reclaimed_by_old_mtime(tmp_path):
    path = tmp_path / "inv.lock"
    with AdvisoryFileLock(path):
        os.utime(path, (1, 1))
        with pytest.raises(LockTimeout):
            AdvisoryFileLock(path, timeout_s=0).acquire()
    assert path.exists()  # inode を消して並行 holder を作らない。
    with AdvisoryFileLock(path, timeout_s=0):
        pass


def test_process_death_releases_lock_without_stale_wait(tmp_path):
    path = tmp_path / "inv.lock"
    script = """
import sys
from pathlib import Path
from roundtable.filelock import AdvisoryFileLock
with AdvisoryFileLock(Path(sys.argv[1])):
    print('locked', flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=Path(__file__).resolve().parents[1],
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(LockTimeout):
            AdvisoryFileLock(path, timeout_s=0).acquire()
        process.terminate()
        process.wait(timeout=10)
        with AdvisoryFileLock(path, timeout_s=0):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_exception_releases_lock(tmp_path):
    path = tmp_path / "inv.lock"
    with pytest.raises(RuntimeError, match="interrupted"):
        with AdvisoryFileLock(path):
            raise RuntimeError("interrupted")
    with AdvisoryFileLock(path, timeout_s=0):
        pass
