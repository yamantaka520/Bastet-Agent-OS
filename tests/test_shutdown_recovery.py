"""Process shutdown and power-loss recovery are explicit engine contracts."""

import asyncio
import inspect
import time

from fastapi.testclient import TestClient

from bastet_agent_os import cli
from bastet_agent_os.config import Home
from bastet_agent_os.db import Db
from bastet_agent_os.server import create_app


def test_database_uses_power_loss_durable_commits(tmp_path):
    db = Db(tmp_path / "durable.db")
    try:
        assert db.one("PRAGMA journal_mode")[0] == "wal"
        assert db.one("PRAGMA synchronous")[0] == 2  # FULL
    finally:
        db.close()


def test_empty_server_shutdown_reaps_background_loops(tmp_path):
    """Regression: production needed systemd's 90-second SIGKILL fence."""
    app = create_app(Home(tmp_path / "home"))
    started = time.monotonic()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/version").status_code == 200
    assert time.monotonic() - started < 5


def test_server_bounds_open_connection_shutdown_before_systemd_fence():
    source = inspect.getsource(cli.serve)
    assert "timeout_graceful_shutdown=5" in source


async def test_orchestrator_shutdown_cancels_and_reaps_owned_tasks(orch):
    cancelled = asyncio.Event()

    async def worker():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    orch._spawn(worker())
    await asyncio.sleep(0)
    await orch.shutdown()

    assert cancelled.is_set()
    assert not orch._tasks
