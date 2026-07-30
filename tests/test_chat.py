"""Chat: sessions bound to real projects, file intake, and authorisation.

The chat is the human input channel, so the tests care about three things:
it cannot drift from the real org, what is said survives (per project), and
it can actually act — dispatch a job, approve a gate.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from bastet_agent_os import chat
from bastet_agent_os.config import Home
from bastet_agent_os.db import Db, now
from bastet_agent_os.server import create_app

OPENAI_REPLY = {
    "id": "chatcmpl-1", "model": "gpt-5",
    "choices": [{"message": {"role": "assistant", "content": "先確認驗收條件。"}}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 30},
}


@pytest.fixture
def client(tmp_path):
    home = Home(tmp_path / "home")
    c = TestClient(create_app(home), base_url="http://127.0.0.1")
    c.headers["Authorization"] = f"Bearer {home.api_token()}"
    c.post("/api/teams", json={"id": "team1", "name": "Team One"})
    c.post("/api/projects", json={"id": "proj1", "repo_path": str(tmp_path / "repo"),
                                  "team_id": "team1"})
    c.put("/api/projects/proj1", json={"description": "貓咪散步預約系統"})
    c.post("/api/agents", json={"id": "ag1", "name": "大貓咪",
                                "executor_type": "claude-code"})
    return c, home


@pytest.fixture
def llm(client, tmp_path, monkeypatch):
    """A pool LLM resource whose upstream is a mock transport."""
    c, home = client
    key = tmp_path / "key"
    key.write_text("sk-test")
    rid = c.post("/api/resources", json={
        "name": "chat-llm", "kind": "llm", "endpoint": "https://llm.example/v1",
        "api_flavor": "openai", "secret_ref": f"file:{key}",
        "scope_type": "project", "scope_id": "proj1",
        "config": {"default_model": "gpt-5"}}).json()["id"]

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=OPENAI_REPLY)

    real = httpx.AsyncClient

    class MockedClient(real):                     # every chat call is intercepted
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", MockedClient)
    return c, home, rid, captured


# ---- sessions are bound to the real org -------------------------------------------

def test_session_must_reference_a_real_project(client):
    c, _ = client
    resp = c.post("/api/chat/sessions", json={"scope_type": "project",
                                              "scope_id": "ghost",
                                              "responder_kind": "agent",
                                              "responder_id": "ag1"})
    assert resp.status_code == 400 and "does not exist" in resp.json()["detail"]
    assert c.post("/api/chat/sessions", json={"scope_type": "project", "scope_id": "",
                                              "responder_kind": "agent",
                                              "responder_id": "ag1"}).status_code == 400
    assert c.post("/api/chat/sessions", json={"scope_type": "project",
                                              "scope_id": "proj1",
                                              "responder_kind": "resource",
                                              "responder_id": "res_ghost"}
                  ).status_code == 400


def test_responder_dropdown_offers_agents_and_pool_llms(llm):
    c, _, rid, _ = llm
    rows = c.get("/api/chat/responders").json()
    assert {"agent", "resource"} == {r["kind"] for r in rows}
    assert any(r["id"] == "ag1" and r["label"] == "大貓咪" for r in rows)
    assert any(r["id"] == rid and r["detail"] == "gpt-5" for r in rows)


def test_sessions_are_listed_per_project(llm):
    c, _, rid, _ = llm
    a = c.post("/api/chat/sessions", json={"scope_type": "project", "scope_id": "proj1",
                                           "responder_kind": "resource",
                                           "responder_id": rid,
                                           "title": "規劃"}).json()["id"]
    c.post("/api/chat/sessions", json={"scope_type": "global",
                                       "responder_kind": "resource",
                                       "responder_id": rid})
    project_sessions = c.get("/api/chat/sessions?scope_type=project&scope_id=proj1").json()
    assert [s["id"] for s in project_sessions] == [a]
    assert len(c.get("/api/chat/sessions").json()) == 2


# ---- a turn ----------------------------------------------------------------------

def test_turn_carries_the_project_state_into_the_prompt(llm):
    c, _, rid, captured = llm
    session = c.post("/api/chat/sessions", json={
        "scope_type": "project", "scope_id": "proj1", "responder_kind": "resource",
        "responder_id": rid}).json()["id"]
    body = c.post(f"/api/chat/sessions/{session}/messages",
                  json={"content": "幫我規劃第一版"}).json()

    assert body["reply"]["content"] == "先確認驗收條件。"
    assert body["reply"]["meta"]["tokens_in"] == 120           # reported, not guessed
    assert body["reply"]["meta"]["precision"] == "reported"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]

    system = captured["body"]["messages"][0]
    assert system["role"] == "system"
    assert "proj1" in system["content"] and "貓咪散步預約系統" in system["content"]
    assert "chat-llm" in system["content"]        # the pool it may draw on
    assert captured["headers"]["authorization"] == "Bearer sk-test"
    assert captured["url"] == "https://llm.example/v1/chat/completions"


def test_history_is_replayed_so_the_thread_has_continuity(llm):
    c, _, rid, captured = llm
    session = c.post("/api/chat/sessions", json={
        "scope_type": "project", "scope_id": "proj1", "responder_kind": "resource",
        "responder_id": rid}).json()["id"]
    c.post(f"/api/chat/sessions/{session}/messages", json={"content": "第一句"})
    c.post(f"/api/chat/sessions/{session}/messages", json={"content": "第二句"})
    roles = [m["role"] for m in captured["body"]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


def test_empty_message_is_rejected(llm):
    c, _, rid, _ = llm
    session = c.post("/api/chat/sessions", json={
        "scope_type": "global", "responder_kind": "resource",
        "responder_id": rid}).json()["id"]
    assert c.post(f"/api/chat/sessions/{session}/messages",
                  json={"content": "  "}).status_code == 400


def test_resource_without_a_model_says_what_to_fix(client, tmp_path):
    c, _ = client
    key = tmp_path / "k"
    key.write_text("x")
    rid = c.post("/api/resources", json={
        "name": "no-model", "kind": "llm", "endpoint": "https://x/v1",
        "secret_ref": f"file:{key}", "scope_type": "global"}).json()["id"]
    session = c.post("/api/chat/sessions", json={
        "scope_type": "global", "responder_kind": "resource",
        "responder_id": rid}).json()["id"]
    resp = c.post(f"/api/chat/sessions/{session}/messages", json={"content": "hi"})
    assert resp.status_code == 400 and "default model" in resp.json()["detail"]
    # the user's message survives the failed reply — it is not lost
    assert [m["role"] for m in
            c.get(f"/api/chat/sessions/{session}/messages").json()["messages"]] == ["user"]


# ---- files ------------------------------------------------------------------------

def test_uploaded_text_file_is_inlined_for_the_model_and_downloadable(llm):
    c, _, rid, captured = llm
    session = c.post("/api/chat/sessions", json={
        "scope_type": "project", "scope_id": "proj1", "responder_kind": "resource",
        "responder_id": rid}).json()["id"]
    up = c.post(f"/api/chat/sessions/{session}/files",
                files={"file": ("spec.md", b"# Spec\nmust support 2 cats", "text/markdown")})
    assert up.status_code == 200 and "path" not in up.json()
    file_id = up.json()["id"]

    c.post(f"/api/chat/sessions/{session}/messages",
           json={"content": "照這份規格", "attachment_ids": [file_id]})
    sent = captured["body"]["messages"][-1]["content"]
    assert "must support 2 cats" in sent and 'name="spec.md"' in sent

    got = c.get(f"/api/chat/sessions/{session}/files/{file_id}")
    assert got.status_code == 200 and b"must support 2 cats" in got.content


def test_image_is_attached_as_a_data_url_for_openai_wire(llm):
    c, _, rid, captured = llm
    session = c.post("/api/chat/sessions", json={
        "scope_type": "project", "scope_id": "proj1", "responder_kind": "resource",
        "responder_id": rid}).json()["id"]
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    file_id = c.post(f"/api/chat/sessions/{session}/files",
                     files={"file": ("shot.png", png, "image/png")}).json()["id"]
    c.post(f"/api/chat/sessions/{session}/messages",
           json={"content": "看這張", "attachment_ids": [file_id]})
    blocks = captured["body"]["messages"][-1]["content"]
    assert isinstance(blocks, list)
    assert any(b.get("type") == "image_url"
               and b["image_url"]["url"].startswith("data:image/png;base64,")
               for b in blocks)


# ---- acting: dispatch + approve ----------------------------------------------------

def test_chat_can_dispatch_a_job_for_its_project(llm, monkeypatch):
    c, _, rid, _ = llm
    session = c.post("/api/chat/sessions", json={
        "scope_type": "project", "scope_id": "proj1", "responder_kind": "resource",
        "responder_id": rid}).json()["id"]
    c.post(f"/api/chat/sessions/{session}/messages", json={"content": "做登入頁"})

    calls = {}
    from bastet_agent_os.orchestrator import Orchestrator
    monkeypatch.setattr(Orchestrator, "dispatch",
                        lambda self, actor, req: calls.setdefault("req", req) and "job_x"
                        or "job_x")
    out = c.post(f"/api/chat/sessions/{session}/dispatch",
                 json={"agent_id": "ag1", "title": "登入頁"}).json()
    assert out["job_id"] == "job_x"
    assert calls["req"].project_id == "proj1"       # the session's real project
    assert "做登入頁" in calls["req"].prompt        # spec built from the discussion

    messages = c.get(f"/api/chat/sessions/{session}/messages").json()["messages"]
    assert messages[-1]["role"] == "system" and messages[-1]["meta"]["job_id"] == "job_x"


def test_global_session_cannot_dispatch(llm):
    c, _, rid, _ = llm
    session = c.post("/api/chat/sessions", json={
        "scope_type": "global", "responder_kind": "resource",
        "responder_id": rid}).json()["id"]
    assert c.post(f"/api/chat/sessions/{session}/dispatch",
                  json={"agent_id": "ag1"}).status_code == 400


def test_blocked_gates_surface_in_the_session(client, tmp_path):
    """The chat is where authorisation is asked for, so it must show what is
    waiting on the human."""
    c, home = client
    db = Db(home.db_path)
    try:
        db.write("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, stage, "
                 "status, created_at, updated_at) VALUES('job1','proj1','[]',"
                 "'需要批准','review','blocked',?,?)", (now(), now()))
    finally:
        db.close()
    session = c.post("/api/chat/sessions", json={
        "scope_type": "project", "scope_id": "proj1", "responder_kind": "agent",
        "responder_id": "ag1"}).json()["id"]
    pending = c.get(f"/api/chat/sessions/{session}/messages").json()["pending_approvals"]
    assert [p["id"] for p in pending] == ["job1"]


# ---- telegram as a second channel --------------------------------------------------

def test_channel_responder_is_configurable_and_validated(client, llm):
    c, _, rid, _ = llm
    channel = c.post("/api/channels", json={"kind": "telegram", "name": "值班",
                                            "secret_ref": "env:TG"}).json()
    assert c.put(f"/api/channels/{channel['id']}/chat",
                 json={"responder_kind": "resource", "responder_id": "res_ghost"}
                 ).status_code == 400
    out = c.put(f"/api/channels/{channel['id']}/chat",
                json={"responder_kind": "resource", "responder_id": rid,
                      "project_id": "proj1"}).json()
    assert out["responder"] == {"kind": "resource", "id": rid}
    row = next(r for r in c.get("/api/channels").json() if r["id"] == channel["id"])
    assert row["project_id"] == "proj1" and row["responder"]["id"] == rid

    # clearing it turns the channel back into notify-only
    c.put(f"/api/channels/{channel['id']}/chat", json={})
    row = next(r for r in c.get("/api/channels").json() if r["id"] == channel["id"])
    assert row["responder"] is None and row["project_id"] == ""


def test_channel_session_is_reused_per_external_conversation(client, llm):
    c, home, rid, _ = llm
    db = Db(home.db_path)
    try:
        first = chat.find_or_create_channel_session(
            db, channel="telegram", external_id="chn1:42", scope_type="project",
            scope_id="proj1", responder_kind="resource", responder_id=rid,
            title="telegram · 貓")
        again = chat.find_or_create_channel_session(
            db, channel="telegram", external_id="chn1:42", scope_type="project",
            scope_id="proj1", responder_kind="agent", responder_id="ag1",
            title="telegram · 貓")
        assert first == again                       # same thread, not a new one
        row = chat.get_session(db, first)
        assert (row["responder_kind"], row["responder_id"]) == ("agent", "ag1")
        other = chat.find_or_create_channel_session(
            db, channel="telegram", external_id="chn1:99", scope_type="project",
            scope_id="proj1", responder_kind="agent", responder_id="ag1", title="x")
        assert other != first
    finally:
        db.close()


async def test_failed_agent_turn_reports_cleanly(client, tmp_path, monkeypatch):
    """Latent crash found while wiring decomposition: RunResult has no `.error`,
    so the failure path itself raised AttributeError instead of reporting."""
    from bastet_agent_os import chat as chat_mod
    from bastet_agent_os.db import Db
    from bastet_agent_os.executors.base import RunResult, register_builtin

    class DeadHandle:
        def state(self):
            return {}

    @register_builtin
    class DeadExecutor:
        kind = "dead-executor"

        async def start(self, task):
            return DeadHandle()

        async def stream(self, handle):
            return
            yield

        async def result(self, handle):
            return RunResult(status="failed", summary="")

        async def respond(self, handle, request_id, reply):
            pass

        async def cancel(self, handle):
            pass

    c, home = client
    c.post("/api/agents", json={"id": "dead", "name": "Dead",
                               "executor_type": "dead-executor"})
    db = Db(home.db_path)
    try:
        session = chat_mod.create_session(db, scope_type="global", scope_id="",
                                          responder_kind="agent", responder_id="dead")
        chat_mod.add_message(db, session, role="user", content="hi")
        with pytest.raises(chat_mod.ChatError, match="failed"):
            await chat_mod.reply(db, home.root, session)
    finally:
        db.close()
