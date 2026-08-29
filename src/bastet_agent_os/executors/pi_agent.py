"""Pi coding-agent executor — ephemeral JSONL mode with explicit tools.

Pi is deliberately run without project packages, extensions, skills, prompt
templates, or saved sessions.  Bastet owns context assembly and isolation;
letting a repository silently extend the executor would bypass both contracts.
Direct runs use the selected Pi account profile.  Gateway runs receive a
temporary ``models.json`` whose credential is an environment reference, never
the run token itself.
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
    parse_event,
    register_builtin,
    run_env,
)

GRACE_SECONDS = 10
READ_ONLY_TOOLS = "read,grep,find,ls"
WRITE_TOOLS = "read,bash,edit,write,grep,find,ls"

log = logging.getLogger("bastet.executor")


def _message_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )


def _write_gateway_profile(profile_dir: Path, task: TaskSpec) -> None:
    assert task.gateway_url and task.run_token and task.llm
    flavor = task.llm["flavor"]
    api = "openai-completions" if flavor == "openai" else "anthropic-messages"
    base_url = (f"{task.gateway_url}/v1" if flavor == "openai"
                else task.gateway_url)
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile = {
        "providers": {
            "bastet": {
                "baseUrl": base_url,
                "api": api,
                "apiKey": "$BASTET_RUN_TOKEN",
                "models": [{"id": task.llm["model"]}],
            }
        }
    }
    (profile_dir / "models.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n")


@dataclass
class PiHandle:
    task: TaskSpec
    process: asyncio.subprocess.Process | None = None
    summary: str = ""
    raw_stdout: str = ""
    failed_reason: str = ""
    session_id: str = ""
    usage: dict = field(default_factory=dict)
    stderr_tail: list[str] = field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False

    def state(self) -> dict:
        return {"kind": "pi", "run_id": self.task.run_id,
                "session_id": self.session_id,
                "pid": self.process.pid if self.process else None}


@register_builtin
class PiExecutor:
    kind = "pi"
    capabilities = {"code", "review", "light-task"}
    route_contract = RouteContract(
        gateway_flavors=frozenset({"openai", "anthropic"}),
        gateway_requires_model=True)

    async def start(self, task: TaskSpec) -> PiHandle:
        gateway = bool(task.gateway_url or task.run_token)
        if gateway and (not task.gateway_url or not task.run_token):
            raise ValueError("pi gateway path requires both URL and run token")
        if gateway and (not task.llm or task.llm.get("flavor") not in
                        {"openai", "anthropic"} or not task.llm.get("model")):
            raise ValueError("pi gateway path requires an openai/anthropic model")

        handle = PiHandle(task=task)
        prompt = task.prompt
        if task.context_text:
            prompt = f"<context>\n{task.context_text}\n</context>\n\n{prompt}"
        cmd = [
            "pi", "--mode", "json", "--no-session", "--no-approve",
            "--no-extensions", "--no-skills", "--no-prompt-templates",
            "--no-themes", "--no-context-files",
            "--tools", READ_ONLY_TOOLS if task.read_only else WRITE_TOOLS,
        ]
        env = run_env(task, PI_SKIP_VERSION_CHECK="1", PI_TELEMETRY="0")
        if gateway:
            profile_dir = Path(task.workdir) / "._bastet" / "pi-agent"
            _write_gateway_profile(profile_dir, task)
            env.update(PI_CODING_AGENT_DIR=str(profile_dir),
                       BASTET_RUN_TOKEN=task.run_token or "")
            cmd += ["--provider", "bastet", "--model", task.llm["model"]]
        elif task.llm and task.llm.get("model"):
            cmd += ["--model", task.llm["model"]]
        cmd += ["--", prompt]

        handle.process = await asyncio.create_subprocess_exec(
            *cmd,
            limit=STREAM_LIMIT,
            cwd=task.workdir, env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=(sys.platform != "win32"))
        return handle

    async def stream(self, handle: PiHandle) -> AsyncIterator[RunEvent]:
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
                    log.warning("run %s: dropped an oversized Pi output line",
                                handle.task.run_id)
                    continue
                if not raw:
                    return
                deadline.note_progress()
                line = raw.decode(errors="replace")
                handle.raw_stdout += line
                event = parse_event(line)
                if event is None:
                    continue
                etype = event.get("type")
                if etype == "session":
                    handle.session_id = str(event.get("id") or "")
                elif etype == "message_update":
                    if isinstance(event.get("usage"), dict):
                        handle.usage = event["usage"]
                    update = event.get("assistantMessageEvent") or {}
                    delta = str(update.get("delta") or "")
                    if update.get("type") == "text_delta" and delta:
                        handle.summary += delta
                        yield RunEvent("progress", {"text": delta[:500]})
                    else:
                        yield RunEvent("activity", {"kind": str(
                            update.get("type") or "message_update")[:80]})
                elif etype == "message_end":
                    message = event.get("message") or {}
                    text = _message_text(message)
                    if text:
                        handle.summary = text
                    if isinstance(message.get("usage"), dict):
                        handle.usage = message["usage"]
                    if message.get("stopReason") in {"error", "aborted"}:
                        handle.failed_reason = str(
                            message.get("errorMessage") or message.get("stopReason"))
                elif etype and etype.startswith("tool_execution_"):
                    yield RunEvent("tool_call_summary", {
                        "tool": str(event.get("toolName") or "")[:120],
                        "state": etype.removeprefix("tool_execution_")})
                elif etype:
                    yield RunEvent("activity", {"kind": str(etype)[:80]})
        finally:
            stderr_task.cancel()

    async def _drain_stderr(self, handle: PiHandle) -> None:
        assert handle.process and handle.process.stderr
        async for raw in handle.process.stderr:
            handle.stderr_tail.append(raw.decode(errors="replace").rstrip())
            del handle.stderr_tail[:-20]

    async def respond(self, handle: PiHandle, request_id: str, reply: dict) -> None:
        raise NotImplementedError("pi JSON mode has no interaction channel")

    async def cancel(self, handle: PiHandle) -> None:
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

    async def result(self, handle: PiHandle) -> RunResult:
        process = handle.process
        if process and process.returncode is None:
            await process.wait()
        if handle.timed_out:
            status = "timeout"
        elif handle.cancelled:
            status = "cancelled"
        elif process and process.returncode == 0 and not handle.failed_reason:
            status = "succeeded"
        else:
            status = "failed"
        usage = handle.usage
        cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
        verdict = None
        if handle.task.expect_verdict:
            data = last_json_object(handle.summary)
            if isinstance(data, dict) and data.get("verdict"):
                verdict = {"verdict": str(data["verdict"]).lower(),
                           "reasons": data.get("reasons") or []}
        return RunResult(
            status=status,
            summary=(handle.summary or handle.failed_reason
                     or "\n".join(handle.stderr_tail[-5:]))[:SUMMARY_LIMIT],
            tokens_in=int(usage.get("input") or 0),
            tokens_out=int(usage.get("output") or 0),
            cache_read=int(usage.get("cacheRead") or 0),
            cache_write=int(usage.get("cacheWrite") or 0),
            cost_usd=float(cost.get("total") or 0),
            precision="reported",
            structured_verdict=verdict,
        )
