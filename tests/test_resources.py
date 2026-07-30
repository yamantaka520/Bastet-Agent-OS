"""Resource pool: classified kinds, scoped visibility, installers, run access.

The point of the pool is that a granted resource is *callable* by the agents
running a project. These tests pin the whole chain: catalog → create with a
scope → credential picked from the saved-credentials pool → what a run
actually receives (env vars, MCP config, prompt manifest).
"""

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from bastet_agent_os import resource_access, resource_install, resource_kinds
from bastet_agent_os.config import Home
from bastet_agent_os.db import Db, now
from bastet_agent_os.server import create_app


@pytest.fixture
def client(tmp_path):
    home = Home(tmp_path / "home")
    c = TestClient(create_app(home), base_url="http://127.0.0.1")
    c.headers["Authorization"] = f"Bearer {home.api_token()}"
    c.post("/api/teams", json={"id": "team1", "name": "Team One"})
    c.post("/api/projects", json={"id": "proj1", "repo_path": "/tmp/repo",
                                  "team_id": "team1"})
    return c


# ---- catalog ---------------------------------------------------------------------

def test_catalog_covers_the_asked_for_categories(client):
    cat = client.get("/api/resource-kinds").json()
    kinds = {k["id"]: k for k in cat["kinds"]}
    assert {"llm", "mcp", "api", "skill", "git"} <= set(kinds)
    assert kinds["skill"]["auth"] == "none"          # skills need no credential
    assert "secret" in kinds["llm"]["fields"]
    assert cat["enums"]["git_provider"] == ["github", "gitlab", "custom"]
    assert {k["group"] for k in cat["kinds"]} <= set(cat["groups"])


def test_validate_reports_what_is_missing():
    assert resource_kinds.validate("llm", "https://x", None, {}) == ["credential-required"]
    assert resource_kinds.validate("mcp", None, None,
                                   {"mcp_transport": "stdio"}) == ["mcp-command-missing"]
    assert resource_kinds.validate("mcp", None, None, {"mcp_transport": "http"}) \
        == ["mcp-url-missing"]
    assert resource_kinds.validate("git", None, None, {"git_provider": "github"}) == []
    assert resource_kinds.validate("skill", None, None,
                                   {"skill_source": "./skills/pptx"}) == []


def test_env_names_are_stable_and_shell_safe():
    assert resource_kinds.env_prefix("Brave Search (prod)") == "BASTET_RES_BRAVE_SEARCH_PROD"


# ---- create with a visibility scope ----------------------------------------------

def test_create_with_scope_creates_the_grant(client):
    rid = client.post("/api/resources", json={
        "name": "team-github", "kind": "git", "scope_type": "team", "scope_id": "team1",
        "config": {"git_provider": "github"}}).json()["id"]
    row = next(r for r in client.get("/api/resources").json() if r["id"] == rid)
    assert row["scopes"] == [{"grant_id": row["scopes"][0]["grant_id"],
                              "scope_type": "team", "scope_id": "team1"}]
    assert row["problems"] == []
    assert client.post("/api/resources", json={"name": "bad", "kind": "git",
                                               "scope_type": "team"}).status_code == 400
    assert client.post("/api/resources", json={"name": "bad2",
                                               "kind": "nope"}).status_code == 400


def test_scopes_can_be_added_and_dropped(client):
    rid = client.post("/api/resources", json={"name": "pool-api", "kind": "api",
                                              "endpoint": "https://api.example",
                                              "secret_ref": "env:X"}).json()["id"]
    gid = client.post(f"/api/resources/{rid}/scopes",
                      json={"scope_type": "global"}).json()["id"]
    assert client.post(f"/api/resources/{rid}/scopes",
                       json={"scope_type": "global"}).status_code == 409
    assert client.delete(f"/api/resources/{rid}/scopes/{gid}").status_code == 200
    assert client.get("/api/resources").json()[0]["scopes"] == []


def test_credential_comes_from_the_saved_pool_not_a_retyped_key(client, tmp_path):
    """The API-key field is a picker over /api/secrets: the resource stores a
    pointer, so rotating the credential updates every resource using it."""
    sec = client.post("/api/secrets", json={"name": "openai key", "value": "sk-live-1",
                                            "scope_type": "global"}).json()
    rid = client.post("/api/resources", json={
        "name": "openai", "kind": "llm", "endpoint": "https://api.openai.com/v1",
        "api_flavor": "openai", "secret_ref": f"secret:{sec['id']}",
        "scope_type": "global"}).json()["id"]
    row = next(r for r in client.get("/api/resources").json() if r["id"] == rid)
    assert row["credential_name"] == "openai key"     # shown by name in the UI
    assert row["secret_ref"] == "secret:…"            # value never echoed

    from bastet_agent_os import secrets_store
    db = Db(tmp_path / "home" / "bastet.db")
    try:
        concrete = secrets_store.expand(db, f"secret:{sec['id']}")
        assert secrets_store.resolve(concrete) == "sk-live-1"
        with pytest.raises(secrets_store.SecretError):
            secrets_store.expand(db, "secret:sec_missing")
    finally:
        db.close()


