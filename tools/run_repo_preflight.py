#!/usr/bin/env python3
"""Run upstream repo-preflight without copying its inspection logic.

Clones/updates nexus-ai-2045/repo-preflight into .tools/repo-preflight (gitignored)
and executes readiness_scan.py + consistency_gate.py against this repository.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


UPSTREAM = "https://github.com/nexus-ai-2045/repo-preflight.git"
DEFAULT_CACHE = Path(".tools") / "repo-preflight"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Invoke upstream repo-preflight against this repo.")
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument("--intent", default=None)
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    cache = (repo / args.cache).resolve() if not args.cache.is_absolute() else args.cache.resolve()
    ensure_checkout(cache)

    scan_cmd = [sys.executable, str(cache / "scripts" / "readiness_scan.py"), "--repo", str(repo)]
    if args.intent:
        scan_cmd.extend(["--intent", args.intent, "--base-ref", args.base_ref])
    else:
        # impact_map を持つ repo では plain scan の内蔵 consistency が
        # change_sensitive_scope_unavailable で tool_error になる。scope だけを
        # 渡し、repo 全体 scan (secret / personal path / docs) は狭めない。
        # 上流 repo-preflight 側の受け入れは fix/plain-scan-accepts-consistency-base-ref。
        scan_cmd.extend(["--consistency-base-ref", args.base_ref])
    consistency_cmd = [
        sys.executable,
        str(cache / "scripts" / "consistency_gate.py"),
        "--repo",
        str(repo),
        "--base-ref",
        args.base_ref,
        "--json",
    ]

    print("==> repo-preflight readiness_scan")
    scan = subprocess.run(scan_cmd, cwd=repo, text=True, capture_output=True)
    print(scan.stdout)
    if scan.stderr:
        print(scan.stderr, file=sys.stderr)

    print("==> repo-preflight consistency_gate")
    consistency = subprocess.run(consistency_cmd, cwd=repo, text=True, capture_output=True)
    print(consistency.stdout)
    if consistency.stderr:
        print(consistency.stderr, file=sys.stderr)

    # Shadow / readiness findings (rc=1) are human materials, not merge approval.
    # But "could not run" (rc=2 / tool_error) must not hide behind exit 0:
    # 実測 (2026-09-19) では readiness_rc=2 のまま wrapper が 0 を返し、
    # readiness_scan が永久に tool_error なのを gates が隠していた。
    payload = _parse_json_report(consistency.stdout)
    mode = payload.get("mode")
    scan_report = _parse_json_report(scan.stdout)
    if scan.returncode < 0 or consistency.returncode < 0:
        return 1
    # argparse の拒否や crash は rc=1 で JSON を出さない。rc だけ見ると
    # 「所見あり (shadow)」と区別できず 0 を返してしまう。report が無い時点で
    # 「実行できなかった」と扱う。
    if scan.returncode == 2 or not scan_report or scan_report.get("status") == "tool_error":
        print(
            f"==> repo-preflight readiness_scan could not run "
            f"(rc={scan.returncode}, report={'yes' if scan_report else 'none'})",
            file=sys.stderr,
        )
        return 1
    if consistency.returncode == 2 or payload.get("status") == "tool_error":
        print("==> repo-preflight consistency_gate could not run (tool_error)", file=sys.stderr)
        return 1
    print(
        f"==> repo-preflight wrapper done "
        f"(readiness_rc={scan.returncode}, consistency_rc={consistency.returncode}, "
        f"mode={mode!r}; not a merge approval)"
    )
    return 0


def _parse_json_report(stdout: str) -> dict:
    """consistency_gate --json は複数行の pretty JSON を出す。最終行だけ読むと
    常に `}` で JSONDecodeError になり mode=None になっていた。全文を優先し、
    1 行 JSON を出す実装向けに最終行 fallback を残す。"""
    text = stdout.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(text.splitlines()[-1])
    except json.JSONDecodeError:
        return {}


def ensure_checkout(cache: Path) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    if (cache / "scripts" / "readiness_scan.py").is_file():
        subprocess.run(["git", "-C", str(cache), "fetch", "--depth", "1", "origin"], check=False)
        subprocess.run(["git", "-C", str(cache), "checkout", "FETCH_HEAD"], check=False)
        return
    subprocess.run(["git", "clone", "--depth", "1", UPSTREAM, str(cache)], check=True)


if __name__ == "__main__":
    raise SystemExit(main())
