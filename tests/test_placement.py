"""Execution-host placement is explicit, immutable and fail-closed."""

import json

import pytest
from fake_executor import req
from fastapi.testclient import TestClient

from bastet_agent_os import placement
from bastet_agent_os.config import Home
from bastet_agent_os.server import create_app
from bastet_agent_os.workflow import parse_stages


def test_local_host_identity_is_stable_and_reports_capacity(seeded):
    first = placement.ensure_local_host(seeded)
    second = placement.ensure_local_host(seeded)

    assert first == second
    view = placement.host(seeded, first)
    assert view["kind"] == "local"
    assert view["dispatchable"] is True
    assert view["active"] == 1  # the legacy fixture job is implicitly local
    assert view["max_concurrency"] == 1
    assert view["available"] == 0
    assert view["executor_types"] == ["claude-code"]


def test_peer_inventory_is_not_remote_execution_authority(seeded):
    peer = placement.register_peer(
        seeded, host_id="build-02", name="Build 02",
        endpoint="https://build-02.example.test", max_concurrency=8, actor="admin")
    assert peer["dispatchable"] is False
    assert "transport" in peer["placement_blocker"]

    report = placement.preview(
        seeded, "proj1", parse_stages([{"name": "work", "gate": "auto"}]),
        "build-02")
    assert report["ok"] is False
    assert report["selected_host_id"] is None


@pytest.mark.parametrize("endpoint", [
    "http://build.example.test",
    "https://user:secret@build.example.test",
    "https://build.example.test/path?token=secret",
])
def test_peer_endpoint_rejects_unsafe_transport_or_embedded_credentials(
        seeded, endpoint):
    with pytest.raises(ValueError):
        placement.register_peer(
            seeded, host_id="build-02", name="Build", endpoint=endpoint,
            max_concurrency=1, actor="admin")


def test_local_dispatch_atomically_links_placement_receipt(orch, seeded, monkeypatch):
    monkeypatch.setattr(orch, "_spawn", lambda coroutine: coroutine.close())
    job_id = orch.dispatch(req())
    job = seeded.one(
        "SELECT execution_host_id,placement_receipt_id FROM jobs WHERE id=?", (job_id,))
    receipt = seeded.one("SELECT * FROM placement_receipts WHERE id=?",
                         (job["placement_receipt_id"],))

    assert job["execution_host_id"] == placement.local_host_id(seeded)
    assert receipt["job_id"] == job_id
    assert receipt["status"] == "selected"
    assert receipt["selected_host_id"] == job["execution_host_id"]
    assert json.loads(receipt["requirements_json"])["stages"] == ["work"]


def test_remote_dispatch_creates_only_a_blocked_receipt(orch, seeded):
    placement.register_peer(
        seeded, host_id="build-02", name="Build 02",
        endpoint="https://build-02.example.test", max_concurrency=8, actor="admin")
    before = seeded.one("SELECT COUNT(*) n FROM jobs")["n"]

    with pytest.raises(placement.PlacementError) as caught:
        orch.dispatch(req(execution_host_id="build-02"))

    assert seeded.one("SELECT COUNT(*) n FROM jobs")["n"] == before
    receipt = seeded.one("SELECT * FROM placement_receipts WHERE id=?",
                         (caught.value.receipt["receipt_id"],))
    assert receipt["status"] == "blocked"
    assert receipt["job_id"] is None


def test_host_and_preview_api_expose_control_plane_without_enabling_peer(tmp_path):
    home = Home(tmp_path / "home")
    client = TestClient(create_app(home), base_url="http://127.0.0.1")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"

    created = client.post("/api/execution-hosts", json={
        "id": "build-02", "name": "Build 02",
        "endpoint": "https://build-02.example.test", "max_concurrency": 2,
    })
    assert created.status_code == 200
    assert created.json()["dispatchable"] is False
    hosts = client.get("/api/execution-hosts").json()
    assert {item["kind"] for item in hosts} == {"local", "peer"}
