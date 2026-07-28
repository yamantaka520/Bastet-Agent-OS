"""WebUI login wizard: run an executor's interactive login in a PTY and
bridge it to the browser over WebSocket.

The command is always taken from the fixed executor catalog
(accounts.login_command) — never from client input — so this is a guided
login flow, not a shell. Device-auth style flows print a URL + code the
user opens anywhere; paste-back flows type into the same panel.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from dataclasses import dataclass, field

from .db import new_id

SESSION_TIMEOUT_S = 600
BUFFER_LIMIT = 200_000


@dataclass
class LoginSession:
    id: str
    kind: str
    pid: int
    master_fd: int
    buffer: bytes = b""
    subscribers: set = field(default_factory=set)
    done: bool = False
    exit_code: int | None = None
    started_at: float = field(default_factory=time.time)


class LoginSessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, LoginSession] = {}

    def start(self, kind: str, env: dict[str, str], argv: list[str]) -> LoginSession:
        if sys.platform == "win32":
            raise RuntimeError("WebUI 登入精靈暫不支援 Windows — 請在終端執行登入指令")
        import pty

        pid, master_fd = pty.fork()
        if pid == 0:  # child: exec the login command in the PTY
            os.environ.update(env)
            os.environ.setdefault("TERM", "xterm-256color")
            try:
                os.execvp(argv[0], argv)
            finally:
                os._exit(127)

        session = LoginSession(id=new_id("lgn"), kind=kind, pid=pid,
                               master_fd=master_fd)
        self.sessions[session.id] = session
        loop = asyncio.get_running_loop()
        loop.add_reader(master_fd, self._on_readable, session)
        loop.call_later(SESSION_TIMEOUT_S, self._timeout, session)
        return session

    def _on_readable(self, session: LoginSession) -> None:
        try:
            data = os.read(session.master_fd, 4096)
        except OSError:
            data = b""
        if not data:
            self._finish(session)
            return
        session.buffer = (session.buffer + data)[-BUFFER_LIMIT:]
        for queue in list(session.subscribers):
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                pass

    def _finish(self, session: LoginSession) -> None:
        if session.done:
            return
        session.done = True
        asyncio.get_running_loop().remove_reader(session.master_fd)
        try:
            _, status = os.waitpid(session.pid, os.WNOHANG)
            session.exit_code = os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            session.exit_code = -1
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        for queue in list(session.subscribers):
            queue.put_nowait(None)  # sentinel: session over

    def _timeout(self, session: LoginSession) -> None:
        if not session.done:
            self.kill(session.id)

    def write(self, session_id: str, text: str) -> None:
        session = self.sessions.get(session_id)
        if session is None or session.done:
            raise ValueError("login session is not active")
        os.write(session.master_fd, text.encode())

    def kill(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None or session.done:
            return
        try:
            os.kill(session.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def subscribe(self, session_id: str) -> tuple[LoginSession, asyncio.Queue]:
        session = self.sessions[session_id]
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        if session.buffer:
            queue.put_nowait(session.buffer)  # replay what already happened
        if session.done:
            queue.put_nowait(None)
        session.subscribers.add(queue)
        return session, queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        session = self.sessions.get(session_id)
        if session is not None:
            session.subscribers.discard(queue)
