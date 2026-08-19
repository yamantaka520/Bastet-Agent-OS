"""The verdict schema reaches review gates only.

Live incident: the user made Codex1 the PM. Decomposition is a read-only run,
and every executor bound its verdict schema to `read_only` — so the PM's whole
answer was forced into `{verdict, reasons, comments}`. The agent could not emit
a task list at all and answered, reasonably:

    {"verdict":"reject","reasons":["The requested output schema requires a
     tasks array, but the active response schema only permits verdict,
     reasons, and comments."]}

which surfaced as "no usable tasks in the decomposition" — for every card
format, three times in a row, until the human gave up. read_only is a tool
restriction; expecting a verdict is a property of the agent-review gate. These
tests hold the two apart.
"""

import inspect

from bastet_agent_os import orchestrator as orch_mod
from bastet_agent_os.executors.agy import AgyExecutor
from bastet_agent_os.executors.base import TaskSpec
from bastet_agent_os.executors.codex import CodexExecutor
from bastet_agent_os.executors.grok import GrokExecutor
from bastet_agent_os.project_runner import decompose


def _plan_spec(tmp_path) -> TaskSpec:
    """Exactly the shape project_runner.decompose builds: read-only, no verdict."""
    return TaskSpec(run_id="plan1", prompt="split this project into tasks",
                    workdir=str(tmp_path), read_only=True)


def _review_spec(tmp_path) -> TaskSpec:
    return TaskSpec(run_id="rev1", prompt="review this diff",
                    workdir=str(tmp_path), read_only=True, expect_verdict=True)


class _FakeProc:
    pid = 4242
    returncode = None
    stdout = None
    stderr = None


async def _argv_of(executor, task, monkeypatch) -> list[str]:
    """Run start() against a captured spawn — the argv is the contract."""
    seen: dict = {}

    async def fake_spawn(*cmd, **kwargs):
        seen["cmd"] = list(cmd)
        return _FakeProc()

    import asyncio
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    await executor.start(task)
    return seen["cmd"]


async def test_codex_decomposition_is_not_schema_bound(tmp_path, monkeypatch):
    argv = await _argv_of(CodexExecutor(), _plan_spec(tmp_path), monkeypatch)
    assert "--output-schema" not in argv, \
        "a PM decomposition forced into the verdict schema can only reject"
    assert "read-only" in argv               # the sandbox restriction remains


async def test_codex_review_is_schema_bound(tmp_path, monkeypatch):
    argv = await _argv_of(CodexExecutor(), _review_spec(tmp_path), monkeypatch)
    assert "--output-schema" in argv and "read-only" in argv


async def test_agy_decomposition_is_not_schema_bound(tmp_path, monkeypatch):
    argv = await _argv_of(AgyExecutor(), _plan_spec(tmp_path), monkeypatch)
    assert "--json-schema" not in argv
    assert "--dangerously-skip-permissions" not in argv   # still soft-denied


async def test_grok_decomposition_keeps_read_only_tools_without_schema(tmp_path, monkeypatch):
    argv = await _argv_of(GrokExecutor(), _plan_spec(tmp_path), monkeypatch)
    assert "--json-schema" not in argv
    assert "--tools" in argv                  # read-only toolset stays
    assert "streaming-json" in argv           # and the run stays observable


def test_codex_start_source_keys_schema_on_expect_verdict():
    source = inspect.getsource(CodexExecutor.start)
    assert "if task.expect_verdict:" in source, \
        "codex binds the verdict schema to something other than expect_verdict"
    assert '"--sandbox", "read-only" if task.read_only' in source, \
        "the sandbox must stay keyed on read_only — that half was correct"


def test_agy_start_source_keys_schema_on_expect_verdict():
    source = inspect.getsource(AgyExecutor.start)
    assert "if task.expect_verdict:" in source
    assert "if not task.read_only:" in source, \
        "agy's soft-deny (skip-permissions withheld) must stay keyed on read_only"


def test_grok_start_source_splits_tools_from_schema():
    source = inspect.getsource(GrokExecutor.start)
    assert "if task.read_only:" in source          # toolset restriction
    assert "if task.expect_verdict:" in source     # schema + one-shot json
    schema_branch = source.split("if task.expect_verdict:")[1]
    assert "--json-schema" in schema_branch, \
        "grok's verdict schema must live under expect_verdict, not read_only"


def test_orchestrator_expects_verdicts_from_review_gates_only():
    source = inspect.getsource(orch_mod.Orchestrator)
    assert 'expect_verdict=(stage.gate == "agent-review")' in source, \
        "the orchestrator is the only place that knows which runs are reviews"


def test_decompose_builds_a_read_only_run_without_a_verdict():
    source = inspect.getsource(decompose)
    assert "read_only=True" in source
    assert "expect_verdict" not in source, \
        "decomposition must rely on the default (False): its answer IS the task list"


def test_taskspec_default_expects_no_verdict():
    task = TaskSpec(run_id="r", prompt="p", workdir="/tmp")
    assert task.expect_verdict is False
    assert task.read_only is False
