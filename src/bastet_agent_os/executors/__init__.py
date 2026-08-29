"""Executor plugin layer (SPEC §5.1)."""

from . import (  # noqa: F401  (register builtins)
    agy,
    bastet_lite,
    claude_code,
    claude_sdk,
    codex,
    codex_app_server,
    grok,
    hermes,
    openclaw,
    pi_agent,
)
from .base import Executor, RunEvent, RunResult, TaskSpec, get_executor

__all__ = ["Executor", "RunEvent", "RunResult", "TaskSpec", "get_executor"]
