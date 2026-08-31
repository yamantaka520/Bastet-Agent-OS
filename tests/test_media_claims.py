"""Async vendor media survives the Agent process and lands in the worktree."""

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from fake_executor import SCRIPT, add_template, req
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bastet_agent_os import media_claims, run_tokens
from bastet_agent_os.db import now
from bastet_agent_os.executors.base import RunResult
from bastet_agent_os.gateway import GatewayContext, build_router
from bastet_agent_os.governance import Reservations
from bastet_agent_os.pricing import PriceBook


def _resource(db, monkeypatch, **extra):
    monkeypatch.setenv("ASYNC_MEDIA_KEY", "media-secret")
    config = {
        "async_status_path": "/tasks/{task_id}",
        "async_status_field": "state",
        "async_success_values": "done",
        "async_failure_values": "failed",
        "async_result_url_field": "result.url",
        "async_download_hosts": "cdn.vendor.test",
        "async_poll_interval_seconds": "1",
        **extra,
    }
    stamp = now()
    db.write("INSERT INTO resources(id,kind,name,endpoint,secret_ref,config_json,"
             "created_at,updated_at) VALUES('async-media','video','cinema',"
             "'https://api.vendor.test','env:ASYNC_MEDIA_KEY',?,?,?)",
             (json.dumps(config), stamp, stamp))
    db.write("INSERT INTO grants(id,resource_id,scope_type,scope_id,created_at) "
             "VALUES('async-grant','async-media','project','proj1',?)", (stamp,))
    return db.one("SELECT * FROM resources WHERE id='async-media'")


def test_gateway_registers_idempotent_safe_claim(seeded, tmp_path, monkeypatch):
    resource = _resource(seeded, monkeypatch)
    seeded.write("UPDATE runs SET workdir=? WHERE id='run1'", (str(tmp_path),))
    app = FastAPI()
    app.include_router(build_router(
        GatewayContext(seeded, PriceBook(), Reservations()),
        upstream_transport=httpx.MockTransport(
            lambda request: httpx.Response(500))))
    client = TestClient(app)
    token = run_tokens.issue(seeded, "run1", ttl_seconds=60)
    headers = {"Authorization": f"Bearer {token}"}
    body = {"resource": resource["name"], "task_id": "vendor-123",
            "destination": "assets/movie.mp4"}

    first = client.post("/v1/media/claims", json=body, headers=headers)
    duplicate = client.post("/v1/media/claims", json=body, headers=headers)
    assert first.status_code == 201 and duplicate.status_code == 201
    assert first.json()["id"] == duplicate.json()["id"]
    assert seeded.one("SELECT COUNT(*) AS n FROM media_claims")["n"] == 1

    escaped = client.post("/v1/media/claims", json={**body, "destination": "../key"},
                          headers=headers)
    assert escaped.status_code == 400
    assert "relative worktree path" in escaped.json()["error"]


@pytest.mark.asyncio
async def test_worker_fetches_then_automatically_resumes_the_stage(
        orch, seeded, repo, monkeypatch):
    resource = _resource(seeded, monkeypatch)
    add_template(seeded, "media-flow", [{"name": "render", "gate": "auto"}])

    def submits_or_verifies(task):
        target = Path(task.workdir) / "assets/movie.mp4"
        if target.exists():
            assert target.read_bytes() == b"finished-video"
            return RunResult(status="succeeded", summary="download verified")
        run = seeded.one("SELECT * FROM runs WHERE id=?", (task.run_id,))
        media_claims.register(
            seeded, run, resource, provider_task_id="vendor-123",
            destination="assets/movie.mp4")
        return RunResult(status="succeeded", summary="vendor task registered")

    SCRIPT.extend([submits_or_verifies, submits_or_verifies])
    job_id = orch.dispatch(req(template_id="media-flow", use_worktree=True))
    await orch.wait_idle()
    assert dict(seeded.one(
        "SELECT status FROM jobs WHERE id=?", (job_id,))) == {"status": "blocked"}
    waiting = seeded.one(
        "SELECT status FROM runs WHERE job_id=? ORDER BY rowid DESC LIMIT 1", (job_id,))
    assert waiting["status"] == "waiting_external"
    # Simulate a process dying after claiming the poll. The next worker must
    # recover the expired fetching lease rather than leave the job parked forever.
    seeded.write("UPDATE media_claims SET status='fetching',"
                 "updated_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job_id,))

    seen = []

    def vendor(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.url.host == "api.vendor.test":
            return httpx.Response(200, json={
                "state": "done",
                "result": {"url": "https://cdn.vendor.test/result.mp4"},
            })
        return httpx.Response(200, content=b"finished-video",
                              headers={"content-type": "video/mp4"})

    worker = media_claims.MediaClaimWorker(
        seeded, orch, transport=httpx.MockTransport(vendor))
    assert await worker.run_once() == 1
    await orch.wait_idle()

    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    claim = seeded.one("SELECT * FROM media_claims WHERE job_id=?", (job_id,))
    assert claim["status"] == "fetched"
    assert claim["bytes"] == len(b"finished-video")
    assert claim["sha256"]
    assert seeded.one("SELECT 1 AS x FROM audit_log WHERE "
                      "action='media.claim_recovered'") is not None
    saved = subprocess.run(
        ["git", "-C", str(repo), "show", f"bastet/{job_id}:assets/movie.mp4"],
        capture_output=True, check=True).stdout
    assert saved == b"finished-video"
    assert seen[0] == ("https://api.vendor.test/tasks/vendor-123",
                       "Bearer media-secret")
    assert seen[1] == ("https://cdn.vendor.test/result.mp4", None)


@pytest.mark.asyncio
async def test_unapproved_download_host_fails_closed(orch, seeded, monkeypatch):
    resource = _resource(seeded, monkeypatch, async_max_attempts="9")
    workdir = Path(orch.home.root) / "claim-workdir"
    workdir.mkdir()
    seeded.write("UPDATE runs SET workdir=? WHERE id='run1'", (str(workdir),))
    claim = media_claims.register(
        seeded, seeded.one("SELECT * FROM runs WHERE id='run1'"), resource,
        provider_task_id="vendor-evil", destination="assets/result.bin")
    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")
    media_claims.park_if_pending(seeded, job, "run1", "work")

    def vendor(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "state": "done", "result": {"url": "https://evil.test/steal"}})

    worker = media_claims.MediaClaimWorker(
        seeded, orch, transport=httpx.MockTransport(vendor))
    await worker.run_once()
    failed = seeded.one("SELECT status,attempts,error FROM media_claims WHERE id=?",
                        (claim["id"],))
    assert failed["status"] == "failed"
    assert failed["attempts"] == 1
    assert "not allow-listed" in failed["error"]
    assert not (workdir / "assets/result.bin").exists()
