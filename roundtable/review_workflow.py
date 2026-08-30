"""独立レビューから実装 fan-in までの fail-closed gate。

議事録の opinion schema や Journal 状態機械は変更しない。レビュー後の実装責任は
別 workflow の構造化 manifest として検査し、Git を唯一の証跡にする。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path, PurePosixPath

SCHEMA = "roundtable.review-to-implementation/v1"
REVIEW_SCHEMA = "roundtable.review/v1"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_BRANCHES = {"main", "master"}
REQUIRED_PROHIBITED = {
    "merge", "release", "settings", "visibility", "auth", "secret", "delete", "force"
}


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _valid_owned_path(value: object) -> bool:
    if not _nonempty(value):
        return False
    text = str(value).replace("\\", "/")
    path = PurePosixPath(text)
    return not path.is_absolute() and ".." not in path.parts and text == path.as_posix()


def _load_review_artifact(
    item: dict, index: int, errors: list[str], *, expected_base: str
) -> set[str]:
    prefix = f"review_artifacts[{index}]"
    if not _nonempty(item.get("reviewer")):
        errors.append(f"{prefix}.reviewer: 非空文字列が必要")
    path_value = item.get("path")
    if not _nonempty(path_value):
        errors.append(f"{prefix}.path: 非空文字列が必要")
        return set()
    path = Path(path_value)
    if not path.is_file():
        errors.append(f"{prefix}.path: review 成果物が存在しない: {path}")
        return set()
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{prefix}.path: JSON を読めない: {exc}")
        return set()
    expected = item.get("schema")
    if expected != REVIEW_SCHEMA or artifact.get("schema") != expected:
        errors.append(f"{prefix}.schema: {REVIEW_SCHEMA} と成果物 schema の一致が必要")
    if artifact.get("base_commit") != expected_base:
        errors.append(f"{prefix}.base_commit: workflowのexact baseとの一致が必要")
    if artifact.get("complete") is not True:
        errors.append(f"{prefix}.complete: true が必要")
    findings = artifact.get("findings")
    if not isinstance(findings, list):
        errors.append(f"{prefix}.findings: list が必要")
        return set()
    ids: set[str] = set()
    for finding_index, finding in enumerate(findings):
        finding_id = finding.get("id") if isinstance(finding, dict) else None
        if not _nonempty(finding_id):
            errors.append(f"{prefix}.findings[{finding_index}].id: 非空文字列が必要")
        elif finding_id in ids:
            errors.append(f"{prefix}.findings[{finding_index}].id: 重複: {finding_id}")
        else:
            ids.add(finding_id)
    return ids


def validate_review_workflow(data, *, phase: str) -> list[str]:
    """manifest を検査し、違反をすべて返す。phase は start / fan-in。"""
    errors: list[str] = []
    if phase not in {"start", "fan-in"}:
        return ["phase: start または fan-in が必要"]
    if not isinstance(data, dict):
        return ["top-level must be object"]
    if data.get("schema") != SCHEMA:
        errors.append(f"schema: {SCHEMA} が必要")
    if not _nonempty(data.get("workflow_id")):
        errors.append("workflow_id: 非空文字列が必要")
    if not isinstance(data.get("base_commit"), str) or not SHA40.fullmatch(data["base_commit"]):
        errors.append("base_commit: exact 40桁 lowercase SHA が必要")

    implementation = data.get("implementation")
    if not isinstance(implementation, dict):
        errors.append("implementation: object が必要")
        implementation = {}
    for key in ("worktree", "branch", "owner"):
        if not _nonempty(implementation.get(key)):
            errors.append(f"implementation.{key}: 非空文字列が必要")
    if implementation.get("branch") in DEFAULT_BRANCHES:
        errors.append("implementation.branch: default branch は禁止")
    owned = implementation.get("owned_files")
    if not isinstance(owned, list) or not owned:
        errors.append("implementation.owned_files: 1件以上必要")
        owned = []
    elif len(owned) != len(set(owned)) or any(not _valid_owned_path(path) for path in owned):
        errors.append("implementation.owned_files: 重複なしのrepo相対正規pathが必要")

    boundary = data.get("permission_boundary")
    if not isinstance(boundary, dict):
        errors.append("permission_boundary: object が必要")
        boundary = {}
    prohibited = boundary.get("prohibited")
    if not isinstance(prohibited, list):
        errors.append("permission_boundary.prohibited: list が必要")
    else:
        missing = sorted(REQUIRED_PROHIBITED - set(prohibited))
        if missing:
            errors.append(f"permission_boundary.prohibited: 必須禁止操作が不足: {', '.join(missing)}")

    artifacts = data.get("review_artifacts")
    finding_ids: set[str] = set()
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("review_artifacts: 1件以上必要")
    else:
        for index, item in enumerate(artifacts):
            if not isinstance(item, dict):
                errors.append(f"review_artifacts[{index}]: object が必要")
                continue
            finding_ids.update(
                _load_review_artifact(item, index, errors, expected_base=data.get("base_commit"))
            )

    clusters = data.get("root_cause_clusters")
    mapped: set[str] = set()
    if not isinstance(clusters, list) or not clusters:
        errors.append("root_cause_clusters: 1件以上必要")
    else:
        for index, cluster in enumerate(clusters):
            prefix = f"root_cause_clusters[{index}]"
            if not isinstance(cluster, dict):
                errors.append(f"{prefix}: object が必要")
                continue
            for key in ("id", "owner", "test"):
                if not _nonempty(cluster.get(key)):
                    errors.append(f"{prefix}.{key}: 非空文字列が必要")
            ids = cluster.get("finding_ids")
            if not isinstance(ids, list) or not ids:
                errors.append(f"{prefix}.finding_ids: 1件以上必要")
                continue
            for finding_id in ids:
                if finding_id not in finding_ids:
                    errors.append(f"{prefix}.finding_ids: review にない finding: {finding_id}")
                mapped.add(finding_id)
    unmapped = sorted(finding_ids - mapped)
    if unmapped:
        errors.append(f"root_cause_clusters: 未割当 finding: {', '.join(unmapped)}")

    if phase == "fan-in":
        fan_in = data.get("fan_in")
        if not isinstance(fan_in, dict):
            errors.append("fan_in: object が必要")
            fan_in = {}
        if not _nonempty(fan_in.get("owner")):
            errors.append("fan_in.owner: production integration owner が必要")
        if fan_in.get("single_pr") is not True:
            errors.append("fan_in.single_pr: true が必要")
        if not isinstance(fan_in.get("commit_sha"), str) or not SHA40.fullmatch(fan_in["commit_sha"]):
            errors.append("fan_in.commit_sha: exact 40桁 lowercase SHA が必要")
        tests = fan_in.get("tests")
        if not isinstance(tests, list) or not tests or any(
            not isinstance(test, dict) or not _nonempty(test.get("command")) or test.get("status") != "passed"
            for test in tests
        ):
            errors.append("fan_in.tests: command と status=passed を持つ1件以上が必要")
        if fan_in.get("integration_reverified") is not True:
            errors.append("fan_in.integration_reverified: true が必要")
        lanes = fan_in.get("terminal_lanes")
        if not isinstance(lanes, list) or not lanes:
            errors.append("fan_in.terminal_lanes: 1件以上必要")
        elif implementation.get("owner") not in lanes:
            errors.append("fan_in.terminal_lanes: implementation owner のterminal化が必要")
    return errors


def validate_live_git(data: dict, *, phase: str, repo: Path) -> list[str]:
    """schema後にlive Gitを照合する。外部操作は行わない。"""
    errors: list[str] = []
    head = _git(repo, "rev-parse", "HEAD")
    branch = _git(repo, "branch", "--show-current")
    if head.returncode != 0 or branch.returncode != 0:
        return [f"repo: Git worktree を読めない: {repo}"]
    expected_branch = data["implementation"]["branch"]
    if branch.stdout.strip() != expected_branch:
        errors.append(f"implementation.branch: live branch {branch.stdout.strip()} と不一致")
    expected_worktree = Path(data["implementation"]["worktree"]).resolve()
    if repo.resolve() != expected_worktree:
        errors.append(f"implementation.worktree: live repo {repo.resolve()} と不一致")
    if phase == "start":
        if head.stdout.strip() != data["base_commit"]:
            errors.append(f"base_commit: live HEAD {head.stdout.strip()} と不一致")
        status = _git(repo, "status", "--porcelain")
        if status.returncode != 0 or status.stdout.strip():
            errors.append("start gate: worktree が clean ではない")
    else:
        commit_sha = data["fan_in"]["commit_sha"]
        if head.stdout.strip() != commit_sha:
            errors.append(f"fan_in.commit_sha: live HEAD {head.stdout.strip()} と不一致")
        ancestor = _git(repo, "merge-base", "--is-ancestor", data["base_commit"], commit_sha)
        if ancestor.returncode != 0:
            errors.append("fan_in.commit_sha: base_commit の子孫ではない")
        changed = _git(repo, "diff", "--name-only", f"{data['base_commit']}..{commit_sha}")
        if changed.returncode != 0:
            errors.append("fan_in: changed files を取得できない")
        else:
            owned = set(data["implementation"]["owned_files"])
            outside = sorted(set(changed.stdout.splitlines()) - owned)
            if outside:
                errors.append(f"fan_in: owned_files 外の変更: {', '.join(outside)}")
    return errors


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
