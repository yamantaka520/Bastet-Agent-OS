"""Multi-user auth (M3): roles, token hygiene, attribution."""

import pytest
from fastapi.testclient import TestClient

from bastet_agent_os.config import Home
from bastet_agent_os.server import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # keep AMOS org sync away from the real ~/.agent-memory during tests
    monkeypatch.setenv("AGENT_MEMORY_HOME", str(tmp_path / "amos"))
    home = Home(tmp_path / "bastet-home")
    app = create_app(home)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.admin = {"Authorization": f"Bearer {home.api_token()}"}
    return c


def make_user(client, name, role):
    resp = client.post("/api/users", headers=client.admin, json={"name": name, "role": role})
    assert resp.status_code == 200
    body = resp.json()
    return body["id"], {"Authorization": f"Bearer {body['token']}"}


def test_bootstrap_token_is_admin_root(client):
    me = client.get("/api/me", headers=client.admin).json()
    assert me == {"user_id": "root", "name": "root", "role": "admin"}


def test_role_hierarchy_enforced(client):
    _, viewer = make_user(client, "vera", "viewer")
    _, operator = make_user(client, "oscar", "operator")

    # viewer: read yes, write no
    assert client.get("/api/jobs", headers=viewer).status_code == 200
    assert client.post("/api/dispatch", headers=viewer,
                       json={"project_id": "x", "prompt": "p", "agent_id": "a"}
                       ).status_code == 403
    assert client.post("/api/users", headers=viewer,
                       json={"name": "evil", "role": "admin"}).status_code == 403

    # operator: dispatch passes the role gate (400 = validation, not authz)
    assert client.post("/api/dispatch", headers=operator,
                       json={"project_id": "x", "prompt": "p", "agent_id": "a"}
                       ).status_code == 400
    # ...but structure/money stays admin-only
    assert client.post("/api/resources", headers=operator,
                       json={"name": "r"}).status_code == 403
    assert client.get("/api/users", headers=operator).status_code == 403


def test_disabled_user_is_rejected_and_reenabled(client):
    user_id, headers = make_user(client, "temp", "viewer")
    assert client.get("/api/me", headers=headers).status_code == 200
    client.post(f"/api/users/{user_id}/enabled", headers=client.admin,
                json={"enabled": False})
    assert client.get("/api/me", headers=headers).status_code == 401
    client.post(f"/api/users/{user_id}/enabled", headers=client.admin,
                json={"enabled": True})
    assert client.get("/api/me", headers=headers).status_code == 200


def test_user_tokens_are_hash_only_and_never_listed(client):
    _, headers = make_user(client, "hana", "viewer")
    token = headers["Authorization"].removeprefix("Bearer ")
    listing = client.get("/api/users", headers=client.admin).json()
    assert token not in str(listing)
    assert all("token_hash" not in row for row in listing)


def test_invalid_role_rejected(client):
    resp = client.post("/api/users", headers=client.admin,
                       json={"name": "x", "role": "superuser"})
    assert resp.status_code == 400


def test_audit_attributes_the_acting_user(client):
    _, operator = make_user(client, "oscar", "operator")
    client.post("/api/templates", headers=operator,
                json={"name": "t1", "stages": [{"name": "s", "gate": "auto"}]})
    audit = client.get("/api/audit", headers=client.admin).json()["rows"]
    template_rows = [r for r in audit if r["action"] == "template.upsert"]
    assert template_rows and template_rows[0]["actor"].startswith("user:usr_")


def test_audit_search_narrows_by_category_time_and_keyword(client):
    """An audit log you cannot search is one nobody reads."""
    client.post("/api/teams", json={"id": "t-search", "name": "S"},
                headers=client.admin)
    body = client.get("/api/audit?action=team&limit=50", headers=client.admin).json()
    assert body["rows"] and all(r["action"].startswith("team")
                                for r in body["rows"])
    assert "team" in body["categories"]          # facets drive the filter UI

    hit = client.get("/api/audit?q=t-search", headers=client.admin).json()
    assert any("t-search" in (r["target_id"] or "") + (r["detail_json"] or "")
               for r in hit["rows"])
    none = client.get("/api/audit?q=zzz-nothing-here", headers=client.admin).json()
    assert none["rows"] == [] and none["count"] == 0

    future = client.get("/api/audit?since=2999-01-01", headers=client.admin).json()
    assert future["rows"] == []
    past = client.get("/api/audit?until=1999-01-01", headers=client.admin).json()
    assert past["rows"] == []
    capped = client.get("/api/audit?limit=99999", headers=client.admin).json()
    assert len(capped["rows"]) <= 1000            # a filter must not become a DoS
