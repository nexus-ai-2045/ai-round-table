"""試験用ledgerが提出branchへ書き込む前に保存先を検査する。"""
import os
from pathlib import Path
import subprocess

import pytest


CHECKOUT = Path(__file__).resolve().parents[1]


def _ignore_probe(target: Path, checkout: Path) -> str:
    """末尾slashではなく、生成予定の子ファイルが無視されることを検査する。"""
    return (target.relative_to(checkout) / "__roundtable_basetemp_probe__").as_posix()


def basetemp_diagnostic(value: str, checkout: Path = CHECKOUT) -> str:
    """失敗したCIで包含判定とignore規則を特定するための診断。"""
    raw = Path(os.path.abspath(value))
    resolved = raw.resolve()
    root = checkout.resolve()
    probe_path = _ignore_probe(resolved, root)
    probe = subprocess.run(
        ["git", "check-ignore", "-v", "--", probe_path],
        cwd=root, capture_output=True, timeout=10,
    )
    return (f"raw={raw!s}; resolved={resolved!s}; checkout={root!s}; "
            f"containment_raw={raw.is_relative_to(root)}; "
            f"containment_resolved={resolved.is_relative_to(root)}; "
            f"probe={probe_path}; git check-ignore -v rc={probe.returncode}; "
            f"stdout={probe.stdout!r}; stderr={probe.stderr!r}")


def validate_basetemp(value: str | None, checkout: Path = CHECKOUT) -> None:
    """checkout内の明示basetempはgitignore対象だけを許可する。"""
    if value is None:
        return
    raw = Path(os.path.abspath(value))
    target = raw.resolve()
    checkout = checkout.resolve()
    if not target.is_relative_to(checkout):
        return
    if target == checkout:
        raise pytest.UsageError("basetempにcheckout自体は指定できません")
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--", _ignore_probe(target, checkout)],
            cwd=checkout, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise pytest.UsageError("basetempのgitignore検査に失敗しました") from exc
    if result.returncode != 0:
        raise pytest.UsageError(
            "checkout内のbasetempはgitignore対象が必要です。"
            " .pytest-tmp*/ またはrepo外の一時ディレクトリを指定してください。 "
            + basetemp_diagnostic(value, checkout)
        )


def pytest_configure(config: pytest.Config) -> None:
    validate_basetemp(config.getoption("basetemp"))
