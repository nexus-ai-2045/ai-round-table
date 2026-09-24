"""tools/run_repo_preflight.py は上流が実行できなかったことを隠さない。

実測 (2026-09-19, main): readiness_rc=2 (tool_error) でも wrapper は 0 を返し、
consistency の JSON は 37 行の pretty print なのに最終行 `}` だけを読んで
mode=None になっていた。上流が壊れていても gates が緑のまま = fail-open。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "tools" / "run_repo_preflight.py"


def _stub_cache(tmp_path: Path, scan_rc: int, consistency_rc: int, consistency_status: str, scan_stdout: str = "{\"status\": \"pass\"}") -> Path:
    """上流 clone の代わりに、指定 rc / status を返す stub script を置く。"""
    cache = tmp_path / "cache"
    (cache / "scripts").mkdir(parents=True)
    (cache / "scripts" / "readiness_scan.py").write_text(
        "import sys, json\n"
        "json.dump({'argv': sys.argv[1:]}, open(sys.argv[sys.argv.index('--repo')+1] + '/scan_argv.json', 'w'))\n"
        f"sys.stdout.write({scan_stdout!r}); sys.exit({scan_rc})\n",
        encoding="utf-8",
    )
    (cache / "scripts" / "consistency_gate.py").write_text(
        "import sys, json\n"
        f"print(json.dumps({{'status': {consistency_status!r}, 'mode': 'shadow'}}, indent=2))\n"
        f"sys.exit({consistency_rc})\n",
        encoding="utf-8",
    )
    return cache


def _run(tmp_path: Path, **kw) -> tuple[int, dict]:  # noqa: D103
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    cache = _stub_cache(tmp_path, **kw)
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), "--repo", str(repo), "--cache", str(cache), "--base-ref", "origin/main"],
        capture_output=True, text=True, check=False,
    )
    argv = json.loads((repo / "scan_argv.json").read_text(encoding="utf-8"))["argv"]
    return proc.returncode, {"argv": argv, "out": proc.stdout}


def test_readiness_tool_error_fails_the_wrapper(tmp_path: Path) -> None:
    rc, _ = _run(tmp_path, scan_rc=2, consistency_rc=0, consistency_status="pass")
    assert rc == 1


def test_consistency_tool_error_in_pretty_json_fails_the_wrapper(tmp_path: Path) -> None:
    # rc は 0 でも JSON が tool_error なら失敗。最終行だけ読むと拾えない。
    rc, info = _run(tmp_path, scan_rc=0, consistency_rc=0, consistency_status="tool_error")
    assert rc == 1, info["out"]


def test_findings_stay_shadow_and_do_not_fail_the_wrapper(tmp_path: Path) -> None:
    rc, info = _run(tmp_path, scan_rc=1, consistency_rc=1, consistency_status="fail")
    assert rc == 0
    assert "mode='shadow'" in info["out"]   # 最終行ではなく全文を parse できている


def test_plain_scan_receives_the_base_ref_for_consistency_scope(tmp_path: Path) -> None:
    _, info = _run(tmp_path, scan_rc=0, consistency_rc=0, consistency_status="pass")
    assert "--consistency-base-ref" in info["argv"] and "origin/main" in info["argv"]
    assert "--intent" not in info["argv"]      # repo 全体 scan を狭めない


def test_argparse_rejection_with_rc1_and_no_report_fails_the_wrapper(tmp_path: Path) -> None:
    """上流が argparse で拒否すると SystemExit("error: ...") で rc=1、stdout は空。
    rc だけ見ると所見 (shadow) と区別できない。report が無ければ実行失敗 (実測:
    未修正 repo-preflight に --consistency-base-ref を渡した場合)。"""
    rc, info = _run(tmp_path, scan_rc=1, consistency_rc=0, consistency_status="pass", scan_stdout="error: bad args\n")
    assert rc == 1, info["out"]


def test_scan_report_tool_error_fails_the_wrapper(tmp_path: Path) -> None:
    rc, _ = _run(tmp_path, scan_rc=1, consistency_rc=0, consistency_status="pass", scan_stdout='{"status": "tool_error"}')
    assert rc == 1
