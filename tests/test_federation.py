"""M5 org view: AMOS org surfaced, binding federated projects locally."""

import pytest
from fastapi.testclient import TestClient

from bastet_agent_os.config import Home
from bastet_agent_os.server import create_app


@pytest.fixture
def fed(tmp_path, monkeypatch):
    """App + an isolated AMOS store simulating a federated org."""
    monkeypatch.setenv("AGENT_MEMORY_HOME", str(tmp_path / "amos"))
    from agent_memory_os.client import MemoryClient

    amos = MemoryClient()
    amos.create_team("team-remote", name="Remote Team")
    amos.register_agent("remote-agent")
    amos.add_team_member("team-remote", "remote-agent")
    amos.create_project("proj-remote", "team-remote")
    amos.add_project_member("proj-remote", "remote-agent")

    home = Home(tmp_path / "bastet-home")
    client = TestClient(create_app(home), base_url="http://127.0.0.1")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"
    return client, amos


def test_org_view_shows_unbound_federated_project(fed):
    client, _ = fed
    org = client.get("/api/org").json()
    assert org["amos"] is True
    team = next(t for t in org["teams"] if t["id"] == "team-remote")
    assert team["members"] == ["remote-agent"]
    project = next(p for p in team["projects"] if p["id"] == "proj-remote")
    assert project["bound"] is False and project["members"] == ["remote-agent"]


def test_bind_federated_project(fed):
    client, _ = fed
    resp = client.post("/api/org/bind",
                       json={"project_id": "proj-remote", "repo_path": "/tmp/repo"})
    assert resp.status_code == 200
    assert resp.json()["team_id"] == "team-remote"  # team came from AMOS, not the caller

    org = client.get("/api/org").json()
    team = next(t for t in org["teams"] if t["id"] == "team-remote")
    assert next(p for p in team["projects"] if p["id"] == "proj-remote")["bound"] is True
    projects = client.get("/api/projects").json()
    assert any(p["id"] == "proj-remote" and p["repo_path"] == "/tmp/repo"
               for p in projects)

    # double bind conflicts; unknown project 404s
    assert client.post("/api/org/bind", json={"project_id": "proj-remote",
                                              "repo_path": "/x"}).status_code == 409
    assert client.post("/api/org/bind", json={"project_id": "ghost",
                                              "repo_path": "/x"}).status_code == 404


def test_local_only_projects_are_listed(fed):
    client, amos = fed
    client.post("/api/org/bind", json={"project_id": "proj-remote",
                                       "repo_path": "/tmp/repo"})
    amos.delete_project("proj-remote")  # deletion propagated from another node
    org = client.get("/api/org").json()
    assert "proj-remote" in org["local_only"]  # local binding/history survives


def test_role_assignment_syncs_amos_membership(fed):
    """Assigning a role must make the agent a real AMOS project member —
    that membership is what gates project-scoped memory access."""
    client, amos = fed
    client.post("/api/org/bind", json={"project_id": "proj-remote",
                                       "repo_path": "/tmp/repo"})
    client.post("/api/agents", json={"id": "ag-worker", "name": "Worker",
                                     "executor_type": "claude-code"})

    resp = client.post("/api/roles", json={"project_id": "proj-remote",
                                           "agent_id": "ag-worker",
                                           "role": "engineer"})
    assert resp.status_code == 200 and resp.json()["amos_member"] is True
    project = next(p for p in amos.list_projects() if p["id"] == "proj-remote")
    assert "ag-worker" in project["members"]        # org view now counts it

    # a second role keeps membership; dropping the last one revokes it
    client.post("/api/roles", json={"project_id": "proj-remote",
                                    "agent_id": "ag-worker", "role": "reviewer"})
    client.delete("/api/roles?project_id=proj-remote&agent_id=ag-worker&role=engineer")
    project = next(p for p in amos.list_projects() if p["id"] == "proj-remote")
    assert "ag-worker" in project["members"]
    client.delete("/api/roles?project_id=proj-remote&agent_id=ag-worker&role=reviewer")
    project = next(p for p in amos.list_projects() if p["id"] == "proj-remote")
    assert "ag-worker" not in project["members"]
