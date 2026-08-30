"""A card cannot claim completion before its declared delivery is real."""

import json
import subprocess
from pathlib import Path

import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os.executors.base import RunResult

pytestmark = pytest.mark.asyncio


@pytest.fixture
def origin(repo, tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(bare)],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "HEAD:main"],
                   check=True)
    return bare


def writes_release(task):
    (Path(task.workdir) / "feature.txt").write_text("delivered\n")
    (Path(task.workdir) / "package.json").write_text(
        json.dumps({"name": "sample", "version": "1.4.0"}))
    return RunResult(status="succeeded", summary="release ready")


async def test_required_branch_is_delivered_before_done(orch, seeded, origin):
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(writes_release)

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True,
                               delivery={"mode": "branch"}))
    await orch.wait_idle()

    job = seeded.one("SELECT status,delivery_status FROM jobs WHERE id=?", (job_id,))
    assert dict(job) == {"status": "done", "delivery_status": "succeeded"}
    assert seeded.one("SELECT status FROM deliveries WHERE job_id=?", (job_id,))[
        "status"] == "succeeded"
    actions = {r["action"] for r in seeded.query(
        "SELECT action FROM audit_log WHERE target_id=?", (job_id,))}
    assert "job.delivered" in actions
    assert "job.done" in actions


async def test_integration_merges_and_verifies_remote_target_before_done(
        orch, seeded, origin):
    profile = {
        "target_branch": "main",
        "target": "origin/main",
        "predeploy_command": "test -f feature.txt",
    }
    seeded.write("UPDATE projects SET config_json=? WHERE id='proj1'",
                 (json.dumps({"delivery_profile": profile}),))
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto",
                                   "delivery_modes": ["integration", "production"]}])
    SCRIPT.append(writes_release)

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True,
                               delivery={"mode": "integration"}))
    await orch.wait_idle()

    receipt = seeded.one("SELECT * FROM deliveries WHERE job_id=?", (job_id,))
    remote_main = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True).stdout.strip()
    evidence = json.loads(receipt["evidence_json"])
    assert receipt["status"] == "succeeded"
    assert receipt["target"] == "origin/main"
    assert receipt["version"] == ""
    assert remote_main == receipt["commit_sha"]
    assert evidence["integration"]["remote_commit_sha"] == remote_main
    assert evidence["predeploy"] == ""
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"


async def test_delivery_policy_rejects_parked_development_branch(orch, seeded):
    add_template(seeded, "dev", [{"name": "release", "gate": "human-approve",
                                   "delivery_modes": ["integration", "production"]}])
    with pytest.raises(ValueError, match="workflow requires delivery.mode"):
        orch.dispatch(req(template_id="dev", delivery={"mode": "branch"}))


async def test_delivery_repair_cannot_bypass_frozen_workflow_policy(
        orch, seeded, origin):
    add_template(seeded, "dev", [{"name": "release", "gate": "auto",
                                   "delivery_modes": ["integration", "production"]}])
    SCRIPT.append(writes_release)
    job_id = orch.dispatch(req(template_id="dev", use_worktree=True,
                               delivery={"mode": "integration", "profile": {
                                   "target_branch": "main",
                                   "predeploy_command": "true",
                               }}))
    await orch.wait_idle()
    with pytest.raises(ValueError, match="workflow requires delivery.mode"):
        orch.configure_delivery(job_id, {"mode": "branch"}, user="repair")


async def test_failed_required_delivery_blocks_instead_of_lying_done(
        orch, seeded, repo, tmp_path):
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    str(tmp_path / "missing.git")], check=True)
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(writes_release)

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True,
                               delivery={"mode": "branch"}))
    await orch.wait_idle()

    job = seeded.one("SELECT status,delivery_status FROM jobs WHERE id=?", (job_id,))
    assert dict(job) == {"status": "blocked", "delivery_status": "failed"}
    assert seeded.one("SELECT 1 AS x FROM audit_log WHERE action='job.done' "
                      "AND target_id=?", (job_id,)) is None
    assert seeded.one("SELECT 1 AS x FROM audit_log WHERE action='job.delivery_failed' "
                      "AND target_id=?", (job_id,)) is not None


