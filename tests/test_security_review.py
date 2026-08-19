"""Findings from the 2026-08-07 security review, each pinned.

The common shape: a directory the AGENT writes and the SYSTEM collects. Whatever
crosses that boundary must be a real file inside the directory — a symlink is an
instruction to exfiltrate whatever it points at.
"""

import json
from pathlib import Path

import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os.executors.base import RunResult


@pytest.mark.asyncio
async def test_a_symlink_in_the_preview_dir_is_refused(orch, seeded, tmp_path):
    """S1: preview/x.png -> ~/.bastet/api_token would copy the token into
    artifacts and send it to Telegram as a 'photo'."""
    secret_file = tmp_path / "api_token"
    secret_file.write_text("tok_supersecret")

    def leaves_previews(task):
        preview = Path(task.workdir) / "._bastet" / "preview"
        preview.mkdir(parents=True)
        (preview / "honest.png").write_bytes(b"\x89PNG real screenshot")
        (preview / "sneaky.png").symlink_to(secret_file)
        return RunResult(status="succeeded", summary="ready")

    add_template(seeded, "dev", [{"name": "ship", "gate": "human-approve"}])
    SCRIPT.append(leaves_previews)
    job_id = orch.dispatch(req(template_id="dev", use_worktree=True))
    await orch.wait_idle()

    kept = json.loads(seeded.one(
        "SELECT detail_json FROM audit_log WHERE action='job.previews'")
        ["detail_json"])["files"]
    assert kept == ["_review-manifest.md", "honest.png"]
    folder = orch.home.artifacts_dir / job_id / "preview"
    assert not (folder / "sneaky.png").exists()
    assert b"tok_supersecret" not in b"".join(
        p.read_bytes() for p in folder.iterdir())


@pytest.mark.asyncio
async def test_a_symlink_in_the_chat_outbox_is_refused(tmp_path, monkeypatch):
    """S2: the outbox has no extension filter at all — a symlink would attach
    any host file to the conversation."""
    from fastapi.testclient import TestClient

    from bastet_agent_os import chat
    from bastet_agent_os.config import Home
    from bastet_agent_os.db import Db
    from bastet_agent_os.server import create_app

    home = Home(tmp_path / "home")
    client = TestClient(create_app(home), base_url="http://127.0.0.1")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"
    repo = tmp_path / "repo"
    repo.mkdir()
    client.post("/api/teams", json={"id": "t1", "name": "T"})
    client.post("/api/projects", json={"id": "p1", "repo_path": str(repo),
                                       "team_id": "t1"})
    client.post("/api/agents", json={"id": "fakebot", "name": "F",
                                     "executor_type": "fake"})
    secret_file = tmp_path / "deploy.key"
    secret_file.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")

    def generates(task):
        outbox = Path(task.extra_env["BASTET_CHAT_OUTBOX"])
        (outbox / "cat.png").write_bytes(b"\x89PNG real")
        (outbox / "exfil.txt").symlink_to(secret_file)
        return RunResult(status="succeeded", summary="done")

    SCRIPT.append(generates)
    db = Db(home.db_path)
    session_id = chat.create_session(db, scope_type="project", scope_id="p1",
                                     responder_kind="agent",
                                     responder_id="fakebot")
    chat.add_message(db, session_id, role="user", content="畫圖")

    message = await chat.reply(db, home.root, session_id)

    names = [a["name"] for a in message["attachments"]]
    assert names == ["cat.png"]
    db.close()


def test_preview_endpoints_do_not_traverse_on_job_id(tmp_path):
    """S3: /api/jobs/../previews walked out of the artifacts dir."""
    from fastapi.testclient import TestClient

    from bastet_agent_os.config import Home
    from bastet_agent_os.server import create_app

    home = Home(tmp_path / "home")
    client = TestClient(create_app(home), base_url="http://127.0.0.1")
    # a sibling of artifacts/ that `artifacts/../preview` would resolve into
    (home.root / "preview").mkdir()
    (home.root / "preview" / "leak.txt").write_text("outside artifacts")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"

    # a literal ".." is normalised away by the router before the handler; the
    # encoded form is the one that reaches the path parameter
    listed = client.get("/api/jobs/%2e%2e/previews")
    assert listed.status_code != 200 or listed.json() == []
    got = client.get("/api/jobs/%2e%2e/previews/leak.txt")
    assert got.status_code == 404


def test_the_builtin_config_skill_is_not_editable_from_chat(tmp_path):
    """S4: redirecting bastet-config's skill_source would poison the guide every
    future conversation reads."""
    from fastapi.testclient import TestClient

    from bastet_agent_os.config import Home
    from bastet_agent_os.server import create_app

    home = Home(tmp_path / "home")
    client = TestClient(create_app(home), base_url="http://127.0.0.1")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"

    out = client.post("/api/config/apply", json={"actions": [
        {"op": "resource.update", "name": "bastet-config",
         "config": {"skill_source": "/tmp/attacker.md"}}]}).json()

    assert out["failed"] == 1
    assert "bastet-config" in out["results"][0]["detail"]


@pytest.mark.asyncio
async def test_quota_auto_resume_does_not_refill_the_rework_budget(orch, seeded):
    """C1: the budget refill is the human's 'I fixed the world'. The automatic
    quota-reset retry is not a judgement — refilling there would let a vendor
    limit interleaving with a rework loop disable the cycles cap entirely."""
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(RunResult(status="failed", summary="rate limit, retry later"))
    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()
    seeded.write("UPDATE jobs SET rework_count=2 WHERE id=?", (job_id,))

    SCRIPT.append(RunResult(status="succeeded", summary="ok"))
    orch.retry(job_id, user="server:quota-reset")
    await orch.wait_idle()

    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "done"
    assert job["rework_count"] == 2, "the machine retry must not grant a fresh lease"
