"""Login-wizard PTY sessions (POSIX only)."""

import asyncio
import sys

import pytest

from bastet_agent_os.login_sessions import LoginSessionManager

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only")


async def test_pty_roundtrip_and_finish():
    manager = LoginSessionManager()
    session = manager.start("test", {}, ["cat"])  # echoes stdin back
    _, queue = manager.subscribe(session.id)

    manager.write(session.id, "device code ABC-123\n")
    chunks = b""
    for _ in range(10):
        chunk = await asyncio.wait_for(queue.get(), timeout=5)
        if chunk is None:
            break
        chunks += chunk
        if b"ABC-123" in chunks:
            break
    assert b"ABC-123" in chunks

    manager.kill(session.id)
    for _ in range(10):
        chunk = await asyncio.wait_for(queue.get(), timeout=5)
        if chunk is None:
            break
    assert session.done


async def test_write_to_dead_session_raises():
    manager = LoginSessionManager()
    session = manager.start("test", {}, ["true"])  # exits immediately
    _, queue = manager.subscribe(session.id)
    for _ in range(10):
        if await asyncio.wait_for(queue.get(), timeout=5) is None:
            break
    with pytest.raises(ValueError):
        manager.write(session.id, "hello")
