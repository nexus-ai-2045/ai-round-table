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
    """repo 相対の posix path だけを通す。

    `dir\\file.py` を受理して内部で `/` に直すと、schema は通るのに fan-in の
    所有照合 (git は `dir/file.py` を返す) で必ず外れる。どちらの綴りが正かを
    ここで決め切り、ずれは schema 段階で見える形にする。
    """
    if not _nonempty(value):
        return False
    text = str(value)
    if "\\" in text:
        return False
    path = PurePosixPath(text)
    return not path.is_absolute() and ".." not in path.parts and text == path.as_posix()


def _qualify(reviewer: str, finding_id: str) -> str:
    """finding を reviewer 名で名前空間化する。

    独立レビューでは席同士が採番を相談しない。grok の R1 と claude の R1 は
    別物なので、素の id で集合に入れると 2 件が 1 件に潰れ、cluster が片方だけを
    参照していても「全 finding 割当済み」に見える (= 片方の指摘が黙って消える)。
    """
    return f"{reviewer}:{finding_id}"


def _default_branch(repo: Path) -> str | None:
    """remote の実体から default branch を取る。判定不能は fail-closed 用に None。"""
    try:
        remote = subprocess.run(
            ["git", "-C", str(repo), "ls-remote", "--symref", "origin", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if remote.returncode != 0:
        return None
    for line in remote.stdout.splitlines():
        match = re.fullmatch(r"ref: refs/heads/(.+)\tHEAD", line)
        if match:
            return match.group(1)
    return None


def _load_review_artifact(
    item: dict, index: int, errors: list[str], *, expected_base: str
) -> set[str]:
    prefix = f"review_artifacts[{index}]"
    reviewer = item.get("reviewer")
    if not _nonempty(reviewer):
        errors.append(f"{prefix}.reviewer: 非空文字列が必要")
        reviewer = f"<artifact{index}>"
    elif ":" in reviewer:
        errors.append(f"{prefix}.reviewer: ':' は finding ID の区切りと衝突するため禁止")
        reviewer = f"<artifact{index}>"
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
    except (OSError, ValueError) as exc:
        # ValueError で JSONDecodeError と UnicodeDecodeError の両方を受ける
        # (非 UTF-8 の成果物を traceback ではなく集約エラーにするため)。
        errors.append(f"{prefix}.path: JSON を読めない: {exc}")
        return set()
    if not isinstance(artifact, dict):
        errors.append(f"{prefix}.path: review 成果物のtop-levelはobjectが必要")
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
            continue
        if ":" in finding_id:
            errors.append(
                f"{prefix}.findings[{finding_index}].id: ':' はreviewer区切りと衝突するため禁止"
            )
            continue
        qualified = _qualify(str(reviewer), str(finding_id))
        if qualified in ids:
            errors.append(f"{prefix}.findings[{finding_index}].id: 重複: {finding_id}")
        else:
            ids.add(qualified)
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
    owned = implementation.get("owned_files")
    if not isinstance(owned, list) or not owned:
        errors.append("implementation.owned_files: 1件以上必要")
        owned = []
    elif any(not _valid_owned_path(path) for path in owned) or len(owned) != len(set(owned)):
        # 妥当性を先に見るのは、list など unhashable が混ざると set() が TypeError で
        # 落ち、集約エラーではなく traceback になるため (or は短絡する)。
        errors.append("implementation.owned_files: 重複なしのrepo相対正規pathが必要")

    boundary = data.get("permission_boundary")
    if not isinstance(boundary, dict):
        errors.append("permission_boundary: object が必要")
        boundary = {}
    prohibited = boundary.get("prohibited")
    if not isinstance(prohibited, list):
        errors.append("permission_boundary.prohibited: list が必要")
    else:
        # str だけを拾うのは unhashable 混入で set() を落とさないため。
        missing = sorted(REQUIRED_PROHIBITED - {x for x in prohibited if isinstance(x, str)})
        if missing:
            errors.append(f"permission_boundary.prohibited: 必須禁止操作が不足: {', '.join(missing)}")
    allowed = boundary.get("allowed")
    if not isinstance(allowed, list) or any(not isinstance(x, str) for x in allowed):
        errors.append("permission_boundary.allowed: 文字列のlistが必要")
    else:
        contradictory = sorted(REQUIRED_PROHIBITED & set(allowed))
        if contradictory:
            errors.append(
                "permission_boundary.allowed: 人間専用操作を許可できない: "
                + ", ".join(contradictory)
            )

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
    cluster_ids: set[str] = set()
    cluster_owners: set[str] = set()
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
            cluster_id = cluster.get("id")
            if _nonempty(cluster_id):
                if cluster_id in cluster_ids:
                    errors.append(f"{prefix}.id: 重複: {cluster_id}")
                cluster_ids.add(cluster_id)
            if _nonempty(cluster.get("owner")):
                cluster_owners.add(cluster["owner"])
            ids = cluster.get("finding_ids")
            if not isinstance(ids, list) or not ids:
                errors.append(f"{prefix}.finding_ids: 1件以上必要")
                continue
            for finding_id in ids:
                if not _nonempty(finding_id):
                    errors.append(f"{prefix}.finding_ids: 非空文字列が必要")
                    continue
                if finding_id not in finding_ids:
                    errors.append(
                        f"{prefix}.finding_ids: review にない finding: {finding_id}"
                        " (形式は '<reviewer>:<id>')"
                    )
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
            not isinstance(test, dict)
            or not _nonempty(test.get("command"))
            or test.get("status") != "passed"
            or not isinstance(test.get("cluster_ids"), list)
            or not test["cluster_ids"]
            or any(not _nonempty(item) for item in test["cluster_ids"])
            for test in tests
        ):
            errors.append(
                "fan_in.tests: command・status=passed・cluster_idsを持つ1件以上が必要"
            )
        else:
            covered = {item for test in tests for item in test["cluster_ids"]}
            unknown = sorted(covered - cluster_ids)
            missing_receipts = sorted(cluster_ids - covered)
            if unknown:
                errors.append(f"fan_in.tests: 未知のcluster_ids: {', '.join(unknown)}")
            if missing_receipts:
                errors.append(
                    "fan_in.tests: test receipt のないcluster: " + ", ".join(missing_receipts)
                )
        if fan_in.get("integration_reverified") is not True:
            errors.append("fan_in.integration_reverified: true が必要")
        lanes = fan_in.get("terminal_lanes")
        if not isinstance(lanes, list) or not lanes:
            errors.append("fan_in.terminal_lanes: 1件以上必要")
        else:
            required_lanes = cluster_owners | {
                owner for owner in [implementation.get("owner")] if _nonempty(owner)
            }
            missing_lanes = sorted(owner for owner in required_lanes if owner not in lanes)
            if missing_lanes:
                errors.append(
                    "fan_in.terminal_lanes: owner のterminal化が必要: "
                    + ", ".join(missing_lanes)
                )
    return errors


def _is_linked_worktree(repo: Path) -> bool | None:
    """repo が primary checkout ではなく linked worktree かを判定する。

    linked worktree では --git-dir が `<main>/.git/worktrees/<name>` を指し、
    --git-common-dir が `<main>/.git` を指すので 2 つが食い違う。primary では一致する。
    判定できなければ None (呼び出し側が fail-closed に倒す)。
    """
    git_dir = _git(repo, "rev-parse", "--absolute-git-dir")
    common = _git(repo, "rev-parse", "--git-common-dir")
    if git_dir.returncode != 0 or common.returncode != 0:
        return None
    common_path = Path(common.stdout.strip())
    if not common_path.is_absolute():
        common_path = repo / common_path
    try:
        return Path(git_dir.stdout.strip()).resolve() != common_path.resolve()
    except OSError:
        return None


def _touched_files(repo: Path, base: str, commit: str) -> tuple[list[str] | None, str]:
    """base..commit の各commitが触った全pathを取る。

    --no-renames が要るのは、rename 検出が既定で有効なため。所有外の
    `outside.txt` を owned な名前へ改名すると --name-only は新しい名前しか返さず、
    所有権検査が素通りする (fail-open)。
    -z は core.quotepath による非 ASCII path のクォート化を避けるため。
    """
    result = _git(
        repo, "log", "-m", "--format=", "--no-renames", "-z", "--name-only",
        f"{base}..{commit}",
    )
    if result.returncode != 0:
        return None, (result.stderr or "").strip()
    return [item for item in result.stdout.split("\0") if item], ""


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

    default_branch = _default_branch(repo)
    if default_branch is None:
        errors.append("implementation.branch: repository の default branch を判定できない")
    elif expected_branch == default_branch:
        errors.append(f"implementation.branch: default branch ({default_branch}) は禁止")

    # 「専用 worktree」を path 一致だけで確かめると、primary checkout をそのまま
    # 指定しても通る (= 分離の保証が文章だけになる)。実際に linked worktree かを見る。
    linked = _is_linked_worktree(repo)
    if linked is None:
        errors.append(f"implementation.worktree: worktree 種別を判定できない: {repo}")
    elif not linked:
        errors.append(
            "implementation.worktree: primary checkout は使えない"
            " (git worktree add で作った専用 worktree が必要)"
        )

    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0 or status.stdout.strip():
        # fan-in でも見るのは、所有外の未 commit / 未追跡を残したまま
        # 「所有ファイルしか触っていない」と通ってしまうため。
        errors.append(f"{phase} gate: worktree が clean ではない")

    if phase == "start":
        if head.stdout.strip() != data["base_commit"]:
            errors.append(f"base_commit: live HEAD {head.stdout.strip()} と不一致")
    else:
        commit_sha = data["fan_in"]["commit_sha"]
        if head.stdout.strip() != commit_sha:
            errors.append(f"fan_in.commit_sha: live HEAD {head.stdout.strip()} と不一致")
        if commit_sha == data["base_commit"]:
            # is-ancestor は自分自身を祖先と判定するので、実装 commit ゼロの
            # 空 receipt がそのまま通る。
            errors.append("fan_in.commit_sha: base_commit と同一 (実装 commit が無い)")
        ancestor = _git(repo, "merge-base", "--is-ancestor", data["base_commit"], commit_sha)
        if ancestor.returncode != 0:
            errors.append("fan_in.commit_sha: base_commit の子孫ではない")
        final_diff = _git(repo, "diff", "--quiet", data["base_commit"], commit_sha)
        if final_diff.returncode == 0:
            errors.append("fan_in: 最終 tree が base_commit と同一 (実装差分が無い)")
        elif final_diff.returncode != 1:
            errors.append("fan_in: 最終 tree の差分を判定できない")
        touched, detail = _touched_files(repo, data["base_commit"], commit_sha)
        if touched is None:
            errors.append(f"fan_in: changed files を取得できない: {detail}")
        else:
            if not touched:
                errors.append("fan_in: base_commit 以降に変更ファイルが無い")
            owned = set(data["implementation"]["owned_files"])
            outside = sorted(set(touched) - owned)
            if outside:
                errors.append(f"fan_in: owned_files 外の変更: {', '.join(outside)}")
    return errors


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
