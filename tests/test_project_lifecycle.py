"""Project lifecycle, PM decomposition, and the run/pause/stop controls.

The light in the UI is this status, so the rules have to hold: only declared
transitions, nothing runs before a human confirms the task plan, pause leaves
the current task alone, stop actually kills what is in flight.
"""

import asyncio
import json

import pytest
from fake_executor import SCRIPT, add_template

from bastet_agent_os import project_lifecycle as lifecycle
from bastet_agent_os import project_runner as runner_mod
from bastet_agent_os.executors.base import RunResult


@pytest.fixture
def proj(seeded):
    """The shared fixture ships one in-progress job; lifecycle tests want a
    clean slate so 'all tasks settled' means what it says."""
    seeded.write("UPDATE projects SET status='planning' WHERE id='proj1'")
    seeded.write("DELETE FROM runs")
    seeded.write("DELETE FROM jobs")
    return seeded


# ---- the state machine -------------------------------------------------------------

def test_only_declared_transitions_are_allowed(proj):
    assert lifecycle.status_of(proj, "proj1") == lifecycle.PLANNING
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.apply(proj, "proj1", "start")          # planning cannot start
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.apply(proj, "proj1", "teleport")

    assert lifecycle.apply(proj, "proj1", "confirm_plan", "u") == lifecycle.READY
    assert lifecycle.apply(proj, "proj1", "start", "u") == lifecycle.RUNNING
    assert lifecycle.apply(proj, "proj1", "pause", "u") == lifecycle.PAUSED
    assert lifecycle.apply(proj, "proj1", "resume", "u") == lifecycle.RUNNING
    assert lifecycle.apply(proj, "proj1", "stop", "u") == lifecycle.READY
    assert lifecycle.apply(proj, "proj1", "close", "u") == lifecycle.CLOSED
    assert lifecycle.apply(proj, "proj1", "reopen", "u") == lifecycle.PLANNING
    assert proj.query("SELECT * FROM audit_log WHERE action='project.reopen'")


def test_every_status_has_a_light_and_the_ui_gets_the_legal_moves(proj):
    assert set(lifecycle.LIGHTS) == set(lifecycle.STATUSES)
    # `activate`/`complete` are internal: the UI must not offer them as buttons
    assert lifecycle.allowed_transitions(lifecycle.PLANNING) == ["confirm_plan"]
    assert "activate" in lifecycle.allowed_transitions(lifecycle.PLANNING,
                                                       include_internal=True)
    assert "start" in lifecycle.allowed_transitions(lifecycle.READY)
    assert lifecycle.allowed_transitions(lifecycle.CLOSED) == ["reopen"]


def test_running_project_moves_to_maintenance_when_all_tasks_settle(proj):
    from bastet_agent_os.db import now as _now
    lifecycle.apply(proj, "proj1", "confirm_plan")
    lifecycle.apply(proj, "proj1", "start")
    proj.write("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, stage, "
               "status, created_at, updated_at) VALUES('j1','proj1','[]','a','x',"
               "'in_progress',?,?)", (_now(), _now()))
    assert lifecycle.maybe_complete(proj, "proj1") is None      # still working
    proj.write("UPDATE jobs SET status='done' WHERE id='j1'")
    assert lifecycle.maybe_complete(proj, "proj1") == lifecycle.MAINTENANCE
    assert lifecycle.overview(proj, "proj1")["progress"]["done"] == 1


def test_restart_parks_a_running_project_instead_of_lying(proj):
    lifecycle.apply(proj, "proj1", "confirm_plan")
    lifecycle.apply(proj, "proj1", "start")
    assert runner_mod.reconcile(proj) == ["proj1"]
    assert lifecycle.status_of(proj, "proj1") == lifecycle.PAUSED


# ---- decomposition ----------------------------------------------------------------

def test_parse_tasks_accepts_json_in_prose_and_rejects_prose_only():
    tasks = runner_mod.parse_tasks(
        'Sure! Here you go:\n{"tasks":[{"title":"後端 API","spec":"做 CRUD",'
        '"role":"backend-engineer"},{"title":"前端頁面","spec":"表單"}]}\nHope that helps')
    assert [t["title"] for t in tasks] == ["後端 API", "前端頁面"]
    assert tasks[0]["role"] == "backend-engineer"
    assert tasks[1]["spec"] == "表單"
    with pytest.raises(runner_mod.PlanError, match="It said: I think we should"):
        runner_mod.parse_tasks("I think we should start with the backend.")
    with pytest.raises(runner_mod.PlanError):
        runner_mod.parse_tasks('{"tasks":[]}')
    with pytest.raises(runner_mod.PlanError):
        runner_mod.parse_tasks('{"tasks":[{"spec":"no title"}]}')


