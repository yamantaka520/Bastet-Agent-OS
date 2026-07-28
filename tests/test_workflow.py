"""Workflow engine: stage pipelines, gate protocol, retries, human approval."""

import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os.executors.base import RunResult
from bastet_agent_os.workflow import evaluate_gate, parse_stages

# ---- stage/template validation ---------------------------------------------------

def test_parse_stages_validates():
    with pytest.raises(ValueError):
        parse_stages([])
    with pytest.raises(ValueError):
        parse_stages([{"name": "a", "gate": "nonsense"}])
    with pytest.raises(ValueError):
        parse_stages([{"name": "a"}, {"name": "a"}])  # duplicate
    with pytest.raises(ValueError):
        parse_stages([{"name": "a", "gate": "tests-pass"}])  # no command


def test_tests_pass_gate_runs_command(tmp_path):
    stage = parse_stages([{"name": "t", "gate": "tests-pass",
                           "gate_config": {"command": "exit 0"}}])[0]
    assert evaluate_gate(stage, str(tmp_path), None).verdict == "passed"
    stage.gate_config["command"] = "exit 3"
    assert evaluate_gate(stage, str(tmp_path), None).verdict == "failed"


def test_agent_review_gate_requires_structured_verdict(tmp_path):
    stage = parse_stages([{"name": "r", "gate": "agent-review"}])[0]
    assert evaluate_gate(stage, str(tmp_path), None).verdict == "failed"  # missing => reject
    assert evaluate_gate(stage, str(tmp_path), {"verdict": "approve"}).verdict == "passed"
    out = evaluate_gate(stage, str(tmp_path), {"verdict": "reject", "reasons": ["bad"]})
    assert out.verdict == "failed" and "bad" in out.detail


# ---- full pipeline behavior -------------------------------------------------------

async def test_single_stage_auto_gate_completes_job(orch, seeded):
    SCRIPT.append(RunResult(status="succeeded", summary="ok"))
    job_id = orch.dispatch(req())
    await orch.wait_idle()
    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "done"


async def test_multi_stage_advances_through_review(orch, seeded):
    add_template(seeded, "dev", [
        {"name": "work", "gate": "auto"},
        {"name": "review", "gate": "agent-review", "read_only": True},
    ])
    SCRIPT.append(RunResult(status="succeeded", summary="implemented"))
    SCRIPT.append(RunResult(status="succeeded", summary="looks good",
                            structured_verdict={"verdict": "approve"}))
    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()
    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "done"
    gates = seeded.query("SELECT verdict FROM gate_results g JOIN runs r ON r.id=g.run_id "
                         "WHERE r.job_id=? ORDER BY g.at", (job_id,))
    assert [g["verdict"] for g in gates] == ["passed", "passed"]


async def test_review_without_verdict_blocks_job(orch, seeded):
    add_template(seeded, "dev", [
        {"name": "work", "gate": "auto"},
        {"name": "review", "gate": "agent-review"},
    ])
    SCRIPT.append(RunResult(status="succeeded"))
    SCRIPT.append(RunResult(status="succeeded", summary="APPROVED!!"))  # prose only — no verdict
    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()
    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "blocked"  # free text never passes an agent-review gate


async def test_execution_failure_retries_then_blocks(orch, seeded):
    add_template(seeded, "retry", [
        {"name": "work", "gate": "auto", "max_retries": 1},
    ])
    SCRIPT.append(RunResult(status="failed", summary="boom"))
    SCRIPT.append(RunResult(status="succeeded"))
    job_id = orch.dispatch(req(template_id="retry"))
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    attempts = seeded.query("SELECT attempt, status FROM runs WHERE job_id=? ORDER BY attempt",
                            (job_id,))
    assert [(a["attempt"], a["status"]) for a in attempts] == [(1, "failed"), (2, "succeeded")]


async def test_human_approve_pauses_then_resumes(orch, seeded):
    add_template(seeded, "gated", [
        {"name": "plan", "gate": "human-approve"},
        {"name": "work", "gate": "auto"},
    ])
    SCRIPT.append(RunResult(status="succeeded", summary="the plan"))
    job_id = orch.dispatch(req(template_id="gated"))
    await orch.wait_idle()
    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert (job["status"], job["stage"]) == ("blocked", "plan")  # waiting for a human

    SCRIPT.append(RunResult(status="succeeded", summary="did the work"))
    orch.approve(job_id, approved=True, comment="lgtm")
    await orch.wait_idle()
    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert (job["status"], job["stage"]) == ("done", "work")


async def test_human_reject_keeps_job_blocked(orch, seeded):
    add_template(seeded, "gated", [{"name": "plan", "gate": "human-approve"}])
    SCRIPT.append(RunResult(status="succeeded"))
    job_id = orch.dispatch(req(template_id="gated"))
    await orch.wait_idle()
    orch.approve(job_id, approved=False, comment="not like this")
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "blocked"
    with pytest.raises(ValueError):
        orch.approve("job_nonexistent", approved=True, comment="")


async def test_role_routing_picks_role_agent(orch, seeded):
    seeded.write("INSERT INTO agents(id, amos_agent_id, name, executor_type, created_at, "
                 "updated_at) VALUES('reviewer-bot','rb','R','fake',datetime('now'),"
                 "datetime('now'))")
    seeded.write("INSERT INTO project_agent_roles(project_id, agent_id, role, preference) "
                 "VALUES('proj1','reviewer-bot','reviewer',10)")
    add_template(seeded, "dev", [
        {"name": "work", "gate": "auto"},
        {"name": "review", "role": "reviewer", "gate": "agent-review"},
    ])
    SCRIPT.append(RunResult(status="succeeded"))
    SCRIPT.append(RunResult(status="succeeded", structured_verdict={"verdict": "approve"}))
    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()
    review_run = seeded.one("SELECT agent_id FROM runs WHERE job_id=? AND stage='review'",
                            (job_id,))
    assert review_run["agent_id"] == "reviewer-bot"  # role match beats job default
