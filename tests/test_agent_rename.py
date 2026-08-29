"""Agent ids are editable identities, without severing operational history."""

from fastapi.testclient import TestClient

from bastet_agent_os.config import Home
from bastet_agent_os.db import Db, now
from bastet_agent_os.server import create_app


def _client(tmp_path):
    home = Home(tmp_path / "home")
    client = TestClient(create_app(home), base_url="http://127.0.0.1")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"
    return client, home


def _seed_graph(home: Home, run_status: str = "succeeded") -> None:
    db = Db(home.db_path)
    ts = now()
    db.write_many([
        ("INSERT INTO projects(id,team_id,repo_path,created_at,updated_at) "
         "VALUES('p1','t1','/tmp/repo',?,?)", (ts, ts)),
        ("INSERT INTO agents(id,amos_agent_id,name,executor_type,created_at,updated_at) "
         "VALUES('old-agent','memory-identity','Old','pi',?,?)", (ts, ts)),
        ("INSERT INTO resources(id,kind,name,created_at,updated_at) "
         "VALUES('r1','llm','model',?,?)", (ts, ts)),
        ("INSERT INTO grants(id,resource_id,scope_type,scope_id,created_at) "
         "VALUES('g1','r1','agent','old-agent',?)", (ts,)),
        ("INSERT INTO project_agent_roles(project_id,agent_id,role) "
         "VALUES('p1','old-agent','engineer')", ()),
        ("INSERT INTO jobs(id,project_id,stages_snapshot_json,title,stage,status,"
         "default_agent_id,agent_override,created_at,updated_at) "
         "VALUES('j1','p1','[]','job','work','blocked','old-agent','old-agent',?,?)",
         (ts, ts)),
        ("INSERT INTO runs(id,job_id,stage,agent_id,executor_type,status) "
         "VALUES('run1','j1','work','old-agent','pi',?)", (run_status,)),
        ("INSERT INTO project_rooms(id,project_id,title,created_at,updated_at) "
         "VALUES('room1','p1','Room',?,?)", (ts, ts)),
        ("INSERT INTO room_messages(id,room_id,author_type,author_id,content,at) "
         "VALUES('msg1','room1','agent','old-agent','handoff',?)", (ts,)),
        ("INSERT INTO chat_sessions(id,scope_type,scope_id,title,responder_kind,"
         "responder_id,created_at,updated_at) "
         "VALUES('chat1','project','p1','Chat','agent','old-agent',?,?)", (ts, ts)),
        ("INSERT INTO stage_handoffs(id,project_id,job_id,run_id,from_stage,agent_id,"
         "delivered_to_agent_id,acknowledged_by,at) "
         "VALUES('h1','p1','j1','run1','work','old-agent','old-agent','old-agent',?)",
         (ts,)),
        ("INSERT INTO handoff_receipts(id,handoff_id,job_id,stage,agent_id,delivered_at) "
         "VALUES('hr1','h1','j1','next','old-agent',?)", (ts,)),
    ])
    db.close()


def test_agent_id_rename_moves_every_reference_and_keeps_memory_identity(tmp_path):
    client, home = _client(tmp_path)
    _seed_graph(home)

    out = client.put("/api/agents/old-agent", json={"id": "new-agent"})

    assert out.status_code == 200, out.text
    db = Db(home.db_path)
    assert dict(db.one("SELECT id,amos_agent_id FROM agents")) == {
        "id": "new-agent", "amos_agent_id": "memory-identity"}
    checks = [
        ("project_agent_roles", "agent_id"), ("runs", "agent_id"),
        ("stage_handoffs", "agent_id"), ("handoff_receipts", "agent_id"),
    ]
    for table, column in checks:
        assert db.one(f"SELECT {column} AS value FROM {table}")["value"] == "new-agent"
    job = db.one("SELECT default_agent_id,agent_override FROM jobs")
    assert (job["default_agent_id"], job["agent_override"]) == ("new-agent", "new-agent")
    assert db.one("SELECT scope_id FROM grants WHERE id='g1'")["scope_id"] == "new-agent"
    assert db.one("SELECT responder_id FROM chat_sessions")["responder_id"] == "new-agent"
    assert db.one("SELECT author_id FROM room_messages")["author_id"] == "new-agent"
    handoff = db.one("SELECT delivered_to_agent_id,acknowledged_by FROM stage_handoffs")
    assert tuple(handoff) == ("new-agent", "new-agent")
    db.close()


def test_agent_id_cannot_change_during_an_active_run(tmp_path):
    client, home = _client(tmp_path)
    _seed_graph(home, run_status="running")

    out = client.put("/api/agents/old-agent", json={"id": "new-agent"})

    assert out.status_code == 409
    assert "active run" in out.json()["detail"]
