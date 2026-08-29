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
            "&& test -n \"$BASTET_DELIVERY_COMMIT\"")
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
