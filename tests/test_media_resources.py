"""The Novita case: a media API resource that is actually usable.

Two live failures. The test probe crashed with `Illegal header name` because the
agent wrote the whole header line into `auth_header`; and the chat responder had
the credential wired but no way to use it, so it truthfully said it had none.
"""

import json

import pytest

from bastet_agent_os import resource_kinds as rk
from bastet_agent_os import resource_test
from bastet_agent_os.db import now


def test_auth_header_accepts_both_shapes():
    """A header NAME and a full header LINE with a placeholder are both
    legitimate input — agents reading vendor docs write the latter."""
    cases = [
        ({"auth_header": "Authorization: Bearer {API_KEY}"}, "tok",
         ("Authorization", "Bearer tok")),
        ({"auth_header": "X-API-Key"}, "tok", ("X-API-Key", "tok")),
        ({"auth_header": "Authorization"}, "tok", ("Authorization", "Bearer tok")),
        ({}, "tok", ("Authorization", "Bearer tok")),
        ({"auth_header": "PRIVATE-TOKEN: {TOKEN}"}, "glpat-x",
         ("PRIVATE-TOKEN", "glpat-x")),
    ]
    for config, secret, expected in cases:
        assert rk.auth_header_pair(config, secret) == expected, config


def test_the_novita_shape_no_longer_crashes_the_probe(db, monkeypatch):
    """The exact stored config from the live host. The probe must produce a real
    verdict about the endpoint, not a LocalProtocolError about our own header."""
    ts = now()
    db.write("INSERT INTO resources(id, kind, name, endpoint, secret_ref, "
             "config_json, created_at, updated_at) VALUES('resnv','image',"
             "'novita-image','https://api.novita.ai','env:NOVITA_TEST_KEY',?,?,?)",
             (json.dumps({"default_model": "seedream-5.0-lite",
                          "auth_header": "Authorization: Bearer {API_KEY}"}), ts, ts))
    monkeypatch.setenv("NOVITA_TEST_KEY", "test-key-123")
    seen = {}

    def fake_get(url, headers):
        seen["url"] = url
        seen["headers"] = headers
        return {"status": "ok", "detail": "200"}

    monkeypatch.setattr(resource_test, "_get", fake_get)

    state = resource_test.run(db, "resnv", "tester")

    assert state["status"] == "ok"
    assert seen["headers"] == {"Authorization": "Bearer test-key-123"}
    assert "Illegal" not in json.dumps(state)


@pytest.mark.asyncio
async def test_chat_agent_receives_the_granted_resource(tmp_path, monkeypatch):
    """The credential must reach the conversation: env vars, the manifest in
    the prompt, and tools that can actually call an API (Bash, WebFetch)."""
    from fastapi.testclient import TestClient

    from bastet_agent_os import chat
    from bastet_agent_os.config import Home
    from bastet_agent_os.db import Db
    from bastet_agent_os.executors.base import RunResult, TaskSpec  # noqa: F401
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
    monkeypatch.setenv("NOVITA_TEST_KEY", "test-key-123")
    client.post("/api/resources", json={
        "name": "novita-image", "kind": "image", "endpoint": "https://api.novita.ai",
        "secret_ref": "env:NOVITA_TEST_KEY",
        "config": {"default_model": "seedream-5.0-lite"},
        "scope_type": "project", "scope_id": "p1"})

    captured: dict = {}

    def capture(task):
        captured["env"] = task.extra_env
        captured["prompt"] = task.prompt
        captured["tools"] = task.allowed_tools
        captured["read_only"] = task.read_only
        return RunResult(status="succeeded", summary="看得到資源了")

    from fake_executor import SCRIPT
    SCRIPT.append(capture)
    db = Db(home.db_path)
    session_id = chat.create_session(db, scope_type="project", scope_id="p1",
                                     responder_kind="agent", responder_id="fakebot")
    chat.add_message(db, session_id, role="user", content="畫一張貓")
    await chat.reply(db, home.root, session_id)

    assert captured["env"]["BASTET_RES_NOVITA_IMAGE_URL"] == "https://api.novita.ai"
    assert captured["env"]["BASTET_RES_NOVITA_IMAGE_KEY"] == "test-key-123"
    assert "novita-image" in captured["prompt"]        # the manifest names it
    assert "Bash" in captured["tools"]                 # and it is callable
    assert "Edit" not in captured["tools"] and "Write" not in captured["tools"]
    # the secret-bearing access dir is cleaned up after the reply
    leftovers = list((home.root / "run-access").glob("*")) \
        if (home.root / "run-access").exists() else []
    assert leftovers == []
    db.close()


@pytest.mark.asyncio
async def test_generated_files_come_back_into_the_conversation(tmp_path, monkeypatch):
    """The loop the user asked about: agent generates media → the file itself
    lands on the reply as an attachment (not a vendor URL that expires)."""
    from pathlib import Path

    from fake_executor import SCRIPT
    from fastapi.testclient import TestClient

    from bastet_agent_os import chat
    from bastet_agent_os.config import Home
    from bastet_agent_os.db import Db
    from bastet_agent_os.executors.base import RunResult
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

    def generates(task):
        outbox = Path(task.extra_env["BASTET_CHAT_OUTBOX"])
        (outbox / "cat.png").write_bytes(b"\x89PNG fake image")
        (outbox / "huge.bin").write_bytes(b"x" * (51 * 1024 * 1024))  # over cap
        assert "BASTET_CHAT_OUTBOX" in task.prompt or "存到" in task.prompt
        return RunResult(status="succeeded", summary="畫好了，檔案已放進 outbox")

    SCRIPT.append(generates)
    db = Db(home.db_path)
    session_id = chat.create_session(db, scope_type="project", scope_id="p1",
                                     responder_kind="agent", responder_id="fakebot")
    chat.add_message(db, session_id, role="user", content="畫一張貓")

    message = await chat.reply(db, home.root, session_id)

    names = [a["name"] for a in message["attachments"]]
    assert names == ["cat.png"]                    # the file, not a URL; cap held
    assert message["attachments"][0]["mime"] == "image/png"
    # downloadable through the session files endpoint
    file_id = message["attachments"][0]["id"]
    got = client.get(f"/api/chat/sessions/{session_id}/files/{file_id}")
    assert got.status_code == 200 and got.content.startswith(b"\x89PNG")
    # outbox cleaned up
    assert not (home.root / "chat-outbox").exists() or \
        list((home.root / "chat-outbox").iterdir()) == []
    db.close()


@pytest.mark.asyncio
async def test_media_stages_are_told_to_persist_assets(orch, seeded, monkeypatch):
    """Q2: a vendor's download URL expires — the brief for a stage with media
    resources granted must demand real files in the worktree."""
    from fake_executor import SCRIPT, add_template, req

    from bastet_agent_os.db import now as _now
    from bastet_agent_os.executors.base import RunResult

    ts = _now()
    seeded.write("INSERT INTO resources(id, kind, name, endpoint, config_json, "
                 "created_at, updated_at) VALUES('resimg','image','gen-image',"
                 "'https://api.example','{}',?,?)", (ts, ts))
    seeded.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, "
                 "created_at) VALUES('grtimg','resimg','project','proj1',?)", (ts,))
    seen: list[str] = []

    def capture(task):
        seen.append(task.prompt)
        return RunResult(status="succeeded", summary="ok")

    add_template(seeded, "dev", [{"name": "美術", "gate": "auto"}])
    SCRIPT.append(capture)
    orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    assert "生成資產的保存" in seen[0]
    assert "下載成 worktree 裡的實體檔案" in seen[0]
