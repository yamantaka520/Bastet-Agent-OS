"""The PM stays responsible for its plan after decomposition.

The live complaint: cards hit business stalls (rework budget spent, criteria
disputes) and simply waited for a human — the PM that planned them had no
further duty. These tests pin the semantic supervision layer: bounded PM
diagnosis of blocked cards, with hard walls around what it may do.
"""

import json

import pytest
from fake_executor import SCRIPT

from bastet_agent_os import pm_supervisor
from bastet_agent_os.db import now
from bastet_agent_os.executors.base import RunResult


def _decision(**kw) -> RunResult:
    return RunResult(status="succeeded", summary=json.dumps(kw))


def _block_business(db, note="已經返工 3 次仍未通過"):
    """A blocked card that is neither infra-recoverable nor a human gate."""
    db.write("UPDATE jobs SET status='blocked', rework_count=3, rework_note=? "
             "WHERE id='job1'", (note,))
    db.write("UPDATE runs SET status='succeeded', error=NULL WHERE id='run1'")
    db.write("INSERT INTO gate_results(id,run_id,gate_type,verdict,reviewer_kind,"
             "reviewer_id,detail_md,at) VALUES('g1','run1','agent-review','failed',"
             "'agent','fakebot','未提供實測證據',?)", (now(),))


def _make_pm(db, agent_id="fakebot"):
    db.write("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
             "VALUES('proj1',?,'pm',100)", (agent_id,))


@pytest.mark.asyncio
async def test_pm_diagnoses_a_business_stall_and_retries(orch, seeded):
    _make_pm(seeded)
    _block_business(seeded)
    SCRIPT.append(_decision(action="retry", reason="環境已修復，重跑即可"))
    called = []
    orch.retry = lambda job_id, agent_id="", user="": called.append(
        (job_id, agent_id, user)) or {}

    outcome = await pm_supervisor.diagnose(orch, seeded.one(
        "SELECT * FROM jobs WHERE id='job1'"))

    assert outcome["action"] == "retry"
    assert called == [("job1", "", "pm-supervisor:fakebot")]
    audit = seeded.one("SELECT detail_json FROM audit_log "
                       "WHERE action='job.pm_intervention'")
    assert json.loads(audit["detail_json"])["decision"]["action"] == "retry"


@pytest.mark.asyncio
async def test_pm_prompt_carries_the_evidence(orch, seeded):
    """The diagnosis is only as good as what the PM is shown: the gate output,
    the rework note, and the run history all have to be in the prompt."""
    _make_pm(seeded)
    _block_business(seeded, note="迴圈不收斂")
    captured = {}

    def check(task):
        captured["prompt"] = task.prompt
        captured["read_only"] = task.read_only
        captured["expect_verdict"] = task.expect_verdict
        return _decision(action="escalate", reason="需要真機")
    SCRIPT.append(check)

    await pm_supervisor.diagnose(orch, seeded.one("SELECT * FROM jobs WHERE id='job1'"))

    assert "未提供實測證據" in captured["prompt"]       # the gate's actual output
    assert "迴圈不收斂" in captured["prompt"]           # the rework note
    assert captured["read_only"] is True
    # the verdict-schema bug class: a diagnosis whose answer is a JSON decision
    # must never be forced into {verdict, reasons, comments}
    assert captured["expect_verdict"] is False


@pytest.mark.asyncio
async def test_supply_then_retry_files_a_ruling(orch, seeded):
    _make_pm(seeded)
    _block_business(seeded)
    SCRIPT.append(_decision(action="supply_then_retry",
                            reason="fps 條件用節流模擬取證",
                            supply="裁定：審查接受 CPU 節流模擬的 fps 數據，真機留給上線核准。"))
    orch.retry = lambda *a, **kw: {}

    await pm_supervisor.diagnose(orch, seeded.one("SELECT * FROM jobs WHERE id='job1'"))

    supply = seeded.one("SELECT * FROM job_supplies WHERE job_id='job1'")
    assert supply is not None and "節流模擬" in supply["content"]
    assert supply["created_by"] == "pm-supervisor:fakebot"


@pytest.mark.asyncio
async def test_secret_shaped_supply_is_refused(orch, seeded):
    """A prompt-injected diagnosis must not smuggle a key into the next run."""
    _make_pm(seeded)
    _block_business(seeded)
    SCRIPT.append(_decision(action="supply_then_retry", reason="x",
                            supply="API_KEY=sk-abc123456789012345678901234567890"))
    orch.retry = lambda **kw: pytest.fail("must not retry after a refused supply")

    outcome = await pm_supervisor.diagnose(orch, seeded.one(
        "SELECT * FROM jobs WHERE id='job1'"))

    assert "refused" in outcome["reason"]
    assert seeded.one("SELECT * FROM job_supplies WHERE job_id='job1'") is None


