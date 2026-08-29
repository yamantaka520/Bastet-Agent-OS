"""claude-sdk executor (M4): Claude Code via the Agent SDK.

What the subprocess executor (`claude-code`) cannot do, this one can:
tool-permission requests pause the run (`interaction_request` event), a human
answers over the API / Telegram / UI, and the SDK's `can_use_tool` callback
resumes with allow/deny. Requires `pip install claude-agent-sdk` and an API
key path — the SDK does not support Max-subscription auth, so gateway
metering (`ANTHROPIC_BASE_URL` + run token) is the natural fit here.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..workflow import read_verdict
from .base import SUMMARY_LIMIT, RouteContract, RunEvent, RunResult, TaskSpec, register_builtin

INTERACTION_TIMEOUT_S = 600  # unattended fallback: deny after this long
# WebFetch/WebSearch are read-only by nature and reviewers/chat agents
# legitimately read docs — their absence surfaced as permission errors
READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "Write", "WebFetch", "WebSearch"]  # Write carries the verdict file

_SENTINEL = object()


def _sdk():
    try:
        import claude_agent_sdk
        return claude_agent_sdk
    except ImportError as exc:
        raise ValueError(
            "claude-sdk executor needs the Agent SDK: pip install claude-agent-sdk"
        ) from exc


@dataclass
class SdkHandle:
    task: TaskSpec
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    driver: asyncio.Task | None = None
    result_message: Any = None
    summary: str = ""
    error: str = ""
    cancelled: bool = False
    counter: int = 0

    def state(self) -> dict:
        return {"kind": "claude-sdk", "run_id": self.task.run_id}


@register_builtin
class ClaudeSdkExecutor:
    kind = "claude-sdk"
    capabilities = {"code", "review", "mcp", "interactive"}
    route_contract = RouteContract(gateway_flavors=frozenset({"anthropic"}))

    async def start(self, task: TaskSpec) -> SdkHandle:
        sdk = _sdk()
        handle = SdkHandle(task=task)

        async def can_use_tool(tool_name: str, input_data: dict, context: Any):
            from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

            handle.counter += 1
            request_id = f"perm_{handle.counter}"
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            handle.pending[request_id] = future
            handle.queue.put_nowait(RunEvent("interaction_request", {
                "request_id": request_id,
                "kind": "permission_request",
                "payload": {"tool": tool_name,
                            "input": json.dumps(input_data, ensure_ascii=False)[:500]},
            }))
            try:
                reply = await asyncio.wait_for(future, timeout=INTERACTION_TIMEOUT_S)
            except TimeoutError:
                return PermissionResultDeny(
                    message="denied: no human response (unattended policy)")
            finally:
                handle.pending.pop(request_id, None)
            if str(reply.get("behavior", "deny")).lower() == "allow":
                return PermissionResultAllow(updated_input=input_data)
            return PermissionResultDeny(message=reply.get("message", "denied by operator"))

        env = dict(task.extra_env)
        if task.gateway_url:
            env["ANTHROPIC_BASE_URL"] = task.gateway_url
            env["ANTHROPIC_AUTH_TOKEN"] = task.run_token or ""
        prompt = task.prompt
        if task.context_text:
            prompt = f"<context>\n{task.context_text}\n</context>\n\n{prompt}"

        options = sdk.ClaudeAgentOptions(
            cwd=task.workdir,
            allowed_tools=READ_ONLY_TOOLS if task.read_only else task.allowed_tools,
            can_use_tool=can_use_tool,
            env={**os.environ, **env},
            max_turns=50,
        )

        async def drive():
            try:
                async for message in sdk.query(prompt=prompt, options=options):
                    self._translate(handle, message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                handle.error = f"{type(exc).__name__}: {exc}"[:500]
            finally:
                handle.queue.put_nowait(_SENTINEL)

        handle.driver = asyncio.get_running_loop().create_task(drive())
        return handle

    def _translate(self, handle: SdkHandle, message: Any) -> None:
        name = type(message).__name__
        if name == "AssistantMessage":
            for block in getattr(message, "content", []) or []:
                text = getattr(block, "text", None)
                if text:
                    handle.summary = text
                    handle.queue.put_nowait(RunEvent("progress", {"text": text[:500]}))
                elif getattr(block, "name", None):
                    handle.queue.put_nowait(
                        RunEvent("tool_call_summary", {"tool": block.name}))
        elif name == "ResultMessage":
            handle.result_message = message

    async def stream(self, handle: SdkHandle) -> AsyncIterator[RunEvent]:
        while True:
            item = await handle.queue.get()
            if item is _SENTINEL:
                return
            yield item

    async def respond(self, handle: SdkHandle, request_id: str, reply: dict) -> None:
        future = handle.pending.get(request_id)
        if future is None or future.done():
            raise ValueError(f"no pending interaction {request_id!r}")
        future.set_result(reply)

    async def cancel(self, handle: SdkHandle) -> None:
        handle.cancelled = True
        if handle.driver and not handle.driver.done():
            handle.driver.cancel()
            handle.queue.put_nowait(_SENTINEL)

    async def result(self, handle: SdkHandle) -> RunResult:
        if handle.driver and not handle.driver.done():
            await asyncio.gather(handle.driver, return_exceptions=True)
        message = handle.result_message
        if handle.cancelled:
            status = "cancelled"
        elif message is not None and not getattr(message, "is_error", False):
            status = "succeeded"
        else:
            status = "failed"
        usage = dict(getattr(message, "usage", None) or {}) if message is not None else {}
        return RunResult(
            status=status,
            summary=(str(getattr(message, "result", "")) or handle.summary
                     or handle.error)[:SUMMARY_LIMIT],
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
            cache_read=int(usage.get("cache_read_input_tokens") or 0),
            cache_write=int(usage.get("cache_creation_input_tokens") or 0),
            cost_usd=float(getattr(message, "total_cost_usd", None) or 0),
            precision="estimated" if handle.cancelled else "reported",
            structured_verdict=read_verdict(handle.task.workdir),
        )