async def test_decompose_uses_the_pm_agent_and_stores_an_unconfirmed_plan(orch, proj,
                                                                         tmp_path):
    proj.write("INSERT INTO project_agent_roles(project_id, agent_id, role, preference) "
               "VALUES('proj1','fakebot','pm',5)")
    add_template(proj, "dev", [{"name": "build", "gate": "auto"}])
    proj.write("UPDATE projects SET default_template_id='dev' WHERE id='proj1'")

    captured = {}

    def capture(task):
        captured["prompt"] = task.prompt
        captured["read_only"] = task.read_only
        return RunResult(status="succeeded", summary=json.dumps(
            {"tasks": [{"title": "T1", "spec": "do one"},
                       {"title": "T2", "spec": "do two"}]}))

    SCRIPT.append(capture)
    tasks = await runner_mod.decompose(proj, tmp_path, "proj1", actor="u")
    assert [t["title"] for t in tasks] == ["T1", "T2"]
    assert captured["read_only"] is True            # planning never writes
    assert "build" in captured["prompt"]            # it knows the workflow stages

    plan = lifecycle.task_plan(proj, "proj1")
    assert plan["confirmed"] is False               # a human still has to say go
    assert plan["by"] == "fakebot"


async def test_decompose_without_a_pm_agent_says_what_to_fix(proj, orch, tmp_path):
    with pytest.raises(runner_mod.PlanError, match="專案經理"):
        await runner_mod.decompose(proj, tmp_path, "proj1")


# ---- the runner --------------------------------------------------------------------

@pytest.fixture
def runner(orch, proj):
    proj.write("INSERT INTO project_agent_roles(project_id, agent_id, role, preference) "
               "VALUES('proj1','fakebot','engineer',1)")
    add_template(proj, "dev", [{"name": "build", "gate": "auto"}])
    proj.write("UPDATE projects SET default_template_id='dev' WHERE id='proj1'")
    r = runner_mod.ProjectRunner(proj, orch)
    runner_mod.POLL_S = 0.01          # tests should not wait three seconds a task
    return r, orch, proj


async def test_nothing_runs_before_a_human_confirms_the_plan(runner):
    r, _, db = runner
    lifecycle.save_task_plan(db, "proj1", [{"title": "T1", "spec": "s"}], by="pm")
    lifecycle.apply(db, "proj1", "confirm_plan")
    lifecycle.apply(db, "proj1", "start")
    with pytest.raises(runner_mod.PlanError, match="人工確認"):
        r.start("proj1", "")


async def test_runner_dispatches_the_confirmed_tasks_in_order(runner):
    r, orch, db = runner
    lifecycle.save_task_plan(db, "proj1", [{"title": "T1", "spec": "one"},
                                           {"title": "T2", "spec": "two"}],
                             by="pm", confirmed=True)
    lifecycle.apply(db, "proj1", "confirm_plan")
    lifecycle.apply(db, "proj1", "start")
    SCRIPT.append(RunResult(status="succeeded", summary="ok"))
    SCRIPT.append(RunResult(status="succeeded", summary="ok"))

    r.start("proj1", "fakebot")
    for _ in range(400):
        if lifecycle.status_of(db, "proj1") == lifecycle.MAINTENANCE:
            break
        await asyncio.sleep(0.02)

    titles = [j["title"] for j in db.query(
        "SELECT title FROM jobs WHERE project_id=? ORDER BY created_at", ("proj1",))]
    assert titles == ["T1", "T2"]                    # one after the other
    # every task remembers its job, so a resume does not run it twice
    plan = lifecycle.task_plan(db, "proj1")
    assert all(t.get("job_id") for t in plan["tasks"])
    # all tasks settled → the project is waiting for acceptance
    assert lifecycle.status_of(db, "proj1") == lifecycle.MAINTENANCE


