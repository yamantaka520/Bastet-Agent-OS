"""Executor admission is part of routing, not a run-time surprise.

The production failure behind these tests sent a direct-path E2E card to
Hermes even though Hermes only supports Bastet Gateway resources.  Hermes was
never launched, one PM intervention was wasted, and the retry audit named a
different agent from the one the stage role actually selected.
"""

import json

import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os.db import now
from bastet_agent_os.executors.base import RunResult


def _agent(db, agent_id: str, executor: str) -> None:
    db.write(
        "INSERT INTO agents(id,amos_agent_id,name,executor_type,enabled,"
        "config_json,created_at,updated_at) VALUES(?,?,?,?,1,'{}',?,?)",
        (agent_id, agent_id, agent_id, executor, now(), now()))


def test_direct_card_can_route_to_logged_in_hermes(orch, seeded):
    _agent(seeded, "Hermes", "hermes")
    seeded.write(
        "INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
        "VALUES('proj1','Hermes','tester',100)")
    add_template(seeded, "e2e", [
        {"name": "E2E", "role": "tester", "gate": "auto"},
    ])
    seeded.write(
        "UPDATE jobs SET stage='E2E', resource_id=NULL, stages_snapshot_json=? "
        "WHERE id='job1'",
        (json.dumps([{"name": "E2E", "role": "tester", "gate": "auto"}]),))
    from bastet_agent_os.workflow import parse_stages

    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")
    stage = parse_stages(json.loads(job["stages_snapshot_json"]))[0]
    assert orch._agent_for_stage(job, stage)["id"] == "Hermes"


def test_alternate_skips_wrong_gateway_flavor_and_finds_the_next_candidate(
        orch, seeded):
    _agent(seeded, "Hermes", "hermes")
    _agent(seeded, "backup", "fake")
    for agent_id, preference in (("Hermes", 100), ("backup", 50)):
        seeded.write(
            "INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
            "VALUES('proj1',?,'tester',?)", (agent_id, preference))
    seeded.write(
        "UPDATE jobs SET stage='E2E', resource_id='res1', stages_snapshot_json=? "
        "WHERE id='job1'",
        (json.dumps([{"name": "E2E", "role": "tester", "gate": "auto"}]),))

    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")
    assert orch._alternate_agent(job, "someone-else") == "backup"


def test_gateway_openai_route_can_select_hermes_when_a_model_exists(orch, seeded):
    _agent(seeded, "Hermes", "hermes")
    seeded.write(
        "UPDATE resources SET api_flavor='openai', routing_json=? WHERE id='res1'",
        (json.dumps({"default_model": "qwen-max"}),))
    seeded.write(
        "INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
        "VALUES('proj1','Hermes','tester',100)")
    seeded.write(
        "UPDATE jobs SET stage='E2E', default_agent_id='Hermes', resource_id='res1', "
        "stages_snapshot_json=? WHERE id='job1'",
        (json.dumps([{"name": "E2E", "role": "tester", "gate": "auto"}]),))
    from bastet_agent_os.workflow import parse_stages

    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")
    stage = parse_stages(json.loads(job["stages_snapshot_json"]))[0]
    assert orch._agent_for_stage(job, stage)["id"] == "Hermes"


async def test_explicit_incompatible_retry_is_refused_without_mutating_card(
        orch, seeded):
    _agent(seeded, "Hermes", "hermes")
    SCRIPT.append(RunResult(status="failed", summary="temporary failure"))
    job_id = orch.dispatch(req(resource_id="res1"))
    await orch.wait_idle()

    with pytest.raises(ValueError, match="gateway API flavor anthropic is incompatible"):
        orch.retry(job_id, agent_id="Hermes", user="manfred")

    job = seeded.one(
        "SELECT status,default_agent_id,agent_override FROM jobs WHERE id=?", (job_id,))
    assert dict(job) == {
        "status": "blocked", "default_agent_id": "fakebot", "agent_override": None}
    assert seeded.one(
        "SELECT id FROM audit_log WHERE action='job.retry' AND target_id=?", (job_id,)) is None


async def test_retry_audit_names_the_agent_selected_by_stage_routing(orch, seeded):
    seeded.write(
        "INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
        "VALUES('proj1','fakebot','tester',0)")
    add_template(seeded, "e2e", [
        {"name": "E2E", "role": "tester", "gate": "auto"},
    ])
    SCRIPT.append(RunResult(status="failed", summary="temporary failure"))
    job_id = orch.dispatch(req(template_id="e2e", resource_id=None))
    await orch.wait_idle()

    _agent(seeded, "rolebot", "fake")
    seeded.write(
        "INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
        "VALUES('proj1','rolebot','tester',100)")
    SCRIPT.append(RunResult(status="succeeded", summary="green"))
    orch.retry(job_id, user="pm-supervisor:test")
    await orch.wait_idle()

    audit = seeded.one(
        "SELECT detail_json FROM audit_log WHERE action='job.retry' AND target_id=? "
        "ORDER BY id DESC LIMIT 1", (job_id,))
    detail = json.loads(audit["detail_json"])
    assert detail["agent"] == "rolebot"
    assert detail["requested_agent"] == ""
    latest = seeded.one(
        "SELECT agent_id FROM runs WHERE job_id=? ORDER BY rowid DESC LIMIT 1", (job_id,))
    assert latest["agent_id"] == detail["agent"]
