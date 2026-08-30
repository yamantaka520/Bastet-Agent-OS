"""Evidence dimensions are visible as durable job acceptance state."""

import json

from fastapi.testclient import TestClient

from bastet_agent_os.config import Home
from bastet_agent_os.db import Db, now
from bastet_agent_os.server import create_app


def test_job_detail_exposes_declared_evidence_with_gate_and_commit(tmp_path):
    home = Home(tmp_path / "home")
    client = TestClient(create_app(home), base_url="http://127.0.0.1")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"
    assert client.post("/api/teams", json={"id": "team1", "name": "T"}).status_code == 200
    assert client.post("/api/projects", json={
        "id": "p1", "repo_path": "/tmp/repo", "team_id": "team1",
    }).status_code == 200

    db = Db(home.db_path)
    stamp = now()
    stages = [{"name": "security", "needs": [], "gate": "agent-review",
               "read_only": True, "evidence": ["security", "architecture"]}]
    db.write_many([
        ("INSERT INTO agents(id,amos_agent_id,name,executor_type,created_at,updated_at) "
         "VALUES('a1','a1','Reviewer','fake',?,?)", (stamp, stamp)),
        ("INSERT INTO jobs(id,project_id,stages_snapshot_json,title,stage,status,"
         "created_at,updated_at) VALUES('j1','p1',?,'Review','security','done',?,?)",
         (json.dumps(stages), stamp, stamp)),
        ("INSERT INTO runs(id,job_id,stage,agent_id,executor_type,status,started_at,"
         "finished_at) VALUES('r1','j1','security','a1','fake','succeeded',?,?)",
         (stamp, stamp)),
        ("INSERT INTO gate_results(id,run_id,gate_type,verdict,reviewer_kind,"
         "reviewer_id,at) VALUES('g1','r1','agent-review','passed','agent','a1',?)",
         (stamp,)),
        ("INSERT INTO job_stage_nodes(job_id,stage,status,needs_json,workspace,"
         "head_commit,updated_at) VALUES('j1','security','passed','[]','shared',"
         "'abc123',?)", (stamp,)),
    ])
    db.close()

    matrix = client.get("/api/jobs/j1").json()["evidence_matrix"]
    assert matrix == [
        {"kind": "security", "stage": "security", "gate": "agent-review",
         "verdict": "passed", "run_id": "r1", "head_commit": "abc123"},
        {"kind": "architecture", "stage": "security", "gate": "agent-review",
         "verdict": "passed", "run_id": "r1", "head_commit": "abc123"},
    ]