@pytest.mark.asyncio
async def test_retry_other_agent_validates_the_choice(orch, seeded):
    _make_pm(seeded)
    _block_business(seeded)
    seeded.write("INSERT INTO agents(id,amos_agent_id,name,executor_type,enabled,"
                 "config_json,created_at,updated_at) "
                 "VALUES('backup','backup','B','fake',1,'{}',?,?)", (now(), now()))
    SCRIPT.append(_decision(action="retry_other_agent", reason="連續同型失敗",
                            agent_id="ghost-agent"))    # does not exist
    called = []
    orch.retry = lambda job_id, agent_id="", user="": called.append(agent_id) or {}
    orch._alternate_agent = lambda job, last: "backup"

    await pm_supervisor.diagnose(orch, seeded.one("SELECT * FROM jobs WHERE id='job1'"))

    assert called == ["backup"], "an invalid agent choice must fall back, not crash"


@pytest.mark.asyncio
async def test_unparseable_diagnosis_spends_the_budget(orch, seeded):
    """A PM that answers prose has had its chance — no free retries."""
    _make_pm(seeded)
    _block_business(seeded)
    SCRIPT.append(RunResult(status="succeeded", summary="我覺得應該沒問題吧"))
    orch.retry = lambda **kw: pytest.fail("nothing usable was decided")

    outcome = await pm_supervisor.diagnose(orch, seeded.one(
        "SELECT * FROM jobs WHERE id='job1'"))

    assert outcome["action"] == "skipped"
    assert pm_supervisor.intervention_count(seeded, "job1") == 1


@pytest.mark.asyncio
async def test_no_pm_role_means_no_intervention(orch, seeded):
    _block_business(seeded)
    outcome = await pm_supervisor.diagnose(orch, seeded.one(
        "SELECT * FROM jobs WHERE id='job1'"))
    assert outcome == {"action": "skipped", "reason": "no pm role assigned"}
    assert pm_supervisor.intervention_count(seeded, "job1") == 0


# ---- the sweep gates: who gets diagnosed at all -----------------------------------

@pytest.mark.asyncio
async def test_sweep_never_hands_human_gates_to_the_pm(orch, seeded):
    _make_pm(seeded)
    seeded.write("UPDATE jobs SET status='blocked' WHERE id='job1'")
    seeded.write("UPDATE runs SET status='succeeded' WHERE id='run1'")
    seeded.write("INSERT INTO gate_results(id,run_id,gate_type,verdict,reviewer_kind,"
                 "reviewer_id,detail_md,at) VALUES('g1','run1','human-approve',"
                 "'pending','human','','',?)", (now(),))

    await orch.supervise_once()
    await orch.wait_idle()

    assert pm_supervisor.intervention_count(seeded, "job1") == 0, \
        "a designed human stop was handed to the PM"


@pytest.mark.asyncio
async def test_sweep_skips_quota_waits(orch, seeded):
    _make_pm(seeded)
    _block_business(seeded)
    seeded.write("UPDATE jobs SET resume_at='2999-01-01T00:00:00+00:00' WHERE id='job1'")

    await orch.supervise_once()
    await orch.wait_idle()

    assert pm_supervisor.intervention_count(seeded, "job1") == 0, \
        "a self-resuming quota wait does not need a PM"


@pytest.mark.asyncio
async def test_sweep_diagnoses_and_caps(orch, seeded):
    _make_pm(seeded)
    _block_business(seeded)
    orch.retry = lambda **kw: {}
    SCRIPT.append(_decision(action="retry", reason="1"))
    SCRIPT.append(_decision(action="retry", reason="2"))

    await orch.supervise_once()
    await orch.wait_idle()
    assert pm_supervisor.intervention_count(seeded, "job1") == 1

    await orch.supervise_once()
    await orch.wait_idle()
    assert pm_supervisor.intervention_count(seeded, "job1") == 2

    # budget spent: the third sweep must not spawn a diagnosis (SCRIPT is empty —
    # a pop from it would raise inside the driver and fail the test via audit)
    await orch.supervise_once()
    await orch.wait_idle()
    assert pm_supervisor.intervention_count(seeded, "job1") == 2


@pytest.mark.asyncio
async def test_escalate_latches_until_a_human_retries(orch, seeded):
    _make_pm(seeded)
    _block_business(seeded)
    SCRIPT.append(_decision(action="escalate", reason="需要真機證據，機器給不了"))

    await orch.supervise_once()
    await orch.wait_idle()
    assert pm_supervisor.intervention_count(seeded, "job1") == 1

    # still blocked, nothing changed: the sweep must NOT burn the second
    # intervention restating the escalation
    await orch.supervise_once()
    await orch.wait_idle()
    assert pm_supervisor.intervention_count(seeded, "job1") == 1

    # a human retry starts a new episode — the PM may look again
    seeded.audit("user:root", "job.retry", "job", "job1", {})
    SCRIPT.append(_decision(action="retry", reason="人已補了真機影片"))
    orch.retry = lambda **kw: {}
    await orch.supervise_once()
    await orch.wait_idle()
    assert pm_supervisor.intervention_count(seeded, "job1") == 2
