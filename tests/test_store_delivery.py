"""App Store and Google Play are asynchronous, durable delivery providers."""

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os.executors.base import RunResult

pytestmark = pytest.mark.asyncio


@pytest.fixture
def origin(repo, tmp_path):
    bare = tmp_path / "store-origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(bare)],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "HEAD:main"],
                   check=True)
    return bare


def writes_mobile_release(task):
    (Path(task.workdir) / "mobile.bin").write_text("signed build\n")
    (Path(task.workdir) / "package.json").write_text(json.dumps({
        "name": "mobile-canary",
        "version": "1.4.0",
    }))
    return RunResult(status="succeeded", summary="mobile release ready")


def command(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def store_profile(provider: str, live_file: Path) -> tuple[dict, dict]:
    target = "app-store:123456" if provider == "app_store_connect" \
        else "google-play:com.example.canary:production"
    identity = ({"app_id": "123456"} if provider == "app_store_connect" else
                {"package_name": "com.example.canary", "track": "production"})
    provider_status = "WAITING_FOR_REVIEW" if provider == "app_store_connect" \
        else "inProgress"
    receipt = {
        "provider": provider,
        **identity,
        "milestone": "submitted",
        "provider_status": provider_status,
    }
    deploy_script = (
        "import json,os,pathlib;"
        f"payload={receipt!r};"
        "payload.update(commit_sha=os.environ['BASTET_DELIVERY_COMMIT'],"
        "version=os.environ['BASTET_DELIVERY_VERSION'],"
        "target=os.environ['BASTET_DELIVERY_TARGET']);"
        f"pathlib.Path({str(live_file)!r}).write_text(json.dumps(payload))"
    )
    profile = {
        "provider": provider,
        **identity,
        "release_goal": "published",
        "poll_interval_seconds": 1,
        "target_branch": "main",
        "target": target,
        "predeploy_command": "test -f mobile.bin",
        "deploy_command": command(deploy_script),
        "verify_command": command(
            f"import pathlib;print(pathlib.Path({str(live_file)!r}).read_text())"),
    }
    return profile, identity


@pytest.mark.parametrize("provider", ["app_store_connect", "google_play"])
async def test_store_submission_waits_across_restart_until_published(
        orch, seeded, origin, tmp_path, provider):
    live_file = tmp_path / f"{provider}.json"
    profile, identity = store_profile(provider, live_file)
    seeded.write("UPDATE projects SET config_json=? WHERE id='proj1'",
                 (json.dumps({"delivery_profile": profile}),))
    add_template(seeded, "mobile", [{"name": "release", "gate": "human-approve",
                                      "delivery_modes": ["production"]}])
    SCRIPT.append(writes_mobile_release)

    job_id = orch.dispatch(req(template_id="mobile", use_worktree=True,
                               delivery={"mode": "production", "version": "1.4.0"}))
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == \
        "blocked"
    orch.approve(job_id, True, "release submission approved", user="release-manager")
    await orch.wait_idle()

    job = seeded.one("SELECT status,delivery_status,worktree_path FROM jobs WHERE id=?",
                     (job_id,))
    assert (job["status"], job["delivery_status"]) == \
        ("in_progress", "waiting_external")
    assert Path(job["worktree_path"]).is_dir()
    delivery = seeded.one("SELECT * FROM deliveries WHERE job_id=?", (job_id,))
    assert delivery["status"] == "waiting_external"
    assert delivery["provider"] == provider
    assert delivery["provider_status"] in {"WAITING_FOR_REVIEW", "inProgress"}
    assert delivery["finished_at"] is None
    assert seeded.one("SELECT 1 FROM audit_log WHERE action='job.deployed' "
                      "AND target_id=?", (job_id,)) is None
    frozen = json.loads(seeded.one(
        "SELECT delivery_json FROM jobs WHERE id=?", (job_id,))["delivery_json"])
    assert frozen["profile"]["provider"] == provider
    seeded.write("UPDATE projects SET config_json=? WHERE id='proj1'",
                 (json.dumps({"delivery_profile": {
                     **profile, "verify_command": "false",
                 }}),))

    seeded.write("UPDATE deliveries SET status='polling' WHERE id=?",
                 (delivery["id"],))
    recovered = orch.resume_interrupted_jobs(actor="test:restart")
    assert job_id not in recovered["resumed"]
    assert job_id not in recovered["parked"]
    assert seeded.one("SELECT status FROM deliveries WHERE id=?", (delivery["id"],))[
        "status"] == "waiting_external"

    receipt = json.loads(live_file.read_text())
    receipt.update({
        "milestone": "published",
        "provider_status": (
            "READY_FOR_DISTRIBUTION" if provider == "app_store_connect"
            else "completed"),
    })
    live_file.write_text(json.dumps(receipt))
    seeded.write("UPDATE deliveries SET status='polling',"
                 "next_poll_at='2020-01-01T00:00:00+00:00' WHERE id=?",
                 (delivery["id"],))

    outcome = await orch.poll_external_deliveries()

    assert outcome == {"completed": [job_id], "waiting": [], "failed": []}
    assert dict(seeded.one(
        "SELECT status,delivery_status FROM jobs WHERE id=?", (job_id,))) == {
            "status": "done", "delivery_status": "succeeded"}
    final = seeded.one("SELECT * FROM deliveries WHERE id=?", (delivery["id"],))
    assert final["status"] == "succeeded"
    assert final["provider_status"] in {"READY_FOR_DISTRIBUTION", "completed"}
    evidence = json.loads(final["evidence_json"])["verification_receipt"]
    assert evidence["milestone"] == "published"
    assert all(evidence[key] == value for key, value in identity.items())
    assert seeded.one("SELECT COUNT(*) AS n FROM audit_log "
                      "WHERE action='job.deployed' AND target_id=?", (job_id,))["n"] == 1


async def test_store_rejection_is_terminal_and_never_claims_deployment(
        orch, seeded, origin, tmp_path):
    live_file = tmp_path / "rejected.json"
    profile, _ = store_profile("app_store_connect", live_file)
    seeded.write("UPDATE projects SET config_json=? WHERE id='proj1'",
                 (json.dumps({"delivery_profile": profile}),))
    add_template(seeded, "mobile", [{"name": "release", "gate": "human-approve"}])
    SCRIPT.append(writes_mobile_release)
    job_id = orch.dispatch(req(template_id="mobile", use_worktree=True,
                               delivery={"mode": "production", "version": "1.4.0"}))
    await orch.wait_idle()
    orch.approve(job_id, True, "release submission approved", user="release-manager")
    await orch.wait_idle()
    delivery = seeded.one("SELECT * FROM deliveries WHERE job_id=?", (job_id,))
    receipt = json.loads(live_file.read_text())
    receipt.update({"milestone": "rejected", "provider_status": "REJECTED"})
    live_file.write_text(json.dumps(receipt))
    seeded.write("UPDATE deliveries SET next_poll_at='2020-01-01T00:00:00+00:00' "
                 "WHERE id=?", (delivery["id"],))

    outcome = await orch.poll_external_deliveries()

    assert outcome == {"completed": [], "waiting": [], "failed": [job_id]}
    assert dict(seeded.one(
        "SELECT status,delivery_status FROM jobs WHERE id=?", (job_id,))) == {
            "status": "blocked", "delivery_status": "failed"}
    assert "rejected release" in seeded.one(
        "SELECT error FROM deliveries WHERE id=?", (delivery["id"],))["error"]
    assert seeded.one("SELECT 1 FROM audit_log WHERE action='job.deployed' "
                      "AND target_id=?", (job_id,)) is None


async def test_store_dispatch_rejects_workflow_without_terminal_human_approval(
        orch, seeded, origin, tmp_path):
    profile, _ = store_profile("google_play", tmp_path / "never.json")
    seeded.write("UPDATE projects SET config_json=? WHERE id='proj1'",
                 (json.dumps({"delivery_profile": profile}),))
    add_template(seeded, "unsafe-mobile", [{"name": "release", "gate": "auto"}])

    with pytest.raises(ValueError, match="human-approve terminal stage"):
        orch.dispatch(req(
            template_id="unsafe-mobile", use_worktree=True,
            delivery={"mode": "production", "version": "1.4.0"}))


async def test_official_api_adapter_polls_durable_submission_without_redeploy(
        orch, seeded, origin, monkeypatch, tmp_path):
    deploy_count = tmp_path / "official-deploy-count.txt"
    receipt = {
        "provider": "app_store_connect",
        "app_id": "123456",
        "app_store_version_id": "version-7",
    }
    deploy_script = (
        "import json,os,pathlib;"
        f"counter=pathlib.Path({str(deploy_count)!r});"
        "counter.write_text(str(int(counter.read_text() or '0')+1) "
        "if counter.exists() else '1');"
        f"payload={receipt!r};"
        "payload.update(commit_sha=os.environ['BASTET_DELIVERY_COMMIT'],"
        "version=os.environ['BASTET_DELIVERY_VERSION'],"
        "target=os.environ['BASTET_DELIVERY_TARGET']);"
        "print(json.dumps(payload))"
    )
    profile = {
        "provider": "app_store_connect",
        "status_adapter": "official_api",
        "app_id": "123456",
        "release_goal": "published",
        "poll_interval_seconds": 1,
        "target_branch": "main",
        "target": "app-store:123456",
        "predeploy_command": "test -f mobile.bin",
        "deploy_command": command(deploy_script),
    }
    seeded.write("UPDATE projects SET config_json=? WHERE id='proj1'",
                 (json.dumps({"delivery_profile": profile}),))
    add_template(seeded, "official-mobile", [
        {"name": "release", "gate": "human-approve", "delivery_modes": ["production"]},
    ])
    SCRIPT.append(writes_mobile_release)
    statuses = iter([("submitted", "IN_REVIEW"),
                     ("published", "READY_FOR_DISTRIBUTION")])
    calls = []

    def fake_status(actual_profile, submission, env):
        calls.append((actual_profile, submission, env))
        milestone, provider_status = next(statuses)
        return {
            "provider": "app_store_connect",
            "app_id": "123456",
            "commit_sha": submission["commit_sha"],
            "version": submission["version"],
            "target": submission["target"],
            "milestone": milestone,
            "provider_status": provider_status,
        }

    monkeypatch.setattr("bastet_agent_os.store_adapters.official_status", fake_status)
    job_id = orch.dispatch(req(
        template_id="official-mobile", use_worktree=True,
        delivery={"mode": "production", "version": "1.4.0"}))
    await orch.wait_idle()
    orch.approve(job_id, True, "release submission approved", user="release-manager")
    await orch.wait_idle()

    delivery = seeded.one("SELECT * FROM deliveries WHERE job_id=?", (job_id,))
    evidence = json.loads(delivery["evidence_json"])
    assert delivery["status"] == "waiting_external"
    assert evidence["submission_receipt"]["app_store_version_id"] == "version-7"
    assert deploy_count.read_text() == "1"

    seeded.write("UPDATE deliveries SET next_poll_at='2020-01-01T00:00:00+00:00' "
                 "WHERE id=?", (delivery["id"],))
    outcome = await orch.poll_external_deliveries()

    assert outcome == {"completed": [job_id], "waiting": [], "failed": []}
    assert deploy_count.read_text() == "1"
    assert len(calls) == 2
    assert calls[0][1] == calls[1][1]