async def test_pause_stops_the_next_dispatch(runner):
    r, orch, db = runner
    lifecycle.save_task_plan(db, "proj1", [{"title": "T1", "spec": "one"},
                                           {"title": "T2", "spec": "two"}],
                             by="pm", confirmed=True)
    lifecycle.apply(db, "proj1", "confirm_plan")
    lifecycle.apply(db, "proj1", "start")

    def first(task):
        lifecycle.apply(db, "proj1", "pause", "u")   # the human pauses mid-task
        return RunResult(status="succeeded", summary="ok")

    SCRIPT.append(first)
    r.start("proj1", "fakebot")
    await asyncio.sleep(0.4)
    titles = [j["title"] for j in db.query(
        "SELECT title FROM jobs WHERE project_id=?", ("proj1",))]
    assert titles == ["T1"]                          # T2 was never dispatched
    assert lifecycle.status_of(db, "proj1") == lifecycle.PAUSED


async def test_stop_cancels_jobs_in_flight(runner):
    r, orch, db = runner
    from bastet_agent_os.db import now as _now
    db.write("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, stage, "
             "status, created_at, updated_at) VALUES('jx','proj1','[]','live','build',"
             "'in_progress',?,?)", (_now(), _now()))
    db.write("INSERT INTO runs(id, job_id, stage, agent_id, executor_type, status) "
             "VALUES('rx','jx','build','fakebot','fake','running')")
    lifecycle.apply(db, "proj1", "confirm_plan")
    lifecycle.apply(db, "proj1", "start")

    out = await r.stop("proj1", actor="u")
    assert out["jobs_cancelled"] == ["jx"]
    assert db.one("SELECT status FROM jobs WHERE id='jx'")["status"] == "cancelled"
    assert db.one("SELECT status FROM runs WHERE id='rx'")["status"] == "cancelled"
    assert db.query("SELECT * FROM audit_log WHERE action='job.cancel'")


def test_parse_tasks_survives_a_real_agent_answer():
    """Live finding: an agent prefixed a summary object before the real one and
    concatenated both. Greedy brace matching produced 'Extra data'."""
    text = ('好的，我先說明思路。\n'
            '{"summary":"先做後端再做前端","risk":"金流未定"}\n'
            '{"tasks":[{"title":"預約 API","spec":"CRUD + 衝突檢查"},'
            '{"title":"預約頁面","spec":"表單與時段選擇"}]}\n'
            '以上，需要調整請告知。')
    assert [t["title"] for t in runner_mod.parse_tasks(text)] == ["預約 API", "預約頁面"]
    # a bare array is fine too
    assert runner_mod.parse_tasks('[{"title":"只有一件事","spec":"做完"}]')[0]["spec"] \
        == "做完"
    # ```json fences and trailing commentary
    assert len(runner_mod.parse_tasks(
        '```json\n{"tasks":[{"title":"A","spec":"a"}]}\n```\nDone!')) == 1


def test_executors_do_not_truncate_the_payload_summary():
    """Live finding: every executor capped summary at 2000 chars. That is fine
    for a label but the summary IS the payload for chat replies and task plans —
    a cut-off JSON list parses as nothing at all."""
    from pathlib import Path

    from bastet_agent_os.executors.base import SUMMARY_LIMIT

    assert SUMMARY_LIMIT >= 100_000
    executors = Path("src/bastet_agent_os/executors")
    offenders = [p.name for p in executors.glob("*.py")
                 if "[:2000]" in p.read_text()]
    assert offenders == []

    # a plan longer than the old cap must still parse
    tasks = [{"title": f"任務 {i}", "spec": "細節 " * 200} for i in range(8)]
    payload = json.dumps({"tasks": tasks}, ensure_ascii=False)
    assert len(payload) > 2000
    assert len(runner_mod.parse_tasks(payload)) == 8


async def test_a_project_with_no_agents_does_not_sit_in_running_forever(runner):
    """Live finding: every task was skipped (no agent assigned), so no job
    existed, maybe_complete had nothing to count, and the project stayed
    'running' with no runner behind it."""
    r, _, db = runner
    db.write("DELETE FROM project_agent_roles WHERE project_id='proj1'")
    lifecycle.save_task_plan(db, "proj1", [{"title": "T1", "spec": "one"}],
                             by="pm", confirmed=True)
    lifecycle.apply(db, "proj1", "confirm_plan")
    lifecycle.apply(db, "proj1", "start")

    r.start("proj1", "")                 # no fallback agent either
    for _ in range(200):
        if lifecycle.status_of(db, "proj1") != lifecycle.RUNNING:
            break
        await asyncio.sleep(0.02)
    assert lifecycle.status_of(db, "proj1") == lifecycle.READY   # honestly parked
    assert db.query("SELECT * FROM audit_log WHERE action='project.runner.idle'")
    assert db.query("SELECT * FROM audit_log WHERE action='project.task.skipped'")
