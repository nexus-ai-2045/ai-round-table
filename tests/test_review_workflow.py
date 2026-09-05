import json
import subprocess
from pathlib import Path

from roundtable.cli import main
from roundtable.review_workflow import validate_live_git, validate_review_workflow


BASE = "a" * 40
COMMIT = "b" * 40


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        encoding="utf-8",
    )


def _seeded_repo(tmp_path, name="main"):
    """seed commit を 1 つ持つ primary checkout を作る。"""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-qb", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-qm", "seed")
    remote = tmp_path / f"{name}-remote.git"
    subprocess.run(["git", "init", "--bare", "-qb", "main", str(remote)], check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _linked_worktree(tmp_path, branch="feat/review-1"):
    """primary checkout と、そこから生やした専用 worktree を返す。

    gate は「専用 worktree であること」を要求するので、test も本物の
    linked worktree を作る (plain な git init では実運用形を踏めない)。
    """
    repo = _seeded_repo(tmp_path)
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", branch, str(worktree))
    return repo, worktree


def _head(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _manifest(tmp_path):
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps({
            "schema": "roundtable.review/v1", "base_commit": BASE, "complete": True,
            "findings": [{"id": "R1"}],
        }),
        encoding="utf-8",
    )
    return {
        "schema": "roundtable.review-to-implementation/v1",
        "workflow_id": "ghost-review-1",
        "base_commit": BASE,
        "implementation": {
            "worktree": str(tmp_path),
            "branch": "feat/review-1",
            "owner": "implementation-lane",
            "owned_files": ["roundtable/review_workflow.py", "tests/test_review_workflow.py"],
        },
        "review_artifacts": [
            {"reviewer": "grok", "path": str(review), "schema": "roundtable.review/v1"}
        ],
        "permission_boundary": {
            "allowed": ["edit-owned-files", "test", "commit"],
            "prohibited": ["merge", "release", "settings", "visibility", "auth", "secret", "delete", "force"],
        },
        "root_cause_clusters": [
            {"id": "RC1", "finding_ids": ["grok:R1"], "owner": "implementation-lane", "test": "tests/test_review_workflow.py"}
        ],
        "fan_in": {
            "owner": "codex-mainline",
            "single_pr": True,
            "commit_sha": COMMIT,
            "tests": [{"command": "pytest", "status": "passed", "cluster_ids": ["RC1"]}],
            "integration_reverified": True,
            "terminal_lanes": ["implementation-lane"],
        },
    }


def test_start_gate_rejects_missing_review_artifact(tmp_path):
    data = _manifest(tmp_path)
    data["review_artifacts"][0]["path"] = str(tmp_path / "missing.json")
    errors = validate_review_workflow(data, phase="start")
    assert any("review_artifacts[0].path" in error for error in errors)


def test_start_gate_rejects_review_for_different_base_or_incomplete(tmp_path):
    data = _manifest(tmp_path)
    path = data["review_artifacts"][0]["path"]
    Path(path).write_text(json.dumps({
        "schema": "roundtable.review/v1", "base_commit": "c" * 40,
        "complete": False, "findings": [{"id": "R1"}],
    }), encoding="utf-8")
    errors = validate_review_workflow(data, phase="start")
    assert any("base_commit" in error for error in errors)
    assert any("complete" in error for error in errors)


def test_start_gate_rejects_invalid_base_and_ownership_violations(tmp_path):
    data = _manifest(tmp_path)
    data["base_commit"] = "main"
    data["implementation"]["branch"] = "main"
    data["implementation"]["owned_files"] = ["../outside.py", "roundtable/review_workflow.py", "roundtable/review_workflow.py"]
    errors = validate_review_workflow(data, phase="start")
    assert any("base_commit" in error for error in errors)
    assert any("owned_files" in error for error in errors)


def test_start_gate_rejects_unmapped_findings_and_weak_boundary(tmp_path):
    data = _manifest(tmp_path)
    data["root_cause_clusters"][0]["finding_ids"] = ["UNKNOWN"]
    data["permission_boundary"]["prohibited"].remove("merge")
    errors = validate_review_workflow(data, phase="start")
    assert any("UNKNOWN" in error for error in errors)
    assert any("merge" in error for error in errors)


def test_fan_in_gate_requires_receipt_tests_and_single_pr(tmp_path):
    data = _manifest(tmp_path)
    data["fan_in"].update({"commit_sha": "", "tests": [], "single_pr": False, "integration_reverified": False, "terminal_lanes": []})
    errors = validate_review_workflow(data, phase="fan-in")
    assert any("commit_sha" in error for error in errors)
    assert any("tests" in error for error in errors)
    assert any("single_pr" in error for error in errors)
    assert any("integration_reverified" in error for error in errors)
    assert any("terminal_lanes" in error for error in errors)


def test_same_finding_id_from_two_reviewers_stays_separate(tmp_path):
    """独立レビュー 2 席が同じ採番 (R1) を使っても 1 件に潰れない。

    素の id で集合に入れると 2 件が 1 件になり、cluster が片方だけを参照していても
    「全 finding 割当済み」に見えて、もう片方の指摘が黙って消える。
    """
    data = _manifest(tmp_path)
    second = tmp_path / "review2.json"
    second.write_text(json.dumps({
        "schema": "roundtable.review/v1", "base_commit": BASE, "complete": True,
        "findings": [{"id": "R1"}],
    }), encoding="utf-8")
    data["review_artifacts"].append(
        {"reviewer": "claude", "path": str(second), "schema": "roundtable.review/v1"}
    )

    # grok の R1 しか割り当てていないので claude の R1 が未割当として残る
    errors = validate_review_workflow(data, phase="start")
    assert any("未割当" in error and "claude:R1" in error for error in errors)

    # 両方を明示すれば通る
    data["root_cause_clusters"][0]["finding_ids"] = ["grok:R1", "claude:R1"]
    assert validate_review_workflow(data, phase="start") == []


def test_owned_files_rejects_backslash_and_unhashable(tmp_path):
    """schema 段階で弾く: 綴り違いは fan-in の照合で必ず外れ、unhashable は set() を落とす。"""
    data = _manifest(tmp_path)
    data["implementation"]["owned_files"] = ["roundtable\\review_workflow.py"]
    assert any("owned_files" in error for error in validate_review_workflow(data, phase="start"))

    data["implementation"]["owned_files"] = [["roundtable/review_workflow.py"]]
    errors = validate_review_workflow(data, phase="start")  # TypeError で落ちないこと
    assert any("owned_files" in error for error in errors)


def test_cli_start_gate_checks_live_exact_base(tmp_path, capsys):
    _, worktree = _linked_worktree(tmp_path)
    data = _manifest(tmp_path)
    data["base_commit"] = BASE
    data["implementation"]["worktree"] = str(worktree)
    manifest = tmp_path / "workflow.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    rc = main(["workflow-gate", str(manifest), "--phase", "start", "--repo", str(worktree)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "base_commit" in out and "HEAD" in out


def test_e2e_mock_start_gate_accepts_clean_exact_worktree(tmp_path, capsys):
    _, worktree = _linked_worktree(tmp_path)
    head = _head(worktree)
    data = _manifest(tmp_path)
    data["base_commit"] = head
    data["implementation"]["worktree"] = str(worktree)
    review_path = Path(data["review_artifacts"][0]["path"])
    review_data = json.loads(review_path.read_text(encoding="utf-8"))
    review_data["base_commit"] = head
    review_path.write_text(json.dumps(review_data), encoding="utf-8")
    manifest = tmp_path / "workflow.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    rc = main(["workflow-gate", str(manifest), "--phase", "start", "--repo", str(worktree)])
    assert rc == 0
    assert "[workflow-gate ok]" in capsys.readouterr().out


def test_start_gate_rejects_primary_checkout(tmp_path):
    """path が一致していても primary checkout なら「専用 worktree」ではない。"""
    repo = _seeded_repo(tmp_path)
    _git(repo, "checkout", "-qb", "feat/review-1")
    data = _manifest(tmp_path)
    data["base_commit"] = _head(repo)
    data["implementation"]["worktree"] = str(repo)

    errors = validate_live_git(data, phase="start", repo=repo)
    assert any("primary checkout" in error for error in errors)


def test_live_fan_in_rejects_change_outside_owned_files(tmp_path):
    _, worktree = _linked_worktree(tmp_path)
    base = _head(worktree)
    (worktree / "outside.txt").write_text("outside", encoding="utf-8")
    _git(worktree, "add", "outside.txt")
    _git(worktree, "commit", "-qm", "implementation")
    data = _manifest(tmp_path)
    data["base_commit"] = base
    data["implementation"]["worktree"] = str(worktree)
    data["fan_in"]["commit_sha"] = _head(worktree)

    errors = validate_live_git(data, phase="fan-in", repo=worktree)
    assert any("owned_files 外" in error and "outside.txt" in error for error in errors)


def test_live_fan_in_rejects_rename_of_unowned_file(tmp_path):
    """所有外ファイルを owned な名前へ改名しても素通りしない。

    git diff は rename 検出が既定で有効なので --name-only は新しい名前しか返さない。
    --no-renames が無いと「owned しか触っていない」に見える (fail-open)。
    """
    _, worktree = _linked_worktree(tmp_path)
    (worktree / "outside.txt").write_text("payload", encoding="utf-8")
    _git(worktree, "add", "outside.txt")
    _git(worktree, "commit", "-qm", "seed outside")
    base = _head(worktree)

    owned = worktree / "owned.py"
    _git(worktree, "mv", "outside.txt", "owned.py")
    _git(worktree, "commit", "-qm", "rename")
    assert owned.exists()

    data = _manifest(tmp_path)
    data["base_commit"] = base
    data["implementation"]["worktree"] = str(worktree)
    data["implementation"]["owned_files"] = ["owned.py"]
    data["fan_in"]["commit_sha"] = _head(worktree)

    errors = validate_live_git(data, phase="fan-in", repo=worktree)
    assert any("owned_files 外" in error and "outside.txt" in error for error in errors)


def test_live_fan_in_rejects_dirty_worktree(tmp_path):
    """所有外の未 commit を残したまま fan-in が ok を返さない。"""
    _, worktree = _linked_worktree(tmp_path)
    base = _head(worktree)
    (worktree / "roundtable").mkdir()
    (worktree / "roundtable" / "review_workflow.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", "roundtable/review_workflow.py")
    _git(worktree, "commit", "-qm", "implementation")
    (worktree / "stray.txt").write_text("uncommitted", encoding="utf-8")

    data = _manifest(tmp_path)
    data["base_commit"] = base
    data["implementation"]["worktree"] = str(worktree)
    data["implementation"]["owned_files"] = ["roundtable/review_workflow.py"]
    data["fan_in"]["commit_sha"] = _head(worktree)

    errors = validate_live_git(data, phase="fan-in", repo=worktree)
    assert any("clean ではない" in error for error in errors)


def test_live_fan_in_rejects_empty_receipt(tmp_path):
    """実装 commit ゼロ (commit_sha == base_commit) を子孫判定が通してしまわない。"""
    _, worktree = _linked_worktree(tmp_path)
    base = _head(worktree)
    data = _manifest(tmp_path)
    data["base_commit"] = base
    data["implementation"]["worktree"] = str(worktree)
    data["fan_in"]["commit_sha"] = base

    errors = validate_live_git(data, phase="fan-in", repo=worktree)
    assert any("実装 commit が無い" in error for error in errors)


def test_valid_fan_in_manifest_passes_schema_only(tmp_path):
    assert validate_review_workflow(_manifest(tmp_path), phase="fan-in") == []


def test_finding_namespace_rejects_ambiguous_delimiters(tmp_path):
    data = _manifest(tmp_path)
    data["review_artifacts"][0]["reviewer"] = "a:b"
    assert any("':'" in error for error in validate_review_workflow(data, phase="start"))

    data = _manifest(tmp_path)
    review = Path(data["review_artifacts"][0]["path"])
    payload = json.loads(review.read_text(encoding="utf-8"))
    payload["findings"][0]["id"] = "b:c"
    review.write_text(json.dumps(payload), encoding="utf-8")
    assert any("':'" in error for error in validate_review_workflow(data, phase="start"))


def test_permission_boundary_rejects_contradictory_allowed_operation(tmp_path):
    data = _manifest(tmp_path)
    data["permission_boundary"]["allowed"].append("merge")
    errors = validate_review_workflow(data, phase="start")
    assert any("allowed" in error and "merge" in error for error in errors)


def test_review_artifact_rejects_non_object_without_traceback(tmp_path):
    data = _manifest(tmp_path)
    Path(data["review_artifacts"][0]["path"]).write_text("[]", encoding="utf-8")
    errors = validate_review_workflow(data, phase="start")
    assert any("object" in error for error in errors)


def test_cli_rejects_non_utf8_manifest_without_traceback(tmp_path, capsys):
    manifest = tmp_path / "workflow.json"
    manifest.write_bytes(b"\xff\xfe")
    rc = main(["workflow-gate", str(manifest), "--phase", "start", "--repo", str(tmp_path)])
    assert rc == 1
    assert "manifest を読めない" in capsys.readouterr().out


def test_start_gate_rejects_actual_nonstandard_default_branch(tmp_path):
    repo, worktree = _linked_worktree(tmp_path, branch="trunk")
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/trunk")
    remote = tmp_path / "main-remote.git"
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/trunk"],
        check=True,
    )
    data = _manifest(tmp_path)
    data["implementation"]["branch"] = "trunk"
    data["implementation"]["worktree"] = str(worktree)
    data["base_commit"] = _head(worktree)
    errors = validate_live_git(data, phase="start", repo=worktree)
    assert any("default branch" in error and "trunk" in error for error in errors)


