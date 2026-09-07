"""試験用ledgerが提出branchへ書き込む前に保存先を検査する。"""
from pathlib import Path
import subprocess

import pytest


CHECKOUT = Path(__file__).resolve().parents[1]


def validate_basetemp(value: str | None, checkout: Path = CHECKOUT) -> None:
    """checkout内の明示basetempはgitignore対象だけを許可する。"""
    if value is None:
        return
    target = Path(value).resolve()
    checkout = checkout.resolve()
    if not target.is_relative_to(checkout):
        return
    relative = target.relative_to(checkout)
    if target == checkout:
        raise pytest.UsageError("basetempにcheckout自体は指定できません")
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--", relative.as_posix() + "/"],
            cwd=checkout, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise pytest.UsageError("basetempのgitignore検査に失敗しました") from exc
    if result.returncode != 0:
        raise pytest.UsageError(
            "checkout内のbasetempはgitignore対象が必要です。"
            " .pytest-tmp*/ またはrepo外の一時ディレクトリを指定してください"
        )


def pytest_configure(config: pytest.Config) -> None:
    validate_basetemp(config.getoption("basetemp"))
