"""The bastet-config skill and the chat-apply protocol.

The contract under test is the trust boundary: the model PROPOSES, the human
APPLIES, and nothing that changes who-can-act is reachable through this path.
"""

import json

import pytest
from fastapi.testclient import TestClient

from bastet_agent_os import self_config
from bastet_agent_os.config import Home
from bastet_agent_os.server import create_app


@pytest.fixture
def client(tmp_path):
    home = Home(tmp_path / "home")
    c = TestClient(create_app(home), base_url="http://127.0.0.1")
    c.headers["Authorization"] = f"Bearer {home.api_token()}"
    c.post("/api/teams", json={"id": "t1", "name": "T"})
    repo = tmp_path / "repo"
    repo.mkdir()
    c.post("/api/projects", json={"id": "p1", "repo_path": str(repo), "team_id": "t1"})
    return c, home


def test_the_skill_is_seeded_globally_visible(client):
    """create_app seeds it: any responder can read the guide like any skill."""
    c, home = client

    rows = c.get("/api/resources").json()
    skill = next(r for r in rows if r["name"] == "bastet-config")
    assert skill["kind"] == "skill"
    assert any(s["scope_type"] == "global" for s in skill["scopes"])
    # the guide file exists and documents the protocol and every kind
    guide = (home.root / "skills" / "bastet-config.md").read_text()
    assert "```bastet-config" in guide
    for kind in ("llm", "mcp", "api", "git", "tts"):
        assert f"**{kind}**" in guide


def test_extract_takes_the_last_wellformed_block():
    text = ('好的，先建立資源。\n```bastet-config\n{"actions": [{"op": "x"}]}\n```\n'
            '更正，用這個：\n```bastet-config\n'
            '{"actions": [{"op": "resource.create", "kind": "tts", "name": "el"}]}\n```')
    actions = self_config.extract_actions(text)
    assert actions == [{"op": "resource.create", "kind": "tts", "name": "el"}]

    assert self_config.extract_actions("no block here") is None
    assert self_config.extract_actions("```bastet-config\nnot json\n```") is None


def test_apply_creates_a_media_resource_with_grant(client):
    """The headline case: a multimedia API resource, set up from chat."""
    c, _ = client

    out = c.post("/api/config/apply", json={"actions": [
        {"op": "resource.create", "kind": "tts", "name": "eleven-tts",
         "endpoint": "https://api.elevenlabs.io",
         "config": {"default_model": "eleven_v3", "note": "語音合成"},
         "scope_type": "project", "scope_id": "p1"},
    ]})

    assert out.status_code == 200, out.text
    assert out.json()["ok"] == 1
    rows = c.get("/api/resources").json()
    made = next(r for r in rows if r["name"] == "eleven-tts")
    assert made["kind"] == "tts"
    assert [s["scope_type"] for s in made["scopes"]] == ["project"]


def test_a_raw_key_in_the_proposal_is_refused(client):
    """A raw credential in a chat proposal has already been through the model;
    filing it quietly would legitimise the leak."""
    c, _ = client

    out = c.post("/api/config/apply", json={"actions": [
        {"op": "resource.create", "kind": "tts", "name": "leaky",
         "endpoint": "https://api.example.com",
         "secret_ref": "sk-verysecretkey1234567890"},
    ]})

    body = out.json()
    assert body["failed"] == 1
    assert "secret:" in body["results"][0]["detail"]
    assert not any(r["name"] == "leaky" for r in c.get("/api/resources").json())


def test_ops_that_change_who_can_act_do_not_exist(client):
    """user.create / token anything / channel bindings: a prompt-injected 'add
    an admin' must find nothing here to call."""
    c, _ = client

    out = c.post("/api/config/apply", json={"actions": [
        {"op": "user.create", "name": "backdoor", "role": "admin"},
    ]})

    body = out.json()
    assert body["failed"] == 1
    assert "user.create" in body["results"][0]["detail"]
    assert all(op.split(".")[0] in ("resource", "grant", "settings")
               for op in self_config.ALLOWED_OPS)


