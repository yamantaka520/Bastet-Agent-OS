"""hermes executor (M4): NousResearch hermes-agent in oneshot mode.

Invocation: `hermes -z "<prompt>"`. With an assigned LLM resource, a
Bastet-managed HERMES_HOME profile routes inference through the gateway and
meters every token. Without one, Hermes keeps its own configured provider and
credentials, matching the subscription/direct path supported by other CLIs.
The gateway run token travels via an env var — never argv or the config file.

Notes from the interface survey (verified against local v0.12 source):
- `-z` prints only the final reply to stdout; exit 0 = success, 2 = bad args
- `-z` is hard-wired YOLO (auto-approves dangerous commands) — the effective
  permission boundary is the toolset allow-list + isolation, so read-only
  review runs are refused rather than pretended
- provider requires an explicit model; cwd controls AGENTS.md resolution
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

from ..workflow import read_verdict
from .base import (
    STREAM_LIMIT,
    SUMMARY_LIMIT,
    ProgressDeadline,
    RouteContract,
    RunEvent,
    RunResult,
    TaskSpec,
    register_builtin,
    run_env,
)

GRACE_SECONDS = 10
DEFAULT_TOOLSETS = "terminal"


log = logging.getLogger("bastet.executor")


@dataclass
class HermesHandle:
    task: TaskSpec
    process: asyncio.subprocess.Process | None = None
    stdout_text: str = ""
    stderr_tail: list[str] = field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False
    usage_file: Path | None = None

    def state(self) -> dict:
        return {"kind": "hermes", "run_id": self.task.run_id,
                "pid": self.process.pid if self.process else None,
                "usage_file": str(self.usage_file) if self.usage_file else None}


def write_profile(profile_dir: Path, gateway_url: str, model: str) -> None:
    """Materialize a HERMES_HOME whose only provider is the Bastet gateway."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "config.yaml").write_text(
        "providers:\n"
        "  bastet:\n"
        f"    base_url: {gateway_url}/v1\n"
        "    api_key_env: BASTET_RUN_TOKEN\n"
        "    api_mode: chat_completions\n"
        f"    model: {model}\n"
    )


@register_builtin
class HermesExecutor:
    kind = "hermes"
    capabilities = {"code", "light-task"}
    route_contract = RouteContract(
        gateway_flavors=frozenset({"openai"}), gateway_requires_model=True)

    async def start(self, task: TaskSpec) -> HermesHandle:
        if task.read_only:
            # `-z` is always YOLO; pretending it is read-only would be a lie
            raise ValueError("hermes executor does not support read-only review runs "
                             "(oneshot mode auto-approves everything)")
        gateway = bool(task.gateway_url or task.run_token)
        if gateway and (not task.gateway_url or not task.run_token):
            raise ValueError("hermes gateway path requires both URL and run token")
        if gateway and (not task.llm or not task.llm.get("model")):
            raise ValueError("hermes executor requires TaskSpec.llm = {flavor, model}")
        if gateway and task.llm.get("flavor") != "openai":
            raise ValueError("hermes speaks chat_completions — use an openai-flavor "
                             "resource")

        handle = HermesHandle(task=task)
        meta_dir = Path(task.workdir) / "._bastet"
        meta_dir.mkdir(parents=True, exist_ok=True)
        handle.usage_file = meta_dir / f"hermes-usage-{task.run_id}.json"

        prompt = task.prompt
        if task.context_text:
            prompt = f"<context>\n{task.context_text}\n</context>\n\n{prompt}"
        toolsets = (task.extra_env.get("BASTET_HERMES_TOOLSETS") or DEFAULT_TOOLSETS)
        cmd = ["hermes", "-z", prompt, "--usage-file", str(handle.usage_file),
               "-t", toolsets]
        env = run_env(task)
        if gateway:
            profile_dir = meta_dir / "hermes-home"
            write_profile(profile_dir, task.gateway_url, task.llm["model"])
            cmd += ["--provider", "bastet", "-m", task.llm["model"]]
            env.update(HERMES_HOME=str(profile_dir),
                       BASTET_RUN_TOKEN=task.run_token)
        elif task.llm and task.llm.get("model"):
            cmd += ["-m", task.llm["model"]]
        handle.process = await asyncio.create_subprocess_exec(
            *cmd,
            limit=STREAM_LIMIT,     # a big tool result must not kill the run
            cwd=task.workdir, env=env,
            stdin=asyncio.subprocess.DEVNULL,   # nothing may wait on a prompt
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=(sys.platform != "win32"))
        return handle

    async def stream(self, handle: HermesHandle) -> AsyncIterator[RunEvent]:
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
                    # a line longer than the reader's limit. asyncio has already
                    # discarded it and the stream recovers, so losing one progress line
                    # beats losing the run — which is what used to happen ("Separator is
                    # found, but chunk is longer than limit" killed a live stage).
                    log.warning("run %s: dropped an oversized output line",
                                handle.task.run_id)
                    continue
                if not raw:
                    return
                deadline.note_progress()
                line = raw.decode(errors="replace").rstrip()
                if line:
                    handle.stdout_text += line + "\n"
                    yield RunEvent("progress", {"text": line[:500]})
        finally:
            stderr_task.cancel()

    async def _drain_stderr(self, handle: HermesHandle) -> None:
        assert handle.process and handle.process.stderr
        async for raw in handle.process.stderr:
            handle.stderr_tail.append(raw.decode(errors="replace").rstrip())
            del handle.stderr_tail[:-20]

    async def respond(self, handle: HermesHandle, request_id: str, reply: dict) -> None:
        raise NotImplementedError("hermes oneshot has no interaction channel")

    async def cancel(self, handle: HermesHandle) -> None:
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

    async def result(self, handle: HermesHandle) -> RunResult:
        process = handle.process
        if process and process.returncode is None:
            await process.wait()
        if handle.timed_out:
            status = "timeout"
        elif handle.cancelled:
            status = "cancelled"
        elif process and process.returncode == 0:
            status = "succeeded"
        else:
            status = "failed"
        summary = handle.stdout_text.strip() or "\n".join(handle.stderr_tail[-5:])
        usage = {}
        try:
            if handle.usage_file and handle.usage_file.is_file():
                usage = json.loads(handle.usage_file.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            log.warning("run %s: Hermes usage report was unreadable", handle.task.run_id)

        def number(name: str, cast):
            try:
                return cast(usage.get(name) or 0)
            except (TypeError, ValueError):
                log.warning("run %s: invalid Hermes usage field %s",
                            handle.task.run_id, name)
                return cast(0)

        return RunResult(
            status=status,
            summary=summary[:SUMMARY_LIMIT],
            tokens_in=number("input_tokens", int),
            tokens_out=number("output_tokens", int),
            cache_read=number("cache_read_tokens", int),
            cache_write=number("cache_write_tokens", int),
            cost_usd=number("estimated_cost_usd", float),
            # Gateway ledger wins in orchestrator._finalize_run. Direct runs
            # retain Hermes's own usage report instead of pretending zero use.
            precision="estimated" if (handle.timed_out or handle.cancelled) else "reported",
            structured_verdict=read_verdict(handle.task.workdir),
        )