async def test_production_requires_new_version_and_online_verification(
        orch, seeded, origin):
    profile = {
        "target_branch": "main",
        "target": "test-production",
        "predeploy_command": "test -f feature.txt",
        "deploy_command": "printf deployed > .deploy-proof",
        "verify_command": (
            "test -f .deploy-proof && test \"$BASTET_DELIVERY_VERSION\" = 1.4.0 "
            "&& test \"$BASTET_DELIVERY_TAG\" = v1.4.0 "
            "&& printf '{\"status\":\"verified\",\"commit_sha\":\"%s\","
            "\"version\":\"%s\",\"target\":\"%s\"}' "
            "\"$BASTET_DELIVERY_COMMIT\" \"$BASTET_DELIVERY_VERSION\" "
            "\"$BASTET_DELIVERY_TARGET\"")
    }
    seeded.write("UPDATE projects SET config_json=? WHERE id='proj1'",
                 (json.dumps({"delivery_profile": profile}),))
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(writes_release)

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True,
                               delivery={"mode": "production", "version": "1.4.0"}))
    await orch.wait_idle()

    job = seeded.one("SELECT status,delivery_status FROM jobs WHERE id=?", (job_id,))
    assert dict(job) == {"status": "done", "delivery_status": "succeeded"}
    receipt = seeded.one("SELECT * FROM deliveries WHERE job_id=?", (job_id,))
    assert receipt["version"] == "1.4.0"
    assert receipt["target"] == "test-production"
    assert receipt["commit_sha"]
    evidence = json.loads(receipt["evidence_json"])
    assert evidence["verification_receipt"] == {
        "status": "verified",
        "commit_sha": receipt["commit_sha"],
        "version": "1.4.0",
        "target": "test-production",
    }
    tags = subprocess.run(
        ["git", "--git-dir", str(origin), "tag", "--list"],
        capture_output=True, text=True, check=True).stdout.splitlines()
    assert tags == ["v1.4.0"]
    assert seeded.one("SELECT 1 AS x FROM audit_log WHERE action='job.deployed' "
                      "AND target_id=?", (job_id,)) is not None
    config = json.loads(seeded.one(
        "SELECT config_json FROM projects WHERE id='proj1'")["config_json"])
    assert config["last_delivery"]["version"] == "1.4.0"


async def test_production_failure_after_publish_never_claims_done(
        orch, seeded, origin):
    profile = {
        "target_branch": "main",
        "target": "test-production",
        "predeploy_command": "true",
        "deploy_command": "false",
        "verify_command": "true",
    }
    seeded.write("UPDATE projects SET config_json=? WHERE id='proj1'",
                 (json.dumps({"delivery_profile": profile}),))
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(writes_release)

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True,
                               delivery={"mode": "production", "version": "1.4.0"}))
    await orch.wait_idle()

    job = seeded.one("SELECT status,delivery_status,worktree_path FROM jobs WHERE id=?",
                     (job_id,))
    assert job["status"] == "blocked"
    assert job["delivery_status"] == "failed"
    assert job["worktree_path"]  # evidence and release state are preserved
    assert seeded.one("SELECT status FROM deliveries WHERE job_id=?", (job_id,))[
        "status"] == "failed"
    assert seeded.one("SELECT 1 FROM audit_log WHERE action='job.done' "
                      "AND target_id=?", (job_id,)) is None
    assert seeded.one("SELECT 1 FROM audit_log WHERE action='job.deployed' "
                      "AND target_id=?", (job_id,)) is None