def test_update_and_delete_resource(client):
    rid = client.post("/api/resources", json={"name": "old", "kind": "api",
                                              "endpoint": "https://a",
                                              "secret_ref": "env:X"}).json()["id"]
    row = client.put(f"/api/resources/{rid}",
                     json={"name": "new", "endpoint": "https://b"}).json()
    assert (row["name"], row["endpoint"]) == ("new", "https://b")
    assert client.put(f"/api/resources/{rid}",
                      json={"config": {"api_key": "leak"}}).status_code == 400
    assert client.delete(f"/api/resources/{rid}").status_code == 200
    assert client.get("/api/resources").json() == []


# ---- MCP install -----------------------------------------------------------------

def test_mcp_install_runs_the_vendor_command_and_keeps_the_log(client):
    rid = client.post("/api/resources", json={
        "name": "brave", "kind": "mcp", "scope_type": "global",
        "config": {"mcp_transport": "stdio",
                   "mcp_command": "npx -y @modelcontextprotocol/server-brave-search",
                   "install_command": "echo pulling brave server"}}).json()["id"]
    state = client.post(f"/api/resources/{rid}/install").json()
    assert state["status"] == "installed" and "pulling brave" in state["log"]
    assert state["exit_code"] == 0


def test_failed_install_keeps_the_output_for_debugging_then_retries(client):
    rid = client.post("/api/resources", json={
        "name": "broken", "kind": "mcp",
        "config": {"mcp_command": "run-me", "install_command":
                   "echo 'E404 not found' >&2; exit 3"}}).json()["id"]
    state = client.post(f"/api/resources/{rid}/install").json()
    assert state["status"] == "failed" and state["exit_code"] == 3
    assert "E404" in state["log"]                     # operator can see why

    # fix the command in place and run it again — no delete/recreate dance
    client.put(f"/api/resources/{rid}",
               json={"config": {"install_command": "echo fixed"}})
    assert client.post(f"/api/resources/{rid}/install").json()["status"] == "installed"


def test_install_without_a_command_is_a_client_error(client):
    rid = client.post("/api/resources", json={"name": "plain", "kind": "api",
                                              "endpoint": "https://a",
                                              "secret_ref": "env:X"}).json()["id"]
    assert client.post(f"/api/resources/{rid}/install").status_code == 400


def test_install_never_happens_implicitly(client, tmp_path):
    marker = tmp_path / "installed"
    client.post("/api/resources", json={
        "name": "sneaky", "kind": "mcp", "scope_type": "global",
        "config": {"mcp_command": "x", "install_command": f"touch {marker}"}})
    client.get("/api/resources")
    assert not marker.exists(), "creating/listing a resource must not run shell"


def test_install_state_starts_absent():
    assert resource_install.state_of({})["status"] == "absent"


# ---- project attach / detach ------------------------------------------------------

def test_project_card_can_add_and_remove_resources(client):
    rid = client.post("/api/resources", json={"name": "proj-api", "kind": "api",
                                              "endpoint": "https://a",
                                              "secret_ref": "env:X"}).json()["id"]
    assert client.get("/api/projects/proj1/overview").json()["resources"] == []

    client.post("/api/projects/proj1/resources", json={"resource_id": rid})
    row = client.get("/api/projects/proj1/overview").json()["resources"][0]
    assert (row["id"], row["scope_type"], row["kind"]) == (rid, "project", "api")
    assert client.post("/api/projects/proj1/resources",
                       json={"resource_id": rid}).status_code == 409

    assert client.delete(f"/api/projects/proj1/resources/{rid}").status_code == 200
    assert client.get("/api/projects/proj1/overview").json()["resources"] == []


def test_inherited_access_shows_up_but_cannot_be_detached_from_the_project(client):
    rid = client.post("/api/resources", json={"name": "team-wide", "kind": "api",
                                              "endpoint": "https://a",
                                              "secret_ref": "env:X",
                                              "scope_type": "team",
                                              "scope_id": "team1"}).json()["id"]
    row = client.get("/api/projects/proj1/overview").json()["resources"][0]
    assert row["scope_type"] == "team"                 # visible through the team
    resp = client.delete(f"/api/projects/proj1/resources/{rid}")
    assert resp.status_code == 404 and "team" in resp.json()["detail"]


# ---- what a run actually gets -----------------------------------------------------

