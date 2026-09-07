"""危険なbasetempはfixture生成前に拒否され、HEADを変更しない。"""
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import pytest

from conftest import CHECKOUT, validate_basetemp, basetemp_diagnostic


def test_unignored_checkout_path_rejected():
    target = str(CHECKOUT / "unignored-test-artifacts")
    try:
        validate_basetemp(target)
    except pytest.UsageError as exc:
        assert "gitignore" in str(exc)
    else:
        pytest.fail(basetemp_diagnostic(target))


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
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, (
        basetemp_diagnostic(str(target)), result.stdout, result.stderr
    )
    assert "gitignore" in result.stderr.decode("utf-8", errors="replace")
    assert not target.exists()
    assert head() == before



@pytest.mark.parametrize("safe", [True, False])
def test_crlf_gitignore_checks_child_file(tmp_path, safe):
    repo = tmp_path / "crlf-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / ".gitignore").write_bytes(b".pytest-tmp*/\r\n__pycache__/\r\n.pytest_cache/\r\n.locks/\r\n\r\n")
    target = repo / (".pytest-tmp-safe" if safe else "unignored-test-artifacts")
    if safe:
        validate_basetemp(str(target), checkout=repo)
    else:
        with pytest.raises(pytest.UsageError, match="gitignore") as caught:
            validate_basetemp(str(target), checkout=repo)
        assert "__roundtable_basetemp_probe__" in str(caught.value)
    assert not target.exists()
