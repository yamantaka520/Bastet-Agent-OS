"""codex executor (M4): OpenAI Codex CLI in non-interactive mode.

`codex exec --json` emits JSONL events on stdout: the final text arrives as an
`item.completed` agent_message, token usage in `turn.completed` (including
cached/cache-write splits), failures as `turn.failed`.

Accounting: current Codex only speaks the Responses API (`wire_api =
"responses"`; chat completions was removed upstream in early 2026). The
gateway proxies `/v1/responses`, so a gateway path is fully metered: we
inject a model provider via `-c` overrides pointing at the gateway, with the
run token travelling through an env var (never argv or a config file).
Without a resource, codex uses its own auth (`reported` precision).

Review runs: codex's read-only sandbox cannot write the verdict file, so the
structured verdict travels via `--output-schema` instead — the CLI forces the
final message to match our verdict JSON schema, which we parse from the
`--output-last-message` file. Schema-enforced JSON, never free prose.
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
from typing import Any

from ..pricing import Usage
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
    worktree_git_dir,
)

GRACE_SECONDS = 10

# OpenAI's strict structured-output validation requires `required` to list
# EVERY key in `properties` at every object level (optionality is expressed by
# a null-union, not by omission). With only ["verdict"] listed, the vendor
# started rejecting the request outright — `invalid_json_schema` — which killed
# every codex review run before the model saw a single token. "Always present,
# possibly empty" costs the model two tokens and satisfies the validator.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "reject"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "comments": {"type": "string"},
    },
    "required": ["verdict", "reasons", "comments"],
    "additionalProperties": False,
}


log = logging.getLogger("bastet.executor")


@dataclass
class CodexHandle:
    task: TaskSpec
    process: asyncio.subprocess.Process | None = None
    usage: Usage = field(default_factory=Usage)
    summary: str = ""
    failed_reason: str = ""
    last_message_path: Path | None = None
    stderr_tail: list[str] = field(default_factory=list)
    stderr_task: Any = None
    timed_out: bool = False
    cancelled: bool = False

    def state(self) -> dict:
        return {"kind": "codex", "run_id": self.task.run_id,
                "pid": self.process.pid if self.process else None}


@register_builtin
class CodexExecutor:
    kind = "codex"
    capabilities = {"code", "review"}
    route_contract = RouteContract(gateway_flavors=frozenset({"openai"}))

    async def start(self, task: TaskSpec) -> CodexHandle:
        if task.gateway_url and (not task.llm or task.llm.get("flavor") != "openai"):
            raise ValueError("codex's gateway path needs an openai-flavor resource "
                             "(the gateway serves /v1/responses for it)")
        handle = CodexHandle(task=task)
        meta_dir = Path(task.workdir) / "._bastet"
        meta_dir.mkdir(parents=True, exist_ok=True)
        handle.last_message_path = meta_dir / "codex-last-message.txt"

        prompt = task.prompt
        if task.context_text:
            prompt = f"<context>\n{task.context_text}\n</context>\n\n{prompt}"
        cmd = [
            "codex", "exec",
            "--cd", task.workdir,
            "--sandbox", "read-only" if task.read_only else "workspace-write",
            "--skip-git-repo-check",
            "--ephemeral",
            "--json",
            "-o", str(handle.last_message_path),
        ]
        # a linked worktree's git metadata lives in the MAIN repo, outside the
        # sandboxed workspace — without this, every git write inside the
        # worktree fails and the agent reports a read-only filesystem
        git_dir = worktree_git_dir(task.workdir)
        if git_dir and not task.read_only:
            cmd += ["--add-dir", git_dir]
        if task.expect_verdict:
            # ONLY for review gates: the schema binds the whole answer, so on a
            # non-review read-only run (PM decomposition) it would leave the
            # agent no way to say anything but a verdict
            schema_path = meta_dir / "verdict-schema.json"
            schema_path.write_text(json.dumps(VERDICT_SCHEMA))
            cmd += ["--output-schema", str(schema_path)]
        env = run_env(task)
        if task.gateway_url:
            # route inference through the gateway: Responses wire, token via env
            cmd += [
                "-c", f'model_providers.bastet.base_url="{task.gateway_url}/v1"',
                "-c", 'model_providers.bastet.env_key="BASTET_RUN_TOKEN"',
                "-c", 'model_providers.bastet.wire_api="responses"',
                "-c", 'model_provider="bastet"',
            ]
            env["BASTET_RUN_TOKEN"] = task.run_token or ""
        if task.llm and task.llm.get("model"):
            cmd += ["-m", task.llm["model"]]
        cmd.append(prompt)

        handle.process = await asyncio.create_subprocess_exec(
            *cmd,
            # a big tool result must not kill the run
            limit=STREAM_LIMIT,
            cwd=task.workdir, env=env,
            stdin=asyncio.subprocess.DEVNULL,   # nothing may wait on a prompt
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=(sys.platform != "win32"))
        return handle

    async def _drain_stderr(self, handle: CodexHandle) -> None:
        """codex writes startup failures (bad cwd, missing auth, config errors)
        to stderr only; without this a failed run reports nothing at all."""
        stream = handle.process.stderr if handle.process else None
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if text:
                handle.stderr_tail.append(text)
                del handle.stderr_tail[:-20]

    async def stream(self, handle: CodexHandle) -> AsyncIterator[RunEvent]:
        assert handle.process and handle.process.stdout
        stderr_task = asyncio.get_event_loop().create_task(self._drain_stderr(handle))
        handle.stderr_task = stderr_task
        deadline = ProgressDeadline(handle.task.timeout_s)
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
            event = parse_event(raw)
            if event is None:
                continue
            etype = event.get("type", "")
            if etype == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    handle.summary = item["text"]
                    yield RunEvent("progress", {"text": item["text"][:500]})
                elif item.get("type") == "command_execution":
                    yield RunEvent("tool_call_summary",
                                   {"tool": (item.get("command") or "command")[:80]})
            elif etype == "turn.completed":
                u = event.get("usage") or {}
                cached = int(u.get("cached_input_tokens") or 0)
                handle.usage.add(Usage(
                    tokens_in=max(0, int(u.get("input_tokens") or 0) - cached),
                    tokens_out=(int(u.get("output_tokens") or 0)
                                + int(u.get("reasoning_output_tokens") or 0)),
                    cache_read=cached,
                    cache_write=int(u.get("cache_write_input_tokens") or 0),
                ))
            elif etype == "turn.failed":
                handle.failed_reason = str((event.get("error") or {}).get("message",
                                                                          ""))[:500]
            elif etype == "error":
                handle.failed_reason = str(event.get("message", ""))[:500]

    async def respond(self, handle: CodexHandle, request_id: str, reply: dict) -> None:
        raise NotImplementedError("codex exec has no interaction channel")

    async def cancel(self, handle: CodexHandle) -> None:
        handle.cancelled = True
        process = handle.process
        if process is None or process.returncode is not None:
            return
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(process.pid), signal.SIGINT)  # graceful interrupt
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

    async def result(self, handle: CodexHandle) -> RunResult:
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

        verdict = None
        if handle.task.expect_verdict and handle.last_message_path and \
                handle.last_message_path.exists():
            try:  # schema-enforced JSON final message = the structured channel
                data = last_json_object(handle.last_message_path.read_text())
                if data and data.get("verdict"):
                    verdict = {"verdict": str(data["verdict"]).lower(),
                               "reasons": data.get("reasons") or []}
            except OSError:
                verdict = None  # unreadable => missing => gate rejects

        return RunResult(
            status=status,
            summary=(handle.summary or handle.failed_reason
                     or ("\n".join(handle.stderr_tail[-5:]) if status == "failed"
                         else ""))[:SUMMARY_LIMIT],
            tokens_in=handle.usage.tokens_in,
            tokens_out=handle.usage.tokens_out,
            cache_read=handle.usage.cache_read,
            cache_write=handle.usage.cache_write,
            precision="estimated" if (handle.timed_out or handle.cancelled)
                      else "reported",
            structured_verdict=verdict,
        )
