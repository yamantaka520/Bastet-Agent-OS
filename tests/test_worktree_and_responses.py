"""Worktree lifecycle (cleanup/gc) and the /v1/responses gateway wire."""

import json
import subprocess

import httpx
import pytest
from fake_executor import SCRIPT, req
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bastet_agent_os import run_tokens
from bastet_agent_os.db import now
from bastet_agent_os.executors.base import RunResult
from bastet_agent_os.gateway import GatewayContext, build_router
from bastet_agent_os.governance import Reservations
from bastet_agent_os.pricing import PriceBook

# ---- worktree cleanup -----------------------------------------------------------


@pytest.fixture
def git_project(seeded, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "README.md").write_text("# demo\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    seeded.write("UPDATE projects SET repo_path=? WHERE id='proj1'", (str(repo),))
    return repo


async def test_worktree_removed_on_done_branch_survives(orch, seeded, git_project):
    SCRIPT.append(RunResult(status="succeeded"))
    job_id = orch.dispatch(req(use_worktree=True))
    await orch.wait_idle()

    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "done"
    assert job["worktree_path"] is None  # checkout swept...
    assert not (orch.home.worktrees_dir / job_id).exists()
    branches = subprocess.run(["git", "branch", "--list", f"bastet/{job_id}"],
                              cwd=git_project, capture_output=True, text=True).stdout
    assert f"bastet/{job_id}" in branches  # ...but the branch survives


async def test_keep_worktrees_opts_out(orch, seeded, git_project):
    seeded.write("UPDATE projects SET config_json=? WHERE id='proj1'",
                 (json.dumps({"keep_worktrees": True}),))
    SCRIPT.append(RunResult(status="succeeded"))
    job_id = orch.dispatch(req(use_worktree=True))
    await orch.wait_idle()
    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["worktree_path"] is not None
    assert (orch.home.worktrees_dir / job_id).exists()


async def test_gc_sweeps_leftovers(orch, seeded, git_project):
    seeded.write("UPDATE projects SET config_json='{\"keep_worktrees\": true}' "
                 "WHERE id='proj1'")
    SCRIPT.append(RunResult(status="succeeded"))
    job_id = orch.dispatch(req(use_worktree=True))
    await orch.wait_idle()
    assert (orch.home.worktrees_dir / job_id).exists()

    seeded.write("UPDATE projects SET config_json='{}' WHERE id='proj1'")
    assert orch.gc_worktrees() == 1
    assert not (orch.home.worktrees_dir / job_id).exists()
    assert orch.gc_worktrees() == 0  # idempotent


# ---- /v1/responses gateway wire ---------------------------------------------------


def test_responses_passthrough_meters(seeded, monkeypatch):
    monkeypatch.setenv("TEST_UPSTREAM_KEY", "sk-upstream")
    ts = now()
    seeded.write("INSERT INTO resources(id, kind, name, endpoint, api_flavor, secret_ref, "
                 "created_at, updated_at) VALUES('res-oai','llm','oai-gw',"
                 "'https://oai.example','openai','env:TEST_UPSTREAM_KEY',?,?)", (ts, ts))
    seeded.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, budget_usd, "
                 "created_at) VALUES('grt-oai','res-oai','project','proj1',10.0,?)", (ts,))
    seeded.write("UPDATE runs SET resource_id='res-oai' WHERE id='run1'")

    captured = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={
            "id": "resp_1", "model": "gpt-5.1-codex",
            "output": [{"type": "message", "content": [{"type": "output_text",
                                                        "text": "done"}]}],
            "usage": {"input_tokens": 50, "output_tokens": 10,
                      "input_tokens_details": {"cached_tokens": 30}},
        })

    app = FastAPI()
    app.include_router(build_router(GatewayContext(seeded, PriceBook(), Reservations()),
                                    upstream_transport=httpx.MockTransport(upstream)))
    client = TestClient(app)
    token = run_tokens.issue(seeded, "run1", ttl_seconds=60)
    resp = client.post("/v1/responses", json={"model": "gpt-5.1-codex", "input": "hi"},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert captured["url"] == "https://oai.example/v1/responses"
    row = seeded.one("SELECT * FROM usage_ledger WHERE run_id='run1'")
    assert (row["tokens_in"], row["tokens_out"], row["cache_read"]) == (20, 10, 30)


def test_responses_wire_rejects_anthropic_resource(seeded):
    # run1's default resource (res1) is anthropic-flavor
    app = FastAPI()
    app.include_router(build_router(GatewayContext(seeded, PriceBook(), Reservations())))
    client = TestClient(app)
    token = run_tokens.issue(seeded, "run1", ttl_seconds=60)
    resp = client.post("/v1/responses", json={"input": "hi"},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400
