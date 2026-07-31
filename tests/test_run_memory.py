"""Do runs actually leave anything in AMOS?

The memory tab was empty on a host that had been running projects for days.
Two causes: chat turns were never written (fixed separately), and runs were only
written by the `bastet-lite` executor — so a project driven by Claude Code or
Codex contributed nothing, and every later context pack read from an empty
store. These tests go through real AMOS rather than a stub, because the previous
bug was exactly a mismatch with AMOS's real API that a stub would have hidden.
"""

import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os import run_memory
from bastet_agent_os.executors.base import RunResult

# the orchestrator tests are async; the pure-function ones at the bottom are not,
# so the mark goes on the async tests rather than the module
asyncio_test = pytest.mark.asyncio


@pytest.fixture
def amos(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_HOME", str(tmp_path / "amos"))
    client = pytest.importorskip("agent_memory_os.client")
    return client.MemoryClient()


def contents(client) -> list[str]:
    return [r.content for r in client.list_recent(limit=50)]


@asyncio_test
async def test_a_run_through_any_executor_writes_what_it_did(orch, seeded, amos):
    """The fake executor stands in for claude-code/codex/grok: none of them
    write memories themselves, so the orchestrator has to."""
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(RunResult(status="succeeded",
                            summary="加了 /api/bookings，補了兩個測試"))

    orch.dispatch(req(template_id="dev", title="預約 API"))
    await orch.wait_idle()

    written = contents(amos)
    assert any("預約 API" in c and "implement" in c for c in written), written
    assert any("/api/bookings" in c for c in written)


@asyncio_test
async def test_the_memory_carries_the_project_grant_so_recall_is_scoped(orch, seeded,
                                                                       amos):
    """Without the grant, one project's memories land in another project's
    context pack — AMOS applies no ACL when nothing identifies the requester."""
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(RunResult(status="succeeded", summary="做完了"))

    orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    stage_memory = next(r for r in amos.list_recent(limit=10)
                        if "implement" in r.content)
    assert stage_memory.scope == "project"
    assert "project:proj1" in stage_memory.visibility
    assert "team:team1" in stage_memory.visibility
    assert stage_memory.owner == "fakebot"      # attributed to the agent that ran
    # the engine's own bookkeeping is owned by the engine, not by an agent
    finished = next(r for r in amos.list_recent(limit=10) if r.type == "decision")
    assert finished.owner == "bastet"


@asyncio_test
async def test_a_rejected_gate_is_remembered_as_a_warning(orch, seeded, amos):
    """The most valuable thing a run produces: a mistake actually made in this
    codebase, so the next agent does not repeat it."""
    add_template(seeded, "dev", [
        {"name": "implement", "gate": "tests-pass", "max_cycles": 1,
         "gate_config": {"command": "echo 'AssertionError: booking not confirmed' "
                                    "&& exit 1"}},
    ])
    SCRIPT.append(RunResult(status="succeeded", summary="v1"))
    SCRIPT.append(RunResult(status="succeeded", summary="v2"))

    orch.dispatch(req(template_id="dev", title="預約流程"))
    await orch.wait_idle()

    warnings = [r for r in amos.list_recent(limit=50) if r.type == "warning"]
    assert warnings, "a failed gate left no trace in memory"
    assert any("booking not confirmed" in r.content for r in warnings)
    assert any("返工" in r.content for r in warnings)


@asyncio_test
async def test_a_failed_write_never_breaks_the_run(orch, seeded, monkeypatch):
    """Memory is valuable, not load-bearing. A broken AMOS must cost a memory,
    not the job."""
    def boom(*a, **k):
        raise RuntimeError("amos is on fire")

    monkeypatch.setattr(run_memory, "_client", boom)
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(RunResult(status="succeeded", summary="fine"))

    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"


def test_recall_identity_is_the_agents_amos_id(seeded):
    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")
    kwargs = run_memory.recall_kwargs(seeded, job, "ag1")

    assert kwargs["requester_agent_id"] == "ag1"
    assert kwargs["requester_team_id"] == "team1"


def test_engine_writes_are_not_attributed_to_an_agent(seeded):
    """Bastet's own bookkeeping is owned by Bastet, and an engine write must not
    turn on an ACL that would hide it from every agent."""
    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")

    assert "requester_agent_id" not in run_memory.recall_kwargs(seeded, job, None)
    assert run_memory.grants(seeded, "proj1") == ["project:proj1", "team:team1"]
