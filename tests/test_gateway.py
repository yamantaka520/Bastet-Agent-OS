"""Gateway integration tests with a mocked upstream (httpx.MockTransport)."""


import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bastet_agent_os import run_tokens
from bastet_agent_os.gateway import GatewayContext, build_router
from bastet_agent_os.governance import Reservations
from bastet_agent_os.pricing import PriceBook

UPSTREAM_RESPONSE = {
    "id": "msg_01",
    "model": "claude-sonnet-4-20250514",
    "content": [{"type": "text", "text": "hello"}],
    "usage": {"input_tokens": 11, "output_tokens": 7,
              "cache_read_input_tokens": 500, "cache_creation_input_tokens": 100},
}


@pytest.fixture
def gateway_client(seeded, monkeypatch):
    monkeypatch.setenv("TEST_UPSTREAM_KEY", "sk-upstream-secret")
    captured = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json=UPSTREAM_RESPONSE)

    ctx = GatewayContext(seeded, PriceBook(), Reservations())
    app = FastAPI()
    app.include_router(build_router(ctx, upstream_transport=httpx.MockTransport(upstream)))
    return TestClient(app), seeded, captured


def test_rejects_missing_or_bogus_token(gateway_client):
    client, db, _ = gateway_client
    assert client.post("/v1/messages", json={}).status_code == 401
    assert client.post("/v1/messages", json={},
                       headers={"Authorization": "Bearer brt_bogus"}).status_code == 401


def test_proxies_and_meters_anthropic_request(gateway_client):
    client, db, captured = gateway_client
    token = run_tokens.issue(db, "run1", ttl_seconds=60)
    resp = client.post("/v1/messages", json={"model": "claude-sonnet-4-20250514",
                                             "messages": [], "max_tokens": 10},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["content"][0]["text"] == "hello"
    # upstream got the real key; the run token never left the gateway
    assert captured["headers"]["x-api-key"] == "sk-upstream-secret"
    assert "authorization" not in captured["headers"]
    # ledger row landed with cache tokens split out
    row = db.one("SELECT * FROM usage_ledger WHERE run_id='run1'")
    assert (row["tokens_in"], row["tokens_out"]) == (11, 7)
    assert (row["cache_read"], row["cache_write"]) == (500, 100)
    assert row["cost_usd"] > 0


def test_flavor_mismatch_is_rejected(gateway_client):
    client, db, _ = gateway_client
    token = run_tokens.issue(db, "run1", ttl_seconds=60)
    resp = client.post("/v1/chat/completions", json={"model": "x", "messages": []},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400  # resource speaks anthropic, not openai


def test_exhausted_budget_returns_429(gateway_client):
    client, db, _ = gateway_client
    db.write("INSERT INTO usage_ledger(id, run_id, resource_id, cost_usd, at) "
             "VALUES('big','run1','res1',10.0,datetime('now'))")  # budget is 10 USD
    token = run_tokens.issue(db, "run1", ttl_seconds=60)
    resp = client.post("/v1/messages", json={"model": "x", "messages": []},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 429


def test_terminal_run_is_rejected(gateway_client):
    client, db, _ = gateway_client
    token = run_tokens.issue(db, "run1", ttl_seconds=60)
    db.write("UPDATE runs SET status='succeeded' WHERE id='run1'")
    resp = client.post("/v1/messages", json={"model": "x", "messages": []},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_resource_credential_can_point_into_the_credential_pool(gateway_client,
                                                                tmp_path):
    """Resources created through the WebUI picker store secret:<id>. The gateway
    must follow that pointer, or every such resource 502s on first use."""
    client, db, captured = gateway_client
    from bastet_agent_os.db import now as _now

    key = tmp_path / "pooled"
    key.write_text("sk-pooled")
    db.write("INSERT INTO resources(id, kind, name, secret_ref, config_json, "
             "created_at, updated_at) VALUES('sec-p','secret','shared',?,'{}',?,?)",
             (f"file:{key}", _now(), _now()))
    db.write("UPDATE resources SET secret_ref='secret:sec-p' WHERE id='res1'")

    token = run_tokens.issue(db, "run1", ttl_seconds=60)
    resp = client.post("/v1/messages", json={"model": "claude-sonnet-4-20250514",
                                             "messages": []},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert captured["headers"]["x-api-key"] == "sk-pooled"
