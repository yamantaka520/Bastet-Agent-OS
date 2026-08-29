"""OpenClaw executor using the bounded, headless ``agent exec`` contract.

The first integration is intentionally direct-only and writable.  OpenClaw's
stable JSON envelope is strong enough for task cards, usage and cancellation,
but its current exec interface does not expose a defensible read-only tool
allowlist.  Route admission therefore prevents review and Gateway jobs from
being assigned here instead of pretending they are safe.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from .base import (
    STREAM_LIMIT,
    SUMMARY_LIMIT,
    ProgressDeadline,
    RouteContract,
    RunEvent,
    RunResult,
    TaskSpec,
    last_json_object,
    register_builtin,
    run_env,
)

GRACE_SECONDS = 10
log = logging.getLogger("bastet.executor")


@dataclass
class OpenClawHandle:
    task: TaskSpec
    process: asyncio.subprocess.Process | None = None
    prompt_file: Path | None = None
    raw_stdout: str = ""
    stderr_tail: list[str] = field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False

    def state(self) -> dict:
        return {"kind": "openclaw", "run_id": self.task.run_id,
                "pid": self.process.pid if self.process else None,
                "prompt_file": str(self.prompt_file) if self.prompt_file else None}


@register_builtin
class OpenClawExecutor:
    kind = "openclaw"
    capabilities = {"code", "light-task"}
    route_contract = RouteContract(gateway=False)

    async def start(self, task: TaskSpec) -> OpenClawHandle:
        if task.read_only:
            raise ValueError("openclaw executor does not yet provide a proven "
                             "read-only tool contract")
        if task.gateway_url or task.run_token:
            raise ValueError("openclaw executor does not yet support Bastet Gateway")
        handle = OpenClawHandle(task=task)
        meta_dir = Path(task.workdir) / "._bastet"
        meta_dir.mkdir(parents=True, exist_ok=True)
        handle.prompt_file = meta_dir / f"openclaw-prompt-{task.run_id}.md"
        prompt = task.prompt
        if task.context_text:
            prompt = f"<context>\n{task.context_text}\n</context>\n\n{prompt}"
        handle.prompt_file.write_text(prompt)
        cmd = [
            "openclaw", "agent", "exec", "--message-file", str(handle.prompt_file),
            "--cwd", task.workdir, "--isolated", "--code-mode", "code",
            "--local-model-lean", "--timeout",
            str(ProgressDeadline.hard_timeout_s(task.timeout_s)), "--json",
        ]
        if task.llm and task.llm.get("model"):
            cmd += ["--model", task.llm["model"]]
        handle.process = await asyncio.create_subprocess_exec(
            *cmd,
            limit=STREAM_LIMIT,
            cwd=task.workdir,
            env=run_env(task, OPENCLAW_OFFLINE="1"),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=(sys.platform != "win32"))
        return handle

    async def stream(self, handle: OpenClawHandle) -> AsyncIterator[RunEvent]:
        assert handle.process and handle.process.stdout
        deadline = ProgressDeadline(handle.task.timeout_s)
        stderr_task = asyncio.create_task(self._drain_stderr(handle))
        try:
            while True:
                remaining = deadline.remaining()
                if remaining <= 0:
                    handle.timed_out = True
                    await self.cancel(handle)
                    return
                try:
                    raw = await asyncio.wait_for(handle.process.stdout.readline(),
                                                 timeout=min(remaining, 30))
                except TimeoutError:
                    continue
                except ValueError:
                    log.warning("run %s: dropped an oversized OpenClaw output line",
                                handle.task.run_id)
                    continue
                if not raw:
                    return
                deadline.note_progress()
                handle.raw_stdout += raw.decode(errors="replace")
                yield RunEvent("activity", {"kind": "result_envelope"})
        finally:
            stderr_task.cancel()

    async def _drain_stderr(self, handle: OpenClawHandle) -> None:
        assert handle.process and handle.process.stderr
        async for raw in handle.process.stderr:
            line = raw.decode(errors="replace").rstrip()
            handle.stderr_tail.append(line)
            del handle.stderr_tail[:-20]

    async def respond(self, handle: OpenClawHandle,
                      request_id: str, reply: dict) -> None:
        raise NotImplementedError("openclaw agent exec has no interaction channel")

    async def cancel(self, handle: OpenClawHandle) -> None:
        handle.cancelled = True
        process = handle.process
        if process is None or process.returncode is not None:
            return
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
            await asyncio.wait_for(process.wait(), timeout=GRACE_SECONDS)
        except (TimeoutError, ProcessLookupError):
            try:
                if sys.platform != "win32":
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass

    async def result(self, handle: OpenClawHandle) -> RunResult:
        process = handle.process
        if process and process.returncode is None:
            await process.wait()
        envelope = last_json_object(handle.raw_stdout) or {}
        error = envelope.get("error") or {}
        if handle.timed_out or envelope.get("status") == "timeout":
            status = "timeout"
        elif handle.cancelled:
            status = "cancelled"
        elif process and process.returncode == 0 and envelope.get("ok") is True:
            status = "succeeded"
        else:
            status = "failed"
        usage = envelope.get("usage") or {}
        summary = str(envelope.get("final") or "")
        if not summary and isinstance(error, dict):
            summary = str(error.get("message") or "")
        return RunResult(
            status=status,
            summary=(summary or "\n".join(handle.stderr_tail[-5:]))[:SUMMARY_LIMIT],
            tokens_in=int(usage.get("input") or 0),
            tokens_out=int(usage.get("output") or 0),
            cost_usd=float(envelope.get("costUsd") or 0),
            precision="reported",
        )
