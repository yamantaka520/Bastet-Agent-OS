"""Executor interface (SPEC §5.1.1).

Executors are plugins (entry point group: bastet.executors). Built-ins are
registered directly. Handle state must be JSON-serializable so the control
plane can persist it (runs.executor_handle_json) and re-attach after restart.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Protocol


@dataclass
class TaskSpec:
    run_id: str
    prompt: str
    workdir: str
    timeout_s: int = 3600
    allowed_tools: list[str] = field(default_factory=lambda: ["Read", "Edit", "Write", "Bash"])
    read_only: bool = False              # reviewer runs: no writes, no arbitrary shell
    unattended_policy: str = "deny"      # deny|timeout — default reply to interaction_request
    context_text: str = ""               # assembled context pack (§5.6)
    gateway_url: str | None = None       # None => subscription/direct path ("reported")
    run_token: str | None = None
    llm: dict | None = None              # {"flavor": "anthropic|openai", "model": ...} for in-process executors
    isolation: str = "worktree"          # worktree|container (SPEC §5.4.3)
    container_image: str | None = None   # image for isolation=container
    extra_env: dict[str, str] = field(default_factory=dict)
    mcp_config: str | None = None        # path to an mcpServers JSON (pool resources)


@dataclass
class RunEvent:
    type: str                            # progress|tool_call_summary|usage|artifact|interaction_request
    data: dict[str, Any] = field(default_factory=dict)


def parse_event(raw: bytes | str) -> dict[str, Any] | None:
    """One streamed line → an event dict, or None.

    `json.loads` happily returns a str, list or number, and pretty-printed output
    puts bare values on their own lines (`    "some reason"` inside an array).
    Calling `.get` on those raised AttributeError mid-stream and killed the run —
    so the type check belongs here, once, not in every executor."""
    try:
        value = json.loads(raw.decode(errors="replace") if isinstance(raw, bytes)
                           else raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def last_json_object(text: str) -> dict[str, Any] | None:
    """The last JSON object in a CLI's output, however it was formatted.

    Line-delimited (`{...}\n{...}`), pretty-printed across many lines, or
    wrapped in prose/``` fences — a parser that only understood one object per
    line silently found nothing in pretty-printed output, which read downstream
    as "the reviewer produced no verdict"."""
    if not text or not text.strip():
        return None
    try:                                   # the common case: the whole thing
        whole = json.loads(text)
        if isinstance(whole, dict):
            return whole
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            found = value                  # keep scanning: we want the last one
    return found


# The summary is not a label: chat replies and PM task plans ARE this string,
# so a small cap silently corrupts them (a cut-off JSON list parses as nothing).
# Still bounded, to keep a runaway agent out of the DB.
SUMMARY_LIMIT = 200_000


@dataclass
class RunResult:
    status: str                          # succeeded|failed|cancelled|timeout
    summary: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    precision: str = "reported"          # what the executor itself can vouch for
    structured_verdict: dict[str, Any] | None = None  # gate channel (§5.4.2)


class Executor(Protocol):
    kind: str
    capabilities: set[str]

    async def start(self, task: TaskSpec) -> Any: ...                      # -> RunHandle
    def stream(self, handle: Any) -> AsyncIterator[RunEvent]: ...
    async def respond(self, handle: Any, request_id: str, reply: dict) -> None: ...
    async def cancel(self, handle: Any) -> None: ...
    async def result(self, handle: Any) -> RunResult: ...


_BUILTINS: dict[str, type] = {}


def register_builtin(cls: type) -> type:
    _BUILTINS[cls.kind] = cls
    return cls


def get_executor(kind: str) -> Executor:
    if kind in _BUILTINS:
        return _BUILTINS[kind]()
    for ep in entry_points(group="bastet.executors"):
        if ep.name == kind:
            return ep.load()()
    raise KeyError(f"unknown executor kind: {kind!r}")