def test_start_gate_fails_closed_when_default_branch_is_unknown(tmp_path):
    repo, worktree = _linked_worktree(tmp_path)
    _git(repo, "remote", "remove", "origin")
    data = _manifest(tmp_path)
    data["implementation"]["worktree"] = str(worktree)
    data["base_commit"] = _head(worktree)
    errors = validate_live_git(data, phase="start", repo=worktree)
    assert any("default branch" in error and "判定できない" in error for error in errors)


def test_start_gate_does_not_trust_stale_origin_head(tmp_path):
    repo = _seeded_repo(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
    _git(repo, "checkout", "--detach", "-q")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(worktree), "main")
    data = _manifest(tmp_path)
    data["implementation"]["branch"] = "main"
    data["implementation"]["worktree"] = str(worktree)
    data["base_commit"] = _head(worktree)
    errors = validate_live_git(data, phase="start", repo=worktree)
    assert any("default branch (main)" in error for error in errors)


def test_fan_in_requires_every_cluster_owner_terminal(tmp_path):
    data = _manifest(tmp_path)
    data["root_cause_clusters"][0]["owner"] = "specialist-lane"
    errors = validate_review_workflow(data, phase="fan-in")
    assert any("specialist-lane" in error and "terminal" in error for error in errors)


def test_fan_in_requires_passed_receipt_for_every_cluster(tmp_path):
    data = _manifest(tmp_path)
    data["root_cause_clusters"].append({
        "id": "RC2", "finding_ids": ["grok:R1"],
        "owner": "implementation-lane", "test": "tests/test_other.py",
    })
    errors = validate_review_workflow(data, phase="fan-in")
    assert any("RC2" in error and "test receipt" in error for error in errors)