@pytest.fixture
def pool(tmp_path):
    """A pool with one resource per scope, plus a credential and an MCP server."""
    db = Db(tmp_path / "pool.db")
    ts = now()
    token = tmp_path / "gh-token"
    token.write_text("ghp_live")
    rows = [
        ("res-secret", "secret", "deploy", None, f"file:{token}",
         '{"env_name": "DEPLOY_TOKEN"}', "global", "*"),
        ("res-llm", "llm", "OpenAI Main", "https://api.openai.com/v1", "secret:res-secret",
         '{"default_model": "gpt-5"}', "project", "proj1"),
        ("res-git", "git", "GitHub", "https://github.com", f"file:{token}",
         '{"git_provider": "github"}', "team", "team1"),
        ("res-skill", "skill", "PPTX", None, None,
         '{"skill_source": "./skills/pptx"}', "global", "*"),
        ("res-mcp", "mcp", "Brave", None, f"file:{token}",
         json.dumps({"mcp_transport": "stdio",
                     "mcp_command": "npx -y @scope/brave --port 1",
                     "mcp_secret_env": "BRAVE_API_KEY"}), "project", "proj1"),
        ("res-other", "api", "Other Project Only", "https://nope", "env:X", "{}",
         "project", "other"),
    ]
    for rid, kind, name, endpoint, ref, config, scope_type, scope_id in rows:
        db.write("INSERT INTO resources(id, kind, name, endpoint, secret_ref, "
                 "config_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                 (rid, kind, name, endpoint, ref, config, ts, ts))
        db.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, created_at) "
                 "VALUES(?,?,?,?,?)", (f"g-{rid}", rid, scope_type, scope_id, ts))
    yield db, tmp_path
    db.close()


def test_visibility_follows_the_grant_scope(pool):
    db, _ = pool
    names = {r["name"] for r in resource_access.visible(db, "proj1", "team1")}
    assert names == {"OpenAI Main", "GitHub", "PPTX", "Brave"}   # not "Other Project Only"
    assert "deploy" not in names          # credentials ride the secret path, not this one


def test_run_access_gives_the_agent_env_mcp_and_a_manifest(pool):
    db, tmp = pool
    access = resource_access.build(db, tmp, "proj1", "team1", "run-1",
                                   audit_actor="run:run-1")

    # endpoints, models and credentials as env vars
    assert access.env["BASTET_RES_OPENAI_MAIN_URL"] == "https://api.openai.com/v1"
    assert access.env["BASTET_RES_OPENAI_MAIN_KEY"] == "ghp_live"   # via secret: pointer
    assert access.env["BASTET_RES_OPENAI_MAIN_MODEL"] == "gpt-5"
    assert access.env["BASTET_RES_GITHUB_TOKEN"] == "ghp_live"      # git → _TOKEN
    assert access.env["BASTET_RES_PPTX_SOURCE"] == "./skills/pptx"

    # MCP servers land in a config file the executor can point at
    config = json.loads(open(access.mcp_config_path).read())
    server = config["mcpServers"]["brave"]
    assert server["command"] == "npx"
    assert server["args"] == ["-y", "@scope/brave", "--port", "1"]
    assert server["env"] == {"BRAVE_API_KEY": "ghp_live"}
    assert access.env[resource_access.MCP_ENV] == access.mcp_config_path

    # the file holds resolved secrets: 0600, outside the worktree, and removed
    mode = stat.S_IMODE(os.stat(access.mcp_config_path).st_mode)
    assert mode == 0o600
    resource_access.cleanup(tmp, "run-1")
    assert not os.path.exists(access.mcp_config_path)

    # and the agent is told what exists (names only, never values)
    manifest = json.loads(access.env[resource_access.MANIFEST_ENV])
    assert {m["name"] for m in manifest} == {"OpenAI Main", "GitHub", "PPTX", "Brave"}
    assert "ghp_live" not in access.notes and "OpenAI Main" in access.notes
    assert db.query("SELECT * FROM audit_log WHERE action='resource.exposed'")


def test_unusable_resources_are_not_advertised(pool):
    """A resource with nothing reachable must not appear in the manifest —
    telling an agent to use something that cannot work wastes a whole run."""
    db, tmp = pool
    ts = now()
    db.write("INSERT INTO resources(id, kind, name, config_json, created_at, updated_at) "
             "VALUES('res-empty','mcp','Half Configured','{}',?,?)", (ts, ts))
    db.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, created_at) "
             "VALUES('g-empty','res-empty','project','proj1',?)", (ts,))
    access = resource_access.build(db, tmp, "proj1", "team1", "run-2")
    assert "Half Configured" not in {m["name"] for m in access.manifest}


def test_http_mcp_server_uses_a_bearer_header(pool):
    db, tmp = pool
    ts = now()
    db.write("INSERT INTO resources(id, kind, name, secret_ref, config_json, created_at, "
             "updated_at) VALUES('res-http','mcp','Remote',?,?,?,?)",
             (f"file:{tmp / 'gh-token'}",
              json.dumps({"mcp_transport": "http", "mcp_url": "https://mcp.example/sse"}),
              ts, ts))
    db.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, created_at) "
             "VALUES('g-http','res-http','global','*',?)", (ts,))
    access = resource_access.build(db, tmp, "proj1", "team1", "run-3")
    server = json.loads(open(access.mcp_config_path).read())["mcpServers"]["remote"]
    assert server == {"type": "http", "url": "https://mcp.example/sse",
                      "headers": {"Authorization": "Bearer ghp_live"}}
    resource_access.cleanup(tmp, "run-3")


def test_disabled_resource_is_not_exposed(pool):
    db, tmp = pool
    db.write("UPDATE resources SET enabled=0 WHERE id='res-llm'")
    access = resource_access.build(db, tmp, "proj1", "team1", "run-4")
    assert "BASTET_RES_OPENAI_MAIN_URL" not in access.env
    resource_access.cleanup(tmp, "run-4")
