"""試験用ledgerが提出branchへ書き込む前に保存先を検査する。"""
import os
from pathlib import Path
import subprocess

import pytest


CHECKOUT = Path(__file__).resolve().parents[1]


def _existing_parent_resolve(path: Path) -> Path:
    """未作成suffixを保ち、Windowsの既存親だけで実パスを解決する。"""
    suffix: list[str] = []
    parent = path
    while not parent.exists():
        if parent == parent.parent:
            raise OSError("existing ancestor unavailable")
        suffix.append(parent.name)
        parent = parent.parent
    return parent.resolve(strict=True).joinpath(*reversed(suffix))


def basetemp_diagnostic(value: str, checkout: Path = CHECKOUT) -> str:
    """失敗したCIで包含判定とignore規則を特定するための診断。"""
    raw = Path(os.path.abspath(value))
    resolved = _existing_parent_resolve(raw)
    root = checkout.resolve()
    probe = subprocess.run(
        ["git", "check-ignore", "-v", "--", str(raw) + "/"],
        cwd=root, capture_output=True, timeout=10,
    )
    return (f"raw={raw!s}; resolved={resolved!s}; checkout={root!s}; "
            f"containment_raw={raw.is_relative_to(root)}; "
            f"containment_resolved={resolved.is_relative_to(root)}; "
            f"git check-ignore -v rc={probe.returncode}; "
            f"stdout={probe.stdout!r}; stderr={probe.stderr!r}")


def validate_basetemp(value: str | None, checkout: Path = CHECKOUT) -> None:
    """checkout内の明示basetempはgitignore対象だけを許可する。"""
    if value is None:
        return
    raw = Path(os.path.abspath(value))
    lexical_checkout = Path(os.path.abspath(checkout))
    try:
        target = _existing_parent_resolve(raw)
        checkout = checkout.resolve(strict=True)
    except OSError as exc:
        raise pytest.UsageError(f"basetemp解決失敗: raw={raw}; checkout={checkout}") from exc
    lexical_inside = (raw.is_relative_to(lexical_checkout)
                      or raw.is_relative_to(checkout))
    resolved_inside = target.is_relative_to(checkout)
    if lexical_inside and not resolved_inside:
        raise pytest.UsageError("basetempの包含判定不一致: " + basetemp_diagnostic(value, checkout))
    if not resolved_inside:
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
            " .pytest-tmp*/ またはrepo外の一時ディレクトリを指定してください。 "
            + basetemp_diagnostic(value, checkout)
        )


def pytest_configure(config: pytest.Config) -> None:
    validate_basetemp(config.getoption("basetemp"))
