import json
import subprocess
from pathlib import Path

from roundtable.cli import main
from roundtable.review_workflow import validate_review_workflow


BASE = "a" * 40
COMMIT = "b" * 40


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
            {"id": "RC1", "finding_ids": ["R1"], "owner": "implementation-lane", "test": "tests/test_review_workflow.py"}
        ],
        "fan_in": {
            "owner": "codex-mainline",
            "single_pr": True,
            "commit_sha": COMMIT,
            "tests": [{"command": "pytest", "status": "passed"}],
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


def test_start_gate_rejects_base_branch_and_ownership_violations(tmp_path):
    data = _manifest(tmp_path)
    data["base_commit"] = "main"
    data["implementation"]["branch"] = "main"
    data["implementation"]["owned_files"] = ["../outside.py", "roundtable/review_workflow.py", "roundtable/review_workflow.py"]
    errors = validate_review_workflow(data, phase="start")
    assert any("base_commit" in error for error in errors)
    assert any("default branch" in error for error in errors)
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


def test_cli_start_gate_checks_live_exact_base(tmp_path, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)
    (tmp_path / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "seed.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "seed"], check=True)
    data = _manifest(tmp_path)
    data["base_commit"] = BASE
    manifest = tmp_path / "workflow.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    rc = main(["workflow-gate", str(manifest), "--phase", "start", "--repo", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "base_commit" in out and "HEAD" in out


def test_e2e_mock_start_gate_accepts_clean_exact_worktree(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-qb", "feat/review-1", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    data = _manifest(tmp_path)
    data["base_commit"] = head
    data["implementation"]["worktree"] = str(repo)
    review_path = Path(data["review_artifacts"][0]["path"])
    review_data = json.loads(review_path.read_text(encoding="utf-8"))
    review_data["base_commit"] = head
    review_path.write_text(json.dumps(review_data), encoding="utf-8")
    manifest = tmp_path / "workflow.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    rc = main(["workflow-gate", str(manifest), "--phase", "start", "--repo", str(repo)])
    assert rc == 0
    assert "[workflow-gate ok]" in capsys.readouterr().out


def test_live_fan_in_rejects_change_outside_owned_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-qb", "feat/review-1", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    (repo / "outside.txt").write_text("outside", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "outside.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "implementation"], check=True)
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    data = _manifest(tmp_path)
    data["base_commit"] = base
    data["implementation"]["worktree"] = str(repo)
    data["fan_in"]["commit_sha"] = commit

    from roundtable.review_workflow import validate_live_git
    errors = validate_live_git(data, phase="fan-in", repo=repo)
    assert any("owned_files 外" in error and "outside.txt" in error for error in errors)


def test_valid_fan_in_manifest_passes_schema_only(tmp_path):
    assert validate_review_workflow(_manifest(tmp_path), phase="fan-in") == []
