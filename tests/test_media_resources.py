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
