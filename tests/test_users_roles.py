"""The three roles must mean what the dropdown says, and tokens must be
manageable: copy once, disable, rotate (old one dead), delete.

The capability table shown in the UI is pinned here against real endpoints —
otherwise it is marketing text that drifts from what the code enforces.
"""

import pytest
from fastapi.testclient import TestClient

from bastet_agent_os import users as users_mod
from bastet_agent_os.config import Home
from bastet_agent_os.server import create_app


@pytest.fixture
def admin(tmp_path):
    home = Home(tmp_path / "home")
    app = create_app(home)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.headers["Authorization"] = f"Bearer {home.api_token()}"
    c.post("/api/teams", json={"id": "team1", "name": "T"})
    c.post("/api/projects", json={"id": "proj1", "repo_path": "/tmp/r",
                                  "team_id": "team1"})
    c.post("/api/agents", json={"id": "ag1", "name": "A",
                                "executor_type": "claude-code"})
    return c, app


def as_user(app, token: str) -> TestClient:
    c = TestClient(app, base_url="http://127.0.0.1")
    c.headers["Authorization"] = f"Bearer {token}"
    return c


def make(c, name: str, role: str) -> tuple[str, str]:
    body = c.post("/api/users", json={"name": name, "role": role}).json()
    return body["id"], body["token"]


# ---- the roles are real ------------------------------------------------------------

def test_role_catalog_is_offered_to_the_dropdown(admin):
    c, _ = admin
    rows = c.get("/api/user-roles").json()
    assert [r["id"] for r in rows] == ["viewer", "operator", "admin"]
    assert rows[0]["rank"] < rows[1]["rank"] < rows[2]["rank"]
    assert "dispatch" in rows[0]["cannot"] and "dispatch" in rows[1]["can"]


def test_viewer_can_read_but_not_act(admin):
    c, app = admin
    _, token = make(c, "Vera", "viewer")
    v = as_user(app, token)
    assert v.get("/api/projects").status_code == 200
    assert v.get("/api/jobs?project_id=proj1").status_code == 200
    assert v.post("/api/dispatch", json={"project_id": "proj1", "prompt": "x",
                                         "agent_id": "ag1"}).status_code == 403
    assert v.post("/api/chat/sessions", json={"scope_type": "global",
                                              "responder_kind": "agent",
                                              "responder_id": "ag1"}).status_code == 403
    assert v.get("/api/users").status_code == 403


def test_operator_can_run_projects_but_not_manage_the_pool(admin):
    c, app = admin
    _, token = make(c, "Otto", "operator")
    o = as_user(app, token)
    assert o.post("/api/chat/sessions", json={"scope_type": "project",
                                              "scope_id": "proj1",
                                              "responder_kind": "agent",
                                              "responder_id": "ag1"}).status_code == 200
    # lifecycle is an operator power (409 = wrong state, not forbidden)
    assert o.post("/api/projects/proj1/lifecycle/pause").status_code == 409
    assert o.post("/api/resources", json={"name": "x", "kind": "api",
                                          "endpoint": "https://a",
                                          "secret_ref": "env:X"}).status_code == 403
    assert o.post("/api/secrets", json={"name": "s", "value": "v",
                                        "scope_type": "global"}).status_code == 403
    assert o.get("/api/users").status_code == 403


def test_admin_can_manage_everything(admin):
    c, app = admin
    _, token = make(c, "Ada", "admin")
    a = as_user(app, token)
    assert a.get("/api/users").status_code == 200
    assert a.post("/api/resources", json={"name": "y", "kind": "api",
                                          "endpoint": "https://a",
                                          "secret_ref": "env:X"}).status_code == 200


# ---- token management --------------------------------------------------------------

def test_token_is_shown_once_at_creation(admin):
    c, _ = admin
    body = c.post("/api/users", json={"name": "Once", "role": "viewer"}).json()
    assert body["token"].startswith("but_")            # copyable, in full
    listed = c.get("/api/users").json()
    assert all("token" not in row and "token_hash" not in row for row in listed)


def test_rotate_kills_the_old_token_immediately(admin):
    c, app = admin
    user_id, old = make(c, "Rota", "operator")
    assert as_user(app, old).get("/api/me").status_code == 200

    fresh = c.post(f"/api/users/{user_id}/token").json()
    assert fresh["token"] != old
    assert as_user(app, old).get("/api/me").status_code == 401      # revoked
    assert as_user(app, fresh["token"]).get("/api/me").status_code == 200


def test_disable_blocks_the_token_and_enable_restores_it(admin):
    c, app = admin
    user_id, token = make(c, "Dis", "operator")
    c.post(f"/api/users/{user_id}/enabled", json={"enabled": False})
    assert as_user(app, token).get("/api/me").status_code == 401
    c.post(f"/api/users/{user_id}/enabled", json={"enabled": True})
    assert as_user(app, token).get("/api/me").status_code == 200


def test_role_change_takes_effect_on_the_existing_token(admin):
    c, app = admin
    user_id, token = make(c, "Prom", "viewer")
    u = as_user(app, token)
    assert u.post("/api/chat/sessions", json={"scope_type": "global",
                                              "responder_kind": "agent",
                                              "responder_id": "ag1"}).status_code == 403
    assert c.put(f"/api/users/{user_id}", json={"role": "operator"}).json()["role"] \
        == "operator"
    assert u.post("/api/chat/sessions", json={"scope_type": "global",
                                              "responder_kind": "agent",
                                              "responder_id": "ag1"}).status_code == 200
    assert c.put(f"/api/users/{user_id}", json={"role": "wizard"}).status_code == 400


def test_delete_removes_the_user_and_their_access(admin):
    c, app = admin
    user_id, token = make(c, "Gone", "operator")
    assert c.delete(f"/api/users/{user_id}").status_code == 200
    assert as_user(app, token).get("/api/me").status_code == 401
    assert c.delete(f"/api/users/{user_id}").status_code == 404


def test_capability_table_matches_the_role_ranks():
    ranks = {row["id"]: row["rank"] for row in users_mod.capabilities()}
    assert ranks == users_mod.ROLES