@pytest.mark.parametrize(("verify_command", "error"), [
    ("true", "must emit a JSON deployment receipt"),
    (("printf '{\"status\":\"verified\",\"commit_sha\":\"stale\","
      "\"version\":\"1.4.0\",\"target\":\"test-production\"}'"),
     "commit_sha expected"),
])
async def test_production_blocks_without_an_exact_provider_receipt(
        orch, seeded, origin, verify_command, error):
    profile = {
        "target_branch": "main",
        "target": "test-production",
        "predeploy_command": "true",
        "deploy_command": "true",
        "verify_command": verify_command,
    }
    seeded.write("UPDATE projects SET config_json=? WHERE id='proj1'",
                 (json.dumps({"delivery_profile": profile}),))
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(writes_release)

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True,
                               delivery={"mode": "production", "version": "1.4.0"}))
    await orch.wait_idle()

    job = seeded.one("SELECT status,delivery_status FROM jobs WHERE id=?", (job_id,))
    assert dict(job) == {"status": "blocked", "delivery_status": "failed"}
    receipt = seeded.one("SELECT status,error FROM deliveries WHERE job_id=?", (job_id,))
    assert receipt["status"] == "failed"
    assert error in receipt["error"]
    assert seeded.one("SELECT 1 FROM audit_log WHERE action='job.deployed' "
                      "AND target_id=?", (job_id,)) is None


async def test_production_profile_is_rejected_before_job_creation_when_incomplete(
        orch, seeded):
    seeded.write("UPDATE projects SET config_json=? WHERE id='proj1'",
                 (json.dumps({"delivery_profile": {
                     "target_branch": "main",
                     "predeploy_command": "true",
                     "deploy_command": "true",
                 }}),))
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    before = seeded.one("SELECT COUNT(*) AS n FROM jobs")["n"]

    with pytest.raises(ValueError, match="missing: verify_command"):
        orch.dispatch(req(template_id="dev", use_worktree=True,
                          delivery={"mode": "production", "version": "1.4.0"}))
    assert seeded.one("SELECT COUNT(*) AS n FROM jobs")["n"] == before


async def test_failed_predeploy_gate_moves_neither_main_nor_tag(
        orch, seeded, origin):
    original_main = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True).stdout.strip()
    profile = {
        "target_branch": "main",
        "target": "test-production",
        "predeploy_command": "false",
        "deploy_command": "true",
        "verify_command": "true",
    }
    seeded.write("UPDATE projects SET config_json=? WHERE id='proj1'",
                 (json.dumps({"delivery_profile": profile}),))
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(writes_release)

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True,
                               delivery={"mode": "production", "version": "1.4.0"}))
    await orch.wait_idle()

    current_main = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert current_main == original_main
    assert subprocess.run(
        ["git", "--git-dir", str(origin), "tag", "--list"],
        capture_output=True, text=True, check=True).stdout.strip() == ""
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))[
        "status"] == "blocked"


async def test_historic_false_done_card_can_be_reopened_for_delivery(
        orch, seeded, origin):
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(writes_release)
    job_id = orch.dispatch(req(template_id="dev", use_worktree=True,
                               delivery={"mode": "none"}))
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"

    result = orch.configure_delivery(job_id, {"mode": "branch"}, user="repair")
    assert result["status"] == "in_progress"
    await orch.wait_idle()

    job = seeded.one("SELECT status,delivery_status FROM jobs WHERE id=?", (job_id,))
    assert dict(job) == {"status": "done", "delivery_status": "succeeded"}
    assert seeded.one("SELECT status FROM deliveries WHERE job_id=?", (job_id,))[
        "status"] == "succeeded"


async def test_historic_repair_reuses_only_a_bastet_owned_worktree(
        orch, seeded, repo):
    branch = "bastet/job1"
    subprocess.run(["git", "-C", str(repo), "branch", branch], check=True)
    repair_path = orch.home.root / "release-worktrees" / "job1-repair"
    repair_path.parent.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q",
                    str(repair_path), branch], check=True)

    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")
    assert orch._ensure_workdir(job, True) == str(repair_path.resolve())
    assert seeded.one("SELECT worktree_path FROM jobs WHERE id='job1'")[
        "worktree_path"] == str(repair_path.resolve())
