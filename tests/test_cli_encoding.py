"""Windowsの非UTF-8標準出力でもプロセス入口が日本語を返す。"""
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("invocation", [
    ["-m", "roundtable.cli"],
    ["-c", "from roundtable.cli import entrypoint; raise SystemExit(entrypoint())"],
])
def test_cli_help_uses_utf8_with_legacy_stdio(invocation):
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    result = subprocess.run([sys.executable, *invocation, "--help"],
                            env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "指名" in result.stdout.decode("utf-8")
