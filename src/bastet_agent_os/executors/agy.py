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
- read-only runs use plan mode + the CLI terminal sandbox. Tool approvals are
  auto-answered inside that boundary because headless mode cannot prompt; this
  lets PM/review tasks read context without granting edits or host shell access
- --json-schema forces the schema-enforced verdict on actual review gates
- exit codes: 0 ok, 1 runtime error (envelope still printed), 2 bad flags
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
    ProgressDeadline,
    RunEvent,
    RunResult,
    TaskSpec,
    last_json_object,
    parse_event,
    register_builtin,
    run_env,
)

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


log = logging.getLogger("bastet.executor")


def unwrap_envelope(obj: dict | None) -> dict:
    """The final envelope, whichever output format produced it.

    `--output-format json` ends with the envelope itself; `stream-json` ends
    with `{"event": "result", "result": {...}}`. Reading the wrapper as the
    envelope makes `status` None, which reads downstream as "every agy run
    failed" — so both shapes are accepted here, once."""
    if not isinstance(obj, dict):
        return {}
    if obj.get("event") == "result" and isinstance(obj.get("result"), dict):
        return obj["result"]
    return obj


def _progress_text(event: dict | None) -> str:
    """One streamed line → what to show a human waiting on this run.

    The agent's own words when it is talking (`text_delta`), otherwise the step
    it is working on — an empty string means "nothing worth a heartbeat", which
    keeps the board from beating on protocol noise."""
    if not event:
        return ""
    step = event.get("step_update")
    if not isinstance(step, dict):
        return ""
    delta = str(step.get("text_delta") or "").strip()
    if delta:
        return delta
    kind = str(step.get("step_type") or "").strip()
    if kind and kind != "unknown" and step.get("state") == "ACTIVE":
        return f"[{kind}]"
    return ""


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
        # stream-json, not json: `json` prints nothing until the process exits,
        # so a long stage looked dead on the board for its whole life (a live PM
        # stage ran 53 minutes with no sign of life). The schema still binds the
        # final result — `--json-schema` documents exactly that for stream mode.
        cmd = ["agy", "--output-format", "stream-json",
               "--print-timeout", f"{ProgressDeadline.hard_timeout_s(task.timeout_s)}s"]
        if task.expect_verdict:
            # review gates only — binding this schema to every read-only run
            # once left the PM decomposer able to answer nothing but a verdict
            cmd += ["--json-schema", json.dumps(VERDICT_SCHEMA)]
        if task.read_only:
            # Default headless plan mode still asks permission for `ls` and then
            # auto-denies it, producing no PM diagnosis. Plan mode preserves the
            # no-edit contract; --sandbox constrains terminal access; approval
            # only removes the impossible TTY prompt inside those boundaries.
            cmd += ["--mode", "plan", "--sandbox", "--add-dir", task.workdir,
                    "--dangerously-skip-permissions"]
        else:
            cmd += ["--dangerously-skip-permissions"]
        if task.llm and task.llm.get("model"):
            cmd += ["--model", task.llm["model"]]
        cmd += ["-p", prompt]

        handle.process = await asyncio.create_subprocess_exec(
            *cmd,
            # a big tool result must not kill the run
            limit=STREAM_LIMIT,
            cwd=task.workdir,
            env=run_env(task, AGY_CLI_DISABLE_AUTO_UPDATE="1"),
            stdin=asyncio.subprocess.DEVNULL,   # nothing may wait on a prompt
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=(sys.platform != "win32"))
        return handle

    async def stream(self, handle: AgyHandle) -> AsyncIterator[RunEvent]:
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
                line = raw.decode(errors="replace")
                handle.raw_stdout += line
                text = _progress_text(parse_event(line))
                if text:
                    yield RunEvent("progress", {"text": text[:500]})
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

        envelope = unwrap_envelope(last_json_object(handle.raw_stdout))
        usage = envelope.get("usage") or {}
        # the envelope outranks the exit code. agy flushes telemetry to Google
        # AFTER printing its result, and on a host with flaky egress that flush
        # fails — the process exits nonzero with a complete SUCCESS envelope in
        # hand. Live cost: 4 of 5 approval-prep stages in one day marked
        # "execution failed" over work that was finished and correct, each one
        # blocking a card and burning a PM intervention. The envelope is the
        # verified fact; the exit code after it is housekeeping.
        rc = process.returncode if process is not None else None
        ok = envelope.get("status") == "SUCCESS"
        if ok and rc != 0:
            log.warning("run %s: agy exited %s after a SUCCESS envelope "
                        "(post-response housekeeping failed); trusting the envelope",
                        handle.task.run_id, rc)
        if handle.timed_out:
            status = "timeout"
        elif handle.cancelled:
            status = "cancelled"
        elif ok:
            status = "succeeded"
        else:
            status = "failed"

        verdict = None
        if handle.task.expect_verdict and envelope:
            data = envelope.get("structured_output")
            if not isinstance(data, dict):
                data = last_json_object(str(envelope.get("response") or ""))
            if isinstance(data, dict) and data.get("verdict"):
                verdict = {"verdict": str(data["verdict"]).lower(),
                           "reasons": data.get("reasons") or []}

        summary = (str(envelope.get("response") or envelope.get("error") or "")
                   or "\n".join(handle.stderr_tail[-5:]))
        if status == "failed":
            # a failed run's record must lead with WHY — the old records held
            # only the (perfectly good) response text, so the actual failure
            # (status? exit code?) was unrecoverable after the fact
            summary = (f"[agy status={envelope.get('status') or 'no-envelope'} "
                       f"exit={rc}] {summary}")
        return RunResult(
            status=status,
            summary=summary[:SUMMARY_LIMIT],
            tokens_in=int(usage.get("input_tokens") or 0),
            # thinking tokens are output-side, like codex's reasoning tokens
            tokens_out=(int(usage.get("output_tokens") or 0)
                        + int(usage.get("thinking_tokens") or 0)),
            cache_read=int(usage.get("cache_read_tokens") or 0),
            precision="estimated" if (handle.timed_out or handle.cancelled)
                      else "reported",
            structured_verdict=verdict,
        )
