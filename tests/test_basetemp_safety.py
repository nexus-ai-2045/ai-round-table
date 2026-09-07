"""危険なbasetempはfixture生成前に拒否され、HEADを変更しない。"""
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import pytest

import conftest
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


def test_missing_suffix_resolves_existing_parent_only(tmp_path, monkeypatch):
    target = tmp_path / "missing" / "nested"
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        if path == target or path == target.parent:
            raise AssertionError("missing path must not be resolved")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    assert conftest._existing_parent_resolve(target) == tmp_path.resolve() / "missing" / "nested"


def test_alias_disagreement_fails_closed(monkeypatch):
    external = Path(tempfile.gettempdir()).resolve() / "roundtable-alias-fixture"
    target = CHECKOUT / "unignored-alias"
    monkeypatch.setattr(conftest, "_existing_parent_resolve", lambda path: external / path.name)
    with pytest.raises(pytest.UsageError, match="包含判定不一致") as caught:
        validate_basetemp(str(target))
    assert "containment_raw=True" in str(caught.value)
    assert "containment_resolved=False" in str(caught.value)
    assert "git check-ignore -v" in str(caught.value)
