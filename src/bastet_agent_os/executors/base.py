"""Executor interface (SPEC §5.1.1).

Executors are plugins (entry point group: bastet.executors). Built-ins are
registered directly. Handle state must be JSON-serializable so the control
plane can persist it (runs.executor_handle_json) and re-attach after restart.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol


@dataclass
class TaskSpec:
    run_id: str
    prompt: str
    workdir: str
    timeout_s: int = 3600
    allowed_tools: list[str] = field(default_factory=lambda: ["Read", "Edit", "Write", "Bash"])
    read_only: bool = False              # tool restriction: no writes, no arbitrary shell
    # a separate fact from read_only, learned the hard way: PM decomposition is
    # a read-only run whose ANSWER is a task list. Executors that bound their
    # verdict schema to read_only forced `{verdict, reasons, comments}` onto it,
    # so the agent could only reject — "no usable tasks in the decomposition",
    # every time, for any card format. Only agent-review gates expect a verdict.
    expect_verdict: bool = False
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
    # line-delimited output (every `stream-json` mode): the last complete line
    # IS the envelope. The character scan below cannot be used for this — it
    # keeps the last object to *start*, which inside `{"result":{...,"usage":
    # {...}}}` is the nested usage dict, so a successful run read as "no status
    # => failed". Caught by replaying a real agy transcript.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
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

# asyncio's StreamReader defaults to a 64 KiB line limit, and every CLI executor
# reads its `stream-json` output one line at a time. A single line carrying a
# large tool result — a file read, a long diff, a test log — overruns it, and
# `readline()` then raises LimitOverrunError, killing a run that was working
# perfectly. Seen live: a CatsWalker stage died with "Separator is found, but
# chunk is longer than limit" after several minutes of real work.
#
# The reader has to hold the largest single line an agent can emit, which is
# bounded by the tool output it decides to print, not by anything we control.
STREAM_LIMIT = 32 * 1024 * 1024


# A headless run has nobody to answer a prompt, so nothing it spawns may ask.
# Live incident: a PM stage ran `npm exec playwright --version`; npx wanted to
# install the package first and asked "Ok to proceed? (y)". Its stdin was a tty,
# so it waited — 52 minutes, 2 seconds of CPU, the agent blocked on its own
# child, the whole card frozen behind a question no human would ever see.
#
# Two halves, both needed: the env vars stop the well-behaved tools from asking,
# and stdin=DEVNULL (below) means anything that asks anyway reads EOF and fails
# fast instead of hanging. Both are inherited by grandchildren, which is the
# point — we do not control what the agent decides to run.
NONINTERACTIVE_ENV = {
    "CI": "1",                      # near-universal "nobody is watching" signal
    "npm_config_yes": "true",       # npx: install without asking
    "NPM_CONFIG_YES": "true",
    "GIT_TERMINAL_PROMPT": "0",     # git: fail instead of asking for credentials
    "DEBIAN_FRONTEND": "noninteractive",
    "PIP_NO_INPUT": "1",
    "PYTHONUNBUFFERED": "1",        # progress lines arrive while they still matter
}


def worktree_git_dir(workdir: str) -> str | None:
    """Where a git worktree keeps its index/HEAD — which is NOT inside it.

    A linked worktree's `.git` is a file reading `gitdir: <main repo>/.git/
    worktrees/<name>`, so every git WRITE lands in the main repository. An
    executor that sandboxes writes to the workspace therefore cannot commit,
    stash, or even refresh the index cache, and says so in ways that read like
    a broken disk: "cannot lock ref 'ORIG_HEAD': Read-only file system", or
    "cannot create .git/worktrees/<job>/index.lock". The directory is perfectly
    writable — it is just outside the sandbox. Sandboxed executors must be told
    about it explicitly."""
    marker = Path(workdir) / ".git"
    try:
        if not marker.is_file():
            return None                      # a normal repo, or not a repo
        text = marker.read_text(errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    path = text.split(":", 1)[1].strip()
    return path if path else None


def run_env(task: TaskSpec, **extra: str) -> dict[str, str]:
    """The environment every CLI executor spawns with.

    `extra_env` (pool credentials) wins over our defaults; the non-interactive
    block is applied first so a resource can still override it deliberately."""
    return {**os.environ, **NONINTERACTIVE_ENV, **task.extra_env, **extra}


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
