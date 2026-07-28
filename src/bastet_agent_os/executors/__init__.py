"""Executor plugin layer (SPEC §5.1)."""

from . import (  # noqa: F401  (register builtins)
    agy,
    bastet_lite,
    claude_code,
    claude_sdk,
    codex,
    grok,
    hermes,
)
from .base import Executor, RunEvent, RunResult, TaskSpec, get_executor

__all__ = ["Executor", "RunEvent", "RunResult", "TaskSpec", "get_executor"]
