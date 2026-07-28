"""Executor interface (SPEC §5.1.1).

Executors are plugins (entry point group: bastet.executors). Built-ins are
registered directly. Handle state must be JSON-serializable so the control
plane can persist it (runs.executor_handle_json) and re-attach after restart.
"""

from __future__ import annotations

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
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass
class RunEvent:
    type: str                            # progress|tool_call_summary|usage|artifact|interaction_request
    data: dict[str, Any] = field(default_factory=dict)


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
