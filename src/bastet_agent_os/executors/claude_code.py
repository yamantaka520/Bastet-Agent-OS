"""claude-code executor (SPEC §5.1.2): drives Claude Code headless.

Two accounting paths:
  gateway      TaskSpec.gateway_url set -> ANTHROPIC_BASE_URL points at the
               Bastet gateway, ANTHROPIC_AUTH_TOKEN is the run token (API
               billing; mutually exclusive with a Max subscription).
  subscription no gateway_url -> Claude Code talks to Anthropic directly; we
               trust the usage in its result event ("reported" precision).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .base import (
    STREAM_LIMIT,
    SUMMARY_LIMIT,
    RunEvent,
    RunResult,
    TaskSpec,
    parse_event,
    register_builtin,
    run_env,
)

log = logging.getLogger("bastet.executor")


GRACE_SECONDS = 10  # SIGTERM -> grace -> SIGKILL

# Reviewer runs: no Edit/Bash. Write stays enabled because the structured
# verdict travels via a file (workflow.VERDICT_RELPATH) — headless
# --allowedTools cannot scope Write to one path yet; the M4 Agent SDK
# migration closes this gap.
# WebFetch/WebSearch are read-only by nature and reviewers/chat agents
# legitimately read docs — their absence surfaced as permission errors
READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "Write", "WebFetch", "WebSearch"]


@dataclass
class ClaudeCodeHandle:
    task: TaskSpec
    process: asyncio.subprocess.Process | None = None
    result_event: dict[str, Any] | None = None
    stderr_tail: list[str] = field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False

    def state(self) -> dict:
        """JSON-serializable handle state (SPEC §5.1.1 persistence contract)."""
        return {"pid": self.process.pid if self.process else None,
                "run_id": self.task.run_id}


@register_builtin
class ClaudeCodeExecutor:
    kind = "claude-code"
    capabilities = {"code", "review", "mcp"}

    async def start(self, task: TaskSpec) -> ClaudeCodeHandle:
        handle = ClaudeCodeHandle(task=task)
        tools = READ_ONLY_TOOLS if task.read_only else task.allowed_tools
        prompt = task.prompt
        if task.context_text:
            prompt = f"<context>\n{task.context_text}\n</context>\n\n{prompt}"
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--allowedTools", ",".join(tools),
        ]
        if task.llm and task.llm.get("model"):
            cmd += ["--model", task.llm["model"]]
        if task.mcp_config:
            # pool MCP resources, granted to this project (SPEC §5.1)
            cmd += ["--mcp-config", task.mcp_config]
        env = run_env(task)
        gateway_url = task.gateway_url
        if task.isolation == "container":
            from .. import container

            container.ensure_available()  # fail loudly, never downgrade
            gateway_url = container.rewrite_gateway_url(gateway_url) if gateway_url else None
            git_common = subprocess.run(
                ["git", "-C", task.workdir, "rev-parse", "--path-format=absolute",
                 "--git-common-dir"], capture_output=True, text=True)
            cmd = container.wrap_command(cmd, container.ContainerSpec(
                workdir=task.workdir,
                image=task.container_image or container.DEFAULT_IMAGE,
                git_common_dir=(git_common.stdout.strip()
                                if git_common.returncode == 0 else None),
                env={"ANTHROPIC_BASE_URL": gateway_url or "",
                     "ANTHROPIC_AUTH_TOKEN": task.run_token or ""},
            ))
        if gateway_url:
            env["ANTHROPIC_BASE_URL"] = gateway_url
            env["ANTHROPIC_AUTH_TOKEN"] = task.run_token or ""
            env.pop("ANTHROPIC_API_KEY", None)
        handle.process = await asyncio.create_subprocess_exec(
            *cmd,
            limit=STREAM_LIMIT,     # a big tool result must not kill the run
            cwd=task.workdir,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,   # nothing may wait on a prompt
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(sys.platform != "win32"),  # own process group for clean kill
        )
        return handle

    async def stream(self, handle: ClaudeCodeHandle) -> AsyncIterator[RunEvent]:
        assert handle.process and handle.process.stdout
        deadline = asyncio.get_event_loop().time() + handle.task.timeout_s
        stderr_task = asyncio.create_task(self._drain_stderr(handle))
        try:
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    handle.timed_out = True
                    await self.cancel(handle)
                    return
                try:
                    raw = await asyncio.wait_for(handle.process.stdout.readline(),
                                                 timeout=min(remaining, 30))
                except TimeoutError:
                    continue  # no output this window; re-check the deadline
                except ValueError:
                    # one line longer than the reader's limit. asyncio has
                    # already discarded it and the stream recovers, so losing a
                    # progress line beats losing the run — which is what used to
                    # happen ("Separator is found, but chunk is longer than
                    # limit" killed a stage after minutes of real work).
                    log.warning("run %s: dropped an oversized output line",
                                handle.task.run_id)
                    continue
                if not raw:
                    return  # EOF
                event = parse_event(raw)
                if event is None:
                    continue
                etype = event.get("type")
                if etype == "assistant":
                    content = (event.get("message") or {}).get("content") or []
                    texts = [c.get("text", "") for c in content if c.get("type") == "text"]
                    if texts:
                        yield RunEvent("progress", {"text": " ".join(texts)[:500]})
                    tools = [c.get("name") for c in content if c.get("type") == "tool_use"]
                    for name in tools:
                        yield RunEvent("tool_call_summary", {"tool": name})
                elif etype == "result":
                    handle.result_event = event
                    yield RunEvent("usage", {"usage": event.get("usage") or {},
                                             "total_cost_usd": event.get("total_cost_usd")})
        finally:
            stderr_task.cancel()

    async def _drain_stderr(self, handle: ClaudeCodeHandle) -> None:
        assert handle.process and handle.process.stderr
        async for raw in handle.process.stderr:
            handle.stderr_tail.append(raw.decode(errors="replace").rstrip())
            del handle.stderr_tail[:-20]

    async def respond(self, handle: ClaudeCodeHandle, request_id: str, reply: dict) -> None:
        # `claude -p` is one-shot: no interaction channel. M4 moves to the Agent
        # SDK for canUseTool round-trips; unattended_policy covers M1.
        raise NotImplementedError("claude-code headless (-p) has no interaction channel")

    async def cancel(self, handle: ClaudeCodeHandle) -> None:
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

    async def result(self, handle: ClaudeCodeHandle) -> RunResult:
        process = handle.process
        if process and process.returncode is None:
            await process.wait()
        event = handle.result_event or {}
        usage = event.get("usage") or {}
        if handle.timed_out:
            status = "timeout"
        elif handle.cancelled:
            status = "cancelled"
        elif event.get("subtype") == "success" and not event.get("is_error"):
            # the result event says subtype "success" even for errors (e.g.
            # "Not logged in"); is_error is the authoritative flag
            status = "succeeded"
        else:
            status = "failed"
        error_tail = "\n".join(handle.stderr_tail[-5:])
        from ..workflow import read_verdict

        return RunResult(
            status=status,
            summary=str(event.get("result") or error_tail or "")[:SUMMARY_LIMIT],
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
            cache_read=int(usage.get("cache_read_input_tokens") or 0),
            cache_write=int(usage.get("cache_creation_input_tokens") or 0),
            cost_usd=float(event.get("total_cost_usd") or 0),
            precision="estimated" if (handle.timed_out or handle.cancelled) else "reported",
            structured_verdict=read_verdict(handle.task.workdir),
        )
