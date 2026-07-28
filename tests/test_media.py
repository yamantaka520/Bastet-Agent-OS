"""Media resource governance (M4): gateway image/tts endpoints + lite tools."""

import base64

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bastet_agent_os import run_tokens
from bastet_agent_os.db import now
from bastet_agent_os.gateway import GatewayContext, build_router
from bastet_agent_os.governance import Reservations
from bastet_agent_os.pricing import PriceBook

PNG = base64.b64encode(b"fake-png-bytes").decode()


@pytest.fixture
def media_setup(seeded, monkeypatch):
    monkeypatch.setenv("IMG_KEY", "sk-img")
    ts = now()
    seeded.write("INSERT INTO resources(id, kind, name, endpoint, api_flavor, secret_ref, "
                 "config_json, created_at, updated_at) VALUES('res-img','image','dalle',"
                 "'https://img.example',NULL,'env:IMG_KEY','{\"cost_per_call\": 0.04}',?,?)",
                 (ts, ts))
    seeded.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, budget_usd, "
                 "created_at) VALUES('grt-img','res-img','project','proj1',1.0,?)", (ts,))

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": PNG}]})

    ctx = GatewayContext(seeded, PriceBook(), Reservations())
    app = FastAPI()
    app.include_router(build_router(ctx, upstream_transport=httpx.MockTransport(upstream)))
    client = TestClient(app)
    token = run_tokens.issue(seeded, "run1", ttl_seconds=60)
    return client, seeded, token


def test_media_requires_resource_header(media_setup):
    client, db, token = media_setup
    resp = client.post("/v1/images/generations", json={"prompt": "a cat"},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400


def test_media_flat_cost_lands_in_ledger(media_setup):
    client, db, token = media_setup
    resp = client.post("/v1/images/generations",
                       json={"prompt": "a cat goddess", "n": 2},
                       headers={"Authorization": f"Bearer {token}",
                                "X-Bastet-Resource": "dalle"})
    assert resp.status_code == 200
    row = db.one("SELECT * FROM usage_ledger WHERE resource_id='res-img'")
    assert row["cost_usd"] == pytest.approx(0.08)  # 2 calls x cost_per_call


def test_media_kind_mismatch_rejected(media_setup):
    client, db, token = media_setup
    # res1 is an llm resource — not usable through the image endpoint
    resp = client.post("/v1/images/generations", json={"prompt": "x"},
                       headers={"Authorization": f"Bearer {token}",
                                "X-Bastet-Resource": "anthropic-main"})
    assert resp.status_code == 403


def test_media_needs_grant(media_setup):
    client, db, token = media_setup
    db.write("DELETE FROM grants WHERE id='grt-img'")
    resp = client.post("/v1/images/generations", json={"prompt": "x"},
                       headers={"Authorization": f"Bearer {token}",
                                "X-Bastet-Resource": "dalle"})
    assert resp.status_code == 403


async def test_lite_generate_image_saves_file(tmp_path, media_setup):
    from bastet_agent_os.executors.base import TaskSpec
    from bastet_agent_os.executors.bastet_lite import _generate_image

    client, db, token = media_setup

    def fake_gateway(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-bastet-resource"] == "dalle"
        return httpx.Response(200, json={"data": [{"b64_json": PNG}]})

    task = TaskSpec(run_id="run1", prompt="p", workdir=str(tmp_path),
                    gateway_url="http://gw.test", run_token=token)
    out = await _generate_image(task, {"prompt": "cat", "path": "art/cat.png",
                                       "resource": "dalle"},
                                httpx.MockTransport(fake_gateway))
    assert "art/cat.png" in out
    assert (tmp_path / "art" / "cat.png").read_bytes() == b"fake-png-bytes"
