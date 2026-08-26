"""Concurrency guarantees for the shared SQLite connection."""

from __future__ import annotations

import threading

from bastet_agent_os.db import Db


def test_query_waits_for_connection_lock(tmp_path):
    """Reads must not overlap another operation on the shared connection."""
    db = Db(tmp_path / "concurrency.db")
    entered = threading.Event()
    finished = threading.Event()
    result: list[int] = []

    def read() -> None:
        entered.set()
        result.append(db.one("SELECT 42 AS value")["value"])
        finished.set()

    db._lock.acquire()
    worker = threading.Thread(target=read)
    try:
        worker.start()
        assert entered.wait(timeout=1)
        assert not finished.wait(timeout=0.05)
    finally:
        db._lock.release()

    worker.join(timeout=1)
    assert not worker.is_alive()
    assert result == [42]
