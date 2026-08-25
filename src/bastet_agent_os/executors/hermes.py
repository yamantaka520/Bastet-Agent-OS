"""hermes executor (M4): NousResearch hermes-agent in oneshot mode.

Invocation: `hermes -z "<prompt>"` with a Bastet-managed HERMES_HOME profile
whose config.yaml routes inference to our gateway (named provider), so every
token is metered (`gateway` precision). The run token travels via an env var
the provider entry references — never argv, never the config file.

Notes from the interface survey (verified against local v0.12 source):
- `-z` prints only the final reply to stdout; exit 0 = success, 2 = bad args
- `-z` is hard-wired YOLO (auto-approves dangerous commands) — the effective
  permission boundary is the toolset allow-list + isolation, so read-only
  review runs are refused rather than pretended
- provider requires an explicit model; cwd controls AGENTS.md resolution
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

from ..workflow import read_verdict
from .base import (
    STREAM_LIMIT,
    SUMMARY_LIMIT,
    ProgressDeadline,
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

    def state(self) -> dict:
        return {"kind": "hermes", "run_id": self.task.run_id,
                "pid": self.process.pid if self.process else None}


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

    async def start(self, task: TaskSpec) -> HermesHandle:
        if task.read_only:
            # `-z` is always YOLO; pretending it is read-only would be a lie
            raise ValueError("hermes executor does not support read-only review runs "
                             "(oneshot mode auto-approves everything)")
        if not task.gateway_url or not task.run_token:
            raise ValueError("hermes executor requires a gateway path "
                             "(assign an LLM resource)")
        if not task.llm or not task.llm.get("model"):
            raise ValueError("hermes executor requires TaskSpec.llm = {flavor, model}")
        if task.llm.get("flavor") != "openai":
            raise ValueError("hermes speaks chat_completions — use an openai-flavor "
                             "resource")

        handle = HermesHandle(task=task)
        profile_dir = Path(task.workdir) / "._bastet" / "hermes-home"
        write_profile(profile_dir, task.gateway_url, task.llm["model"])

        prompt = task.prompt
        if task.context_text:
            prompt = f"<context>\n{task.context_text}\n</context>\n\n{prompt}"
        toolsets = (task.extra_env.get("BASTET_HERMES_TOOLSETS") or DEFAULT_TOOLSETS)
        cmd = ["hermes", "-z", prompt,
               "--provider", "bastet", "-m", task.llm["model"],
               "-t", toolsets]
        env = run_env(task, HERMES_HOME=str(profile_dir),
                      BASTET_RUN_TOKEN=task.run_token)
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
        return RunResult(
            status=status,
            summary=summary[:SUMMARY_LIMIT],
            # tokens/cost come from the gateway ledger (the only inference path)
            precision="estimated" if (handle.timed_out or handle.cancelled) else "reported",
            structured_verdict=read_verdict(handle.task.workdir),
        )
