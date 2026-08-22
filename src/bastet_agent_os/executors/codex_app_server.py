"""Codex app-server executor: stateful threads, streamed turns, and approvals.

The stable local transport is JSONL over stdio.  This executor deliberately
does not use the experimental websocket transport, and lives beside (rather
than replacing) the battle-tested ``codex exec --json`` executor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..pricing import Usage
from .base import (
    STREAM_LIMIT,
    SUMMARY_LIMIT,
    RunEvent,
    RunResult,
    TaskSpec,
    last_json_object,
    parse_event,
    register_builtin,
    run_env,
    worktree_git_dir,
)
from .codex import GRACE_SECONDS, VERDICT_SCHEMA

log = logging.getLogger("bastet.executor")


@dataclass
class CodexAppServerHandle:
    task: TaskSpec
    process: asyncio.subprocess.Process
    thread_id: str
    turn_id: str
    next_id: int = 10
    usage: Usage = field(default_factory=Usage)
    summary: str = ""
    failed_reason: str = ""
    pending: list[dict[str, Any]] = field(default_factory=list)
    request_ids: dict[str, int | str] = field(default_factory=dict)
    finished: bool = False
    cancelled: bool = False
    timed_out: bool = False
    stderr_tail: list[str] = field(default_factory=list)
    stderr_task: Any = None
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def state(self) -> dict:
        return {"kind": "codex-app-server", "run_id": self.task.run_id,
                "pid": self.process.pid, "thread_id": self.thread_id,
                "turn_id": self.turn_id}


@register_builtin
class CodexAppServerExecutor:
    kind = "codex-app-server"
    capabilities = {"code", "review", "interaction", "stateful-thread"}

    @staticmethod
    async def _write(process: asyncio.subprocess.Process, message: dict) -> None:
        if process.stdin is None:
            raise RuntimeError("codex app-server stdin is unavailable")
        process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await process.stdin.drain()

    @staticmethod
    async def _response(process: asyncio.subprocess.Process, request_id: int,
                        pending: list[dict[str, Any]]) -> dict:
        assert process.stdout
        while True:
            raw = await process.stdout.readline()
            if not raw:
                raise RuntimeError("codex app-server exited during initialization")
            message = parse_event(raw)
            if not message:
                continue
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if message.get("error"):
                    raise RuntimeError(str(message["error"]))
                return message.get("result") or {}
            pending.append(message)

    async def start(self, task: TaskSpec) -> CodexAppServerHandle:
        if task.gateway_url:
            raise ValueError("codex-app-server gateway routing is not supported yet; "
                             "use executor type 'codex' for metered gateway runs")
        process = await asyncio.create_subprocess_exec(
            "codex", "app-server", cwd=task.workdir, env=run_env(task),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=STREAM_LIMIT,
            start_new_session=(sys.platform != "win32"))
        pending: list[dict[str, Any]] = []
        await self._write(process, {"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "bastet-agent-os", "version": "0.31"},
            "capabilities": {"experimentalApi": True},
        }})
        await self._response(process, 1, pending)
        await self._write(process, {"method": "initialized", "params": {}})

        thread_params: dict[str, Any] = {"cwd": task.workdir,
                                        "approvalPolicy": "unlessTrusted"}
        if task.llm and task.llm.get("model"):
            thread_params["model"] = task.llm["model"]
        await self._write(process, {"id": 2, "method": "thread/start",
                                    "params": thread_params})
        result = await self._response(process, 2, pending)
        thread = result.get("thread") or result
        thread_id = str(thread.get("id") or thread.get("threadId") or "")
        if not thread_id:
            raise RuntimeError("codex app-server returned no thread id")

        prompt = task.prompt
        if task.context_text:
            prompt = f"<context>\n{task.context_text}\n</context>\n\n{prompt}"
        if task.read_only:
            sandbox = {"type": "readOnly"}
        else:
            roots = [task.workdir]
            git_dir = worktree_git_dir(task.workdir)
            if git_dir:
                roots.append(git_dir)
            sandbox = {"type": "workspaceWrite", "writableRoots": roots,
                       "networkAccess": False}
        turn_params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": task.workdir,
            "approvalPolicy": "unlessTrusted",
            "sandboxPolicy": sandbox,
        }
        if task.expect_verdict:
            turn_params["outputSchema"] = VERDICT_SCHEMA
        await self._write(process, {"id": 3, "method": "turn/start", "params": turn_params})
        result = await self._response(process, 3, pending)
        turn = result.get("turn") or result
        turn_id = str(turn.get("id") or turn.get("turnId") or "")
        if not turn_id:
            raise RuntimeError("codex app-server returned no turn id")
        return CodexAppServerHandle(task, process, thread_id, turn_id, pending=pending)

    async def _drain_stderr(self, handle: CodexAppServerHandle) -> None:
        assert handle.process.stderr
        while raw := await handle.process.stderr.readline():
            text = raw.decode(errors="replace").rstrip()
            if text:
                handle.stderr_tail.append(text)
                del handle.stderr_tail[:-20]

    async def stream(self, handle: CodexAppServerHandle) -> AsyncIterator[RunEvent]:
        assert handle.process.stdout
        handle.stderr_task = asyncio.create_task(self._drain_stderr(handle))
        deadline = asyncio.get_running_loop().time() + handle.task.timeout_s
        while not handle.finished:
            if handle.pending:
                message = handle.pending.pop(0)
            else:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    handle.timed_out = True
                    await self.cancel(handle)
                    return
                try:
                    raw = await asyncio.wait_for(handle.process.stdout.readline(),
                                                 min(remaining, 30))
                except TimeoutError:
                    continue
                if not raw:
                    handle.failed_reason = "codex app-server exited before turn completion"
                    return
                message = parse_event(raw) or {}
            async for event in self._events(handle, message):
                yield event

    async def _events(self, handle: CodexAppServerHandle,
                      message: dict[str, Any]) -> AsyncIterator[RunEvent]:
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        if "id" in message and method.endswith("requestApproval"):
            public_id = str(message["id"])
            handle.request_ids[public_id] = message["id"]
            yield RunEvent("interaction_request", {
                "request_id": public_id, "kind": "permission_request",
                "payload": {"method": method, **params},
            })
            return
        if method in ("item/agentMessage/delta", "item/outputText/delta"):
            delta = str(params.get("delta") or "")
            handle.summary += delta
            if delta:
                yield RunEvent("progress", {"text": delta[:500]})
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") in ("agentMessage", "agent_message"):
                text = str(item.get("text") or item.get("content") or "")
                if text and not handle.summary:
                    handle.summary = text
                    yield RunEvent("progress", {"text": text[:500]})
            elif item.get("type") in ("commandExecution", "command_execution"):
                yield RunEvent("tool_call_summary", {
                    "tool": str(item.get("command") or "command")[:80]})
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or params.get("usage") or params
            cached = int(usage.get("cachedInputTokens") or 0)
            handle.usage = Usage(
                tokens_in=max(0, int(usage.get("inputTokens") or 0) - cached),
                tokens_out=int(usage.get("outputTokens") or 0), cache_read=cached)
        elif method == "turn/completed":
            turn = params.get("turn") or params
            status = str(turn.get("status") or "completed")
            if status not in ("completed", "succeeded"):
                handle.failed_reason = str(turn.get("error") or status)
            handle.finished = True
        elif method in ("turn/failed", "error"):
            handle.failed_reason = str(params.get("error") or params.get("message") or method)
            handle.finished = True

    async def respond(self, handle: CodexAppServerHandle, request_id: str,
                      reply: dict) -> None:
        rpc_id = handle.request_ids.pop(request_id, request_id)
        decision = reply.get("decision")
        if not decision:
            decision = "accept" if reply.get("approved") else "decline"
        async with handle.write_lock:
            await self._write(handle.process, {"id": rpc_id,
                                               "result": {"decision": decision}})

    async def cancel(self, handle: CodexAppServerHandle) -> None:
        handle.cancelled = True
        if handle.process.returncode is not None:
            return
        try:
            async with handle.write_lock:
                request_id = handle.next_id
                handle.next_id += 1
                await self._write(handle.process, {"id": request_id,
                    "method": "turn/interrupt", "params": {
                        "threadId": handle.thread_id, "turnId": handle.turn_id}})
            await asyncio.wait_for(handle.process.wait(), GRACE_SECONDS)
        except (TimeoutError, ProcessLookupError):
            self._terminate(handle.process)

    @staticmethod
    def _terminate(process: asyncio.subprocess.Process) -> None:
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass

    async def result(self, handle: CodexAppServerHandle) -> RunResult:
        if handle.process.returncode is None:
            self._terminate(handle.process)
            try:
                await asyncio.wait_for(handle.process.wait(), GRACE_SECONDS)
            except TimeoutError:
                handle.process.kill()
        status = ("timeout" if handle.timed_out else "cancelled" if handle.cancelled
                  else "failed" if handle.failed_reason else "succeeded")
        verdict = None
        if handle.task.expect_verdict:
            data = last_json_object(handle.summary)
            if data and data.get("verdict"):
                verdict = {"verdict": str(data["verdict"]).lower(),
                           "reasons": data.get("reasons") or []}
        return RunResult(
            status=status,
            summary=(handle.summary or handle.failed_reason
                     or "\n".join(handle.stderr_tail[-5:]))[:SUMMARY_LIMIT],
            tokens_in=handle.usage.tokens_in, tokens_out=handle.usage.tokens_out,
            cache_read=handle.usage.cache_read,
            precision="estimated" if handle.timed_out or handle.cancelled else "reported",
            structured_verdict=verdict,
        )
