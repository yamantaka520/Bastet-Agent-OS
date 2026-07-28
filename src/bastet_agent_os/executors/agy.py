"""agy executor (Google Antigravity CLI) — `-p` print mode.

Interface facts (surveyed against the local 1.0.12 binary + embedded docs):
- `agy -p "<prompt>" --output-format json` runs one task and prints a single
  JSON envelope: {status: SUCCESS|ERROR|CANCELED|..., response, error,
  num_turns, usage:{input_tokens, output_tokens, thinking_tokens,
  cache_read_tokens, total_tokens}} — usage is right there (`reported`)
- auth is Google OAuth (~/.gemini); NO custom base URL exists, so a gateway
  path is impossible — assigning an LLM resource is refused honestly
- workdir = process cwd (this version has no --cwd); the workspace must be
  in settings.json trustedWorkspaces or agy exits with an auth-style error
- review runs: without --dangerously-skip-permissions headless tools are
  soft-denied (a de-facto read-only run — the diff travels in the prompt),
  and --json-schema forces the schema-enforced verdict
- exit codes: 0 ok, 1 runtime error (envelope still printed), 2 bad flags
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from .base import RunEvent, RunResult, TaskSpec, register_builtin

GRACE_SECONDS = 10

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "reject"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "comments": {"type": "string"},
    },
    "required": ["verdict"],
    "additionalProperties": False,
}


@dataclass
class AgyHandle:
    task: TaskSpec
    process: asyncio.subprocess.Process | None = None
    raw_stdout: str = ""
    stderr_tail: list[str] = field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False

    def state(self) -> dict:
        return {"kind": "agy", "run_id": self.task.run_id,
                "pid": self.process.pid if self.process else None}


@register_builtin
class AgyExecutor:
    kind = "agy"
    capabilities = {"code", "review", "light-task"}

    async def start(self, task: TaskSpec) -> AgyHandle:
        if task.gateway_url:
            raise ValueError(
                "agy (Antigravity) has no custom-endpoint support — it cannot route "
                "through the gateway; omit the resource (Google OAuth billing, "
                "reported precision)")
        handle = AgyHandle(task=task)
        prompt = task.prompt
        if task.context_text:
            prompt = f"<context>\n{task.context_text}\n</context>\n\n{prompt}"
        cmd = ["agy", "--output-format", "json",
               "--print-timeout", f"{task.timeout_s}s"]
        if task.read_only:
            # headless without skip-permissions soft-denies tools (de-facto
            # read-only); the verdict rides the schema-enforced output
            cmd += ["--json-schema", json.dumps(VERDICT_SCHEMA)]
        else:
            cmd += ["--dangerously-skip-permissions"]
        if task.llm and task.llm.get("model"):
            cmd += ["--model", task.llm["model"]]
        cmd += ["-p", prompt]

        handle.process = await asyncio.create_subprocess_exec(
            *cmd, cwd=task.workdir,
            env={**os.environ, **task.extra_env, "AGY_CLI_DISABLE_AUTO_UPDATE": "1"},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=(sys.platform != "win32"))
        return handle

    async def stream(self, handle: AgyHandle) -> AsyncIterator[RunEvent]:
        assert handle.process and handle.process.stdout
        deadline = asyncio.get_event_loop().time() + handle.task.timeout_s + 30
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
                    continue
                if not raw:
                    return
                handle.raw_stdout += raw.decode(errors="replace")
                yield RunEvent("progress", {"text": "…"})  # json mode: envelope at end
        finally:
            stderr_task.cancel()

    async def _drain_stderr(self, handle: AgyHandle) -> None:
        assert handle.process and handle.process.stderr
        async for raw in handle.process.stderr:
            handle.stderr_tail.append(raw.decode(errors="replace").rstrip())
            del handle.stderr_tail[:-20]

    async def respond(self, handle: AgyHandle, request_id: str, reply: dict) -> None:
        raise NotImplementedError("agy print mode has no interaction channel")

    async def cancel(self, handle: AgyHandle) -> None:
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

    async def result(self, handle: AgyHandle) -> RunResult:
        process = handle.process
        if process and process.returncode is None:
            await process.wait()

        envelope = _last_json_object(handle.raw_stdout) or {}
        usage = envelope.get("usage") or {}
        ok = (process is not None and process.returncode == 0
              and envelope.get("status") == "SUCCESS")
        if handle.timed_out:
            status = "timeout"
        elif handle.cancelled:
            status = "cancelled"
        elif ok:
            status = "succeeded"
        else:
            status = "failed"

        verdict = None
        if handle.task.read_only and envelope:
            data = envelope.get("structured_output")
            if not isinstance(data, dict):
                try:
                    data = json.loads(envelope.get("response") or "")
                except json.JSONDecodeError:
                    data = None
            if isinstance(data, dict) and data.get("verdict"):
                verdict = {"verdict": str(data["verdict"]).lower(),
                           "reasons": data.get("reasons") or []}

        return RunResult(
            status=status,
            summary=(str(envelope.get("response") or envelope.get("error") or "")
                     or "\n".join(handle.stderr_tail[-5:]))[:2000],
            tokens_in=int(usage.get("input_tokens") or 0),
            # thinking tokens are output-side, like codex's reasoning tokens
            tokens_out=(int(usage.get("output_tokens") or 0)
                        + int(usage.get("thinking_tokens") or 0)),
            cache_read=int(usage.get("cache_read_tokens") or 0),
            precision="estimated" if (handle.timed_out or handle.cancelled)
                      else "reported",
            structured_verdict=verdict,
        )


def _last_json_object(text: str) -> dict | None:
    for line in reversed(text.strip().splitlines()):
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None
