"""危険なbasetempはfixture生成前に拒否され、HEADを変更しない。"""
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import pytest

from conftest import CHECKOUT, validate_basetemp


def test_unignored_checkout_path_rejected():
    with pytest.raises(pytest.UsageError, match="gitignore"):
        validate_basetemp(str(CHECKOUT / "unignored-test-artifacts"))


def test_ignored_checkout_path_allowed():
    validate_basetemp(str(CHECKOUT / ".pytest-tmp-safety"))


def test_external_path_allowed():
    external = Path(tempfile.gettempdir()).resolve() / "roundtable-external-safety"
    assert not external.is_relative_to(CHECKOUT)
    validate_basetemp(str(external))


def test_unspecified_basetemp_allowed():
    validate_basetemp(None)


def test_actual_pytest_stops_before_creating_basetemp():
    target = CHECKOUT / ("unsafe-basetemp-" + uuid.uuid4().hex)
    def head():
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=CHECKOUT)
    before = head()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_basetemp_safety.py",
         "--collect-only", "--basetemp", str(target)],
        cwd=CHECKOUT, capture_output=True, timeout=30,
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "gitignore" in result.stderr.decode("utf-8", errors="replace")
    assert not target.exists()
    assert head() == before
