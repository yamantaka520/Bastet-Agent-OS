"""API surface security (SPEC §5.9 / M1 acceptance #6): host/origin guard, token."""

import pytest
from fastapi.testclient import TestClient

from bastet_agent_os.config import Home
from bastet_agent_os.server import create_app


@pytest.fixture
def client(tmp_path):
    home = Home(tmp_path / "bastet-home")
    app = create_app(home)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.token = home.api_token()
    return c


def test_bad_host_is_rejected(client):
    resp = client.get("/api/projects", headers={"Host": "evil.example.com",
                                                "Authorization": f"Bearer {client.token}"})
    assert resp.status_code == 403  # DNS-rebinding defence


def test_docker_internal_host_is_allowed(client):
    # container runs reach the gateway with Host: host.docker.internal
    # (SPEC §5.4.3); ".internal" is ICANN-reserved so this survives the
    # DNS-rebinding threat model
    resp = client.get("/v1/health", headers={"Host": "host.docker.internal:8890"})
    assert resp.status_code == 200


def test_bad_origin_is_rejected(client):
    resp = client.get("/api/projects", headers={"Origin": "https://evil.example.com",
                                                "Authorization": f"Bearer {client.token}"})
    assert resp.status_code == 403


def test_api_requires_token(client):
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/projects",
                      headers={"Authorization": f"Bearer {client.token}"}).status_code == 200


def test_status_page_and_gateway_health_are_open(client):
    assert client.get("/").status_code == 200
    assert client.get("/v1/health").status_code == 200


def test_resource_secret_ref_is_never_echoed(client):
    headers = {"Authorization": f"Bearer {client.token}"}
    client.post("/api/resources", headers=headers,
                json={"name": "r1", "endpoint": "https://x", "api_flavor": "anthropic",
                      "secret_ref": "env:SUPER_SECRET_NAME"})
    listing = client.get("/api/resources", headers=headers).json()
    assert "SUPER_SECRET_NAME" not in str(listing)


def test_config_json_rejects_smuggled_secrets(client):
    headers = {"Authorization": f"Bearer {client.token}"}
    resp = client.post("/api/resources", headers=headers,
                       json={"name": "r2", "config": {"api_key": "sk-oops"}})
    assert resp.status_code == 400
    assert "secret" in resp.json()["detail"]