def test_live_gate_forces_untracked_files_visible(tmp_path):
    repo, worktree = _linked_worktree(tmp_path)
    _git(repo, "config", "status.showUntrackedFiles", "no")
    (worktree / "hidden.txt").write_text("hidden", encoding="utf-8")
    data = _manifest(tmp_path)
    data["implementation"]["worktree"] = str(worktree)
    data["base_commit"] = _head(worktree)
    errors = validate_live_git(data, phase="start", repo=worktree)
    assert any("clean ではない" in error for error in errors)


def test_live_fan_in_rejects_empty_descendant_commit(tmp_path):
    _, worktree = _linked_worktree(tmp_path)
    base = _head(worktree)
    _git(worktree, "commit", "--allow-empty", "-qm", "empty")
    data = _manifest(tmp_path)
    data["base_commit"] = base
    data["implementation"]["worktree"] = str(worktree)
    data["fan_in"]["commit_sha"] = _head(worktree)
    errors = validate_live_git(data, phase="fan-in", repo=worktree)
    assert any("変更ファイルが無い" in error for error in errors)


def test_live_fan_in_rejects_transient_unowned_commit(tmp_path):
    _, worktree = _linked_worktree(tmp_path)
    base = _head(worktree)
    (worktree / "outside.txt").write_text("temporary", encoding="utf-8")
    _git(worktree, "add", "outside.txt")
    _git(worktree, "commit", "-qm", "touch unowned")
    (worktree / "outside.txt").unlink()
    (worktree / "owned.py").write_text("owned", encoding="utf-8")
    _git(worktree, "add", "outside.txt", "owned.py")
    _git(worktree, "commit", "-qm", "restore tree and implement")
    data = _manifest(tmp_path)
    data["base_commit"] = base
    data["implementation"]["worktree"] = str(worktree)
    data["implementation"]["owned_files"] = ["owned.py"]
    data["fan_in"]["commit_sha"] = _head(worktree)
    errors = validate_live_git(data, phase="fan-in", repo=worktree)
    assert any("owned_files 外" in error and "outside.txt" in error for error in errors)