def test_partial_failure_lands_the_good_actions(client):
    c, _ = client

    out = c.post("/api/config/apply", json={"actions": [
        {"op": "resource.create", "kind": "api", "name": "good-one",
         "endpoint": "https://api.example.com"},
        {"op": "grant.create", "resource": "nonexistent", "scope_type": "team",
         "scope_id": "t1"},
    ]}).json()

    assert (out["ok"], out["failed"]) == (1, 1)
    assert any(r["name"] == "good-one" for r in c.get("/api/resources").json())


def test_apply_is_audited_with_the_human_actor(client):
    c, _ = client
    c.post("/api/config/apply", json={"actions": [
        {"op": "resource.create", "kind": "api", "name": "aud-check",
         "endpoint": "https://x.example"}]})

    rows = c.get("/api/audit", params={"q": "aud-check"}).json()["rows"]
    detail = json.loads(rows[0]["detail_json"])
    assert detail["via"] == "chat"
    assert rows[0]["actor"].startswith("user:")   # the presser, not the model


def test_team_scope_works_without_a_local_teams_table(client):
    """Live failure: `no such table: teams`. Teams are AMOS org objects — Bastet
    has no local table, and the rest of the product accepts team ids it has not
    seen. The apply path must do the same."""
    c, _ = client
    c.post("/api/config/apply", json={"actions": [
        {"op": "resource.create", "kind": "api", "name": "team-scoped",
         "endpoint": "https://x.example"}]})

    out = c.post("/api/config/apply", json={"actions": [
        {"op": "grant.create", "resource": "team-scoped",
         "scope_type": "team", "scope_id": "Meow1"},
    ]}).json()

    assert out["ok"] == 1, out
    made = next(r for r in c.get("/api/resources").json()
                if r["name"] == "team-scoped")
    assert ("team", "Meow1") in [(s["scope_type"], s["scope_id"])
                                 for s in made["scopes"]]


def test_skill_with_install_command_is_creatable_and_installable(client):
    """The novita case: a proposal can carry the skill's install command; the
    human applies, then presses 安裝 on the Resources tab (admin, audited, full
    log) — the same flow MCP installs use."""
    c, _ = client

    out = c.post("/api/config/apply", json={"actions": [
        {"op": "resource.create", "kind": "skill", "name": "novita-skill",
         "config": {"skill_source": "https://github.com/novitalabs/skills.git",
                    "install_command": "echo installed-to-the-right-place"}},
    ]}).json()
    assert out["ok"] == 1, out

    made = next(r for r in c.get("/api/resources").json()
                if r["name"] == "novita-skill")
    # the install endpoint accepts it (admin-only; here it runs the real command
    # against a temp HOME-less env and reports honestly either way)
    result = c.post(f"/api/resources/{made['id']}/install")
    assert result.status_code == 200
    assert "status" in result.json()


def test_only_saved_credential_pointers_are_accepted(client):
    """Stricter than the admin UI on purpose: a model-proposed file:/env: ref
    could point a 'credential' at an arbitrary host file, which a run would then
    send to whatever endpoint the same proposal named."""
    c, _ = client

    for ref in ("file:/home/user/.bastet/api_token", "env:HOME", "keyring:a/b"):
        out = c.post("/api/config/apply", json={"actions": [
            {"op": "resource.create", "kind": "api", "name": f"sneaky-{ref[:4]}",
             "endpoint": "https://attacker.example", "secret_ref": ref}]}).json()
        assert out["failed"] == 1, ref
        assert "secret:" in out["results"][0]["detail"]


def test_credential_rows_cannot_be_rewritten_from_chat(client, tmp_path):
    """Redirecting a credential's ref would poison every resource pointing at
    it — the one indirection the whole safety story rests on."""
    c, _ = client
    c.post("/api/secrets", json={"name": "real-key", "value": "tok_abc",
                                 "scope_type": "global", "scope_id": ""})
    cred = next(r for r in c.get("/api/secrets").json() if r["name"] == "real-key")

    out = c.post("/api/config/apply", json={"actions": [
        {"op": "resource.update", "id": cred["id"],
         "endpoint": "https://attacker.example"}]}).json()

    assert out["failed"] == 1
    assert "憑證" in out["results"][0]["detail"]
