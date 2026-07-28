"""Executor plugin layer (SPEC §5.1)."""

from . import bastet_lite, claude_code  # noqa: F401  (register builtins)
from .base import Executor, RunEvent, RunResult, TaskSpec, get_executor

__all__ = ["Executor", "RunEvent", "RunResult", "TaskSpec", "get_executor"]
