"""grok executor (xAI Grok Build CLI) — headless `-p` mode.

Interface facts (surveyed against the local 0.2.67 binary + bundled docs):
- `-p "<prompt>" --cwd <dir>` runs one agentic task and exits; stdout stays
  clean (updates go to stderr)
- `--output-format streaming-json` emits NDJSON events: text / thought /
  end(stopReason, sessionId) / error — unknown types must be ignored
- review runs: `--tools "read_file,grep,list_dir"` gives a real read-only
  toolset, and `--json-schema` (implies one-shot json output) forces the
  final message into our verdict schema — the structured channel
- gateway path: it speaks OpenAI chat completions; GROK_MODELS_BASE_URL
  points at the gateway and XAI_API_KEY carries the run token (Bearer) —
  it probes {base_url}/models at startup, which the gateway serves
- headless output carries NO token usage — gateway metering is the only
  precise accounting; the direct path is honestly `estimated`
- exit codes: 0 ok, 1 error, 130 SIGINT, 143 SIGTERM; no built-in wall-clock
  timeout, so ours drives SIGTERM -> SIGKILL
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
)

GRACE_SECONDS = 10
MAX_TURNS = 40
READ_ONLY_TOOLS = "read_file,grep,list_dir"

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


log = logging.getLogger("bastet.executor")


@dataclass
class GrokHandle:
    task: TaskSpec
    process: asyncio.subprocess.Process | None = None
    summary: str = ""
    raw_stdout: str = ""
    failed_reason: str = ""
    session_id: str = ""
    stderr_tail: list[str] = field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False

    def state(self) -> dict:
        return {"kind": "grok", "run_id": self.task.run_id,
                "session_id": self.session_id,
                "pid": self.process.pid if self.process else None}


@register_builtin
class GrokExecutor:
    kind = "grok"
    capabilities = {"code", "review"}

    async def start(self, task: TaskSpec) -> GrokHandle:
        if task.gateway_url and (not task.llm or task.llm.get("flavor") != "openai"):
            raise ValueError("grok's gateway path needs an openai-flavor resource")
        handle = GrokHandle(task=task)

        prompt = task.prompt
        if task.context_text:
            prompt = f"<context>\n{task.context_text}\n</context>\n\n{prompt}"
        cmd = ["grok", "-p", prompt, "--cwd", task.workdir,
               "--max-turns", str(MAX_TURNS), "--no-auto-update"]
        if task.read_only:
            # real read-only toolset + schema-enforced verdict (one-shot json)
            cmd += ["--tools", READ_ONLY_TOOLS,
                    "--json-schema", json.dumps(VERDICT_SCHEMA),
                    "--output-format", "json"]
        else:
            cmd += ["--always-approve", "--output-format", "streaming-json"]
        if task.llm and task.llm.get("model"):
            cmd += ["-m", task.llm["model"]]

        env = run_env(task, GROK_DISABLE_AUTOUPDATER="1")
        if task.gateway_url:
            env["GROK_MODELS_BASE_URL"] = f"{task.gateway_url}/v1"
            env["XAI_API_KEY"] = task.run_token or ""

        handle.process = await asyncio.create_subprocess_exec(
            *cmd,
            # a big tool result must not kill the run
            limit=STREAM_LIMIT,
            cwd=task.workdir, env=env,
            stdin=asyncio.subprocess.DEVNULL,   # nothing may wait on a prompt
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=(sys.platform != "win32"))
        return handle

    async def stream(self, handle: GrokHandle) -> AsyncIterator[RunEvent]:
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
                line = raw.decode(errors="replace")
                handle.raw_stdout += line
                event = parse_event(line)
                if event is None:
                    continue  # prose, a fragment of pretty-printed JSON, or a
                              # bare value: one-shot output is parsed in result()
                etype = event.get("type")
                if etype == "text" and event.get("text"):
                    handle.summary += event["text"]
                    yield RunEvent("progress", {"text": event["text"][:500]})
                elif etype == "end":
                    handle.session_id = str(event.get("sessionId") or "")
                elif etype == "error":
                    handle.failed_reason = str(event.get("message", ""))[:500]
                # unknown event types are ignored by design (docs: non-exhaustive)
        finally:
            stderr_task.cancel()

    async def _drain_stderr(self, handle: GrokHandle) -> None:
        assert handle.process and handle.process.stderr
        async for raw in handle.process.stderr:
            handle.stderr_tail.append(raw.decode(errors="replace").rstrip())
            del handle.stderr_tail[:-20]

    async def respond(self, handle: GrokHandle, request_id: str, reply: dict) -> None:
        raise NotImplementedError("grok -p has no interaction channel "
                                  "(ACP via `grok agent stdio` is future work)")

    async def cancel(self, handle: GrokHandle) -> None:
        handle.cancelled = True
        process = handle.process
        if process is None or process.returncode is not None:
            return
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)  # session survives
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

    async def result(self, handle: GrokHandle) -> RunResult:
        process = handle.process
        if process and process.returncode is None:
            await process.wait()
        verdict = None
        summary = handle.summary

        if handle.task.read_only:
            # one-shot json mode: {"text": "<schema-constrained JSON>", ...}
            payload = last_json_object(handle.raw_stdout)
            if payload and payload.get("type") == "error":
                handle.failed_reason = str(payload.get("message", ""))[:500]
            elif payload:
                summary = str(payload.get("text") or "")
                # the schema-constrained answer, however the model packaged it:
                # two objects back to back, prose around it, or ``` fences
                data = last_json_object(summary)
                if data and data.get("verdict"):
                    verdict = {"verdict": str(data["verdict"]).lower(),
                               "reasons": data.get("reasons") or []}

        if handle.timed_out:
            status = "timeout"
        elif handle.cancelled:
            status = "cancelled"
        elif process and process.returncode == 0 and not handle.failed_reason:
            status = "succeeded"
        else:
            status = "failed"
        return RunResult(
            status=status,
            summary=(summary or handle.failed_reason
                     or "\n".join(handle.stderr_tail[-5:]))[:SUMMARY_LIMIT],
            # headless output has no usage; the gateway ledger is the only
            # precise source — direct-path numbers would be a guess
            precision="estimated",
            structured_verdict=verdict,
        )


