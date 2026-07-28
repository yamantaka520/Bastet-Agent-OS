"""Executor plugin layer (SPEC §5.1)."""

from . import claude_code  # noqa: F401  (registers the builtin)
from .base import Executor, RunEvent, RunResult, TaskSpec, get_executor

__all__ = ["Executor", "RunEvent", "RunResult", "TaskSpec", "get_executor"]
