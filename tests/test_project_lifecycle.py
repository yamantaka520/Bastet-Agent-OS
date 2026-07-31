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
from bastet_agent_os.db import Db, now
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


def test_a_restart_parks_a_project_it_cannot_continue(proj):
    """No confirmed plan means there is nothing to resume — park it and say why
    rather than leaving a 'running' light with no runner."""
    lifecycle.apply(proj, "proj1", "confirm_plan")
    lifecycle.apply(proj, "proj1", "start")
    outcome = runner_mod.reconcile(proj, None)
    assert outcome == {"resumed": [], "parked": ["proj1"]}
    assert lifecycle.status_of(proj, "proj1") == lifecycle.PAUSED
    reason = proj.one("SELECT detail_json FROM audit_log WHERE action='project.parked' "
                      "ORDER BY at DESC LIMIT 1")["detail_json"]
    assert "nothing the runner could continue" in reason


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


# ---- the plan must be traceable to the conversation it came from --------------------

async def test_a_breakdown_taken_before_the_chat_moved_on_is_marked_stale(orch, proj,
                                                                         tmp_path):
    """The live case: a decomposition described a booking system while the chat
    had already turned the project into something else. Passing that off as "the
    plan" is worse than showing nothing."""
    from bastet_agent_os import chat as chat_mod

    proj.write("INSERT INTO project_agent_roles(project_id, agent_id, role, "
               "preference) VALUES('proj1','fakebot','pm',5)")
    session = chat_mod.create_session(proj, scope_type="project", scope_id="proj1",
                                      responder_kind="agent", responder_id="fakebot")
    chat_mod.add_message(proj, session, role="user", content="做一個預約系統")

    SCRIPT.append(RunResult(status="succeeded", summary=json.dumps(
        {"tasks": [{"title": "預約 API", "spec": "s"}]})))
    await runner_mod.decompose(proj, tmp_path, "proj1", actor="u")
    plan = lifecycle.plan_with_jobs(proj, "proj1")
    assert plan["stale"] is False
    assert plan["source"]["kind"] == "chat" and plan["source"]["messages"] == 1

    # the conversation continues: the project is now something else
    chat_mod.add_message(proj, session, role="user",
                         content="改成一個網頁小遊戲吧")
    plan = lifecycle.plan_with_jobs(proj, "proj1")
    assert plan["stale"] is True          # the UI must warn instead of pretending


async def test_re_decomposing_keeps_dispatched_tasks(orch, proj, tmp_path):
    """Replacing the proposal must not cut the plan's link to running jobs."""
    proj.write("INSERT INTO project_agent_roles(project_id, agent_id, role, "
               "preference) VALUES('proj1','fakebot','pm',5)")
    lifecycle.save_task_plan(proj, "proj1", [
        {"title": "已派工的事", "spec": "s", "job_id": "job_live"},
        {"title": "還沒派的舊提案", "spec": "s"}], by="pm", confirmed=True)

    SCRIPT.append(RunResult(status="succeeded", summary=json.dumps(
        {"tasks": [{"title": "新提案 A", "spec": "s"},
                   {"title": "新提案 B", "spec": "s"}]})))
    await runner_mod.decompose(proj, tmp_path, "proj1", actor="u")

    titles = [t["title"] for t in lifecycle.task_plan(proj, "proj1")["tasks"]]
    assert titles == ["已派工的事", "新提案 A", "新提案 B"]
    assert "還沒派的舊提案" not in titles          # the stale proposal is replaced


def test_clearing_a_stale_plan_keeps_the_running_work(proj):
    lifecycle.save_task_plan(proj, "proj1", [
        {"title": "跑著的", "spec": "s", "job_id": "job_live"},
        {"title": "提案一", "spec": "s"},
        {"title": "提案二", "spec": "s"}], by="pm", confirmed=True)
    assert lifecycle.clear_undispatched(proj, "proj1", actor="u") == 2
    tasks = lifecycle.task_plan(proj, "proj1")["tasks"]
    assert [t["title"] for t in tasks] == ["跑著的"]
    assert lifecycle.clear_undispatched(proj, "proj1") == 0      # idempotent
    assert proj.query("SELECT * FROM audit_log WHERE action='project.tasks.clear'")


def test_a_plan_with_no_recorded_source_is_flagged_unverified(proj):
    """The plans that existed when provenance shipped have no source. Passing
    them off as verified is how a breakdown describing an abandoned direction
    keeps looking authoritative."""
    lifecycle.save_task_plan(proj, "proj1", [{"title": "來源不明的提案", "spec": "s"}],
                             by="pm", confirmed=False, source={})
    plan = lifecycle.plan_with_jobs(proj, "proj1")
    assert plan["provenance"] == "unknown" and plan["unverified"] is True
    assert plan["stale"] is False          # unknowable, not proven stale


def test_linking_a_job_cannot_mask_staleness(proj):
    """plan["at"] moves when a job is linked; the staleness check must use the
    time the breakdown was *taken*."""
    from bastet_agent_os import chat as chat_mod
    from bastet_agent_os.db import now as _now

    session = chat_mod.create_session(proj, scope_type="project", scope_id="proj1",
                                      responder_kind="agent", responder_id="ag1")
    chat_mod.add_message(proj, session, role="user", content="第一版")
    lifecycle.save_task_plan(proj, "proj1", [{"title": "提案", "spec": "s"}], by="pm",
                             source={"kind": "chat", "at": _now(), "messages": 1})
    chat_mod.add_message(proj, session, role="user", content="其實改成別的")
    assert lifecycle.plan_with_jobs(proj, "proj1")["stale"] is True

    # linking a job re-saves the plan (bumping plan["at"]) — still stale
    proj.write("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, stage, "
               "status, created_at, updated_at) VALUES('jn','proj1','[]','其他事',"
               "'x','in_progress',?,?)", (_now(), _now()))
    lifecycle.link_job(proj, "proj1", "jn", "其他事", "s", origin="chat")
    assert lifecycle.plan_with_jobs(proj, "proj1")["stale"] is True


# ---- automatic continuation must survive a restart ---------------------------------

async def test_a_restart_resumes_a_project_that_still_has_work(runner):
    """The live failure: a deploy killed the loop, the project still read 執行中,
    and the next task was never dispatched — the user did it by hand."""
    r, orch, db = runner
    lifecycle.save_task_plan(db, "proj1", [{"title": "T1", "spec": "one"},
                                           {"title": "T2", "spec": "two"}],
                             by="pm", confirmed=True)
    lifecycle.apply(db, "proj1", "confirm_plan")
    lifecycle.apply(db, "proj1", "start")
    SCRIPT.append(RunResult(status="succeeded"))
    SCRIPT.append(RunResult(status="succeeded"))

    # a fresh process: no loop in memory, project still marked running
    fresh = runner_mod.ProjectRunner(db, orch)
    outcome = runner_mod.reconcile(db, fresh)
    assert outcome["resumed"] == ["proj1"] and outcome["parked"] == []
    assert db.query("SELECT * FROM audit_log WHERE action='project.runner.resumed'")

    for _ in range(400):
        if lifecycle.status_of(db, "proj1") == lifecycle.MAINTENANCE:
            break
        await asyncio.sleep(0.02)
    titles = [j["title"] for j in db.query(
        "SELECT title FROM jobs WHERE project_id=? ORDER BY created_at", ("proj1",))]
    assert titles == ["T1", "T2"]           # it carried on by itself


async def test_ensure_running_is_idempotent_and_respects_pause(runner):
    r, orch, db = runner
    lifecycle.save_task_plan(db, "proj1", [{"title": "T1", "spec": "one"}],
                             by="pm", confirmed=True)
    lifecycle.apply(db, "proj1", "confirm_plan")
    lifecycle.apply(db, "proj1", "start")
    SCRIPT.append(RunResult(status="succeeded"))
    assert r.ensure_running("proj1") is True
    assert r.ensure_running("proj1") is False        # one loop is enough
    await asyncio.sleep(0.3)

    lifecycle.apply(db, "proj1", "pause", "u") if \
        lifecycle.status_of(db, "proj1") == lifecycle.RUNNING else None
    assert r.ensure_running("proj1") is False        # paused stays paused


async def test_a_settled_job_revives_a_dead_runner(runner):
    """Belt and braces: even if a loop dies unexpectedly, the next job to settle
    puts the project back in motion."""
    from bastet_agent_os.events import EventBus

    r, orch, db = runner
    bus = EventBus()
    lifecycle.save_task_plan(db, "proj1", [{"title": "T1", "spec": "one"},
                                           {"title": "T2", "spec": "two"}],
                             by="pm", confirmed=True)
    lifecycle.apply(db, "proj1", "confirm_plan")
    lifecycle.apply(db, "proj1", "start")
    SCRIPT.append(RunResult(status="succeeded"))
    SCRIPT.append(RunResult(status="succeeded"))

    watcher = asyncio.get_running_loop().create_task(r.watch(bus))
    await asyncio.sleep(0.05)
    bus.emit("job.done", project_id="proj1", job_id="whatever")
    for _ in range(400):
        if r.is_active("proj1") or lifecycle.status_of(db, "proj1") != lifecycle.RUNNING:
            break
        await asyncio.sleep(0.02)
    assert db.query("SELECT * FROM audit_log WHERE action='project.runner.resumed'")
    watcher.cancel()


# ---- deleting a project ------------------------------------------------------

def _client(tmp_path):
    from fastapi.testclient import TestClient

    from bastet_agent_os.config import Home
    from bastet_agent_os.server import create_app
    home = Home(tmp_path / "home")
    client = TestClient(create_app(home), base_url="http://127.0.0.1")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"
    return client, home


def test_a_trial_project_can_be_removed(tmp_path):
    """Test projects pile up — a workflow trial, a lifecycle probe — and there
    was no way to get rid of one, so the only option was editing the DB."""
    client, home = _client(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    client.post("/api/teams", json={"id": "t1", "name": "T"})
    client.post("/api/projects", json={"id": "probe", "repo_path": str(repo),
                                       "team_id": "t1"})
    client.post("/api/agents", json={"id": "a1", "name": "A",
                                     "executor_type": "claude-code"})
    client.post("/api/projects/probe/roles", json={"agent_id": "a1", "role": "engineer"})

    out = client.delete("/api/projects/probe")
    assert out.status_code == 200, out.text
    assert out.json()["deleted"] == "probe"

    assert client.get("/api/projects").json() == []
    db = Db(home.db_path)
    assert db.query("SELECT * FROM project_agent_roles WHERE project_id='probe'") == []
    # the audit trail keeps the fact that it existed
    assert db.one("SELECT 1 AS ok FROM audit_log WHERE action='project.delete'")["ok"] == 1
    db.close()


def test_deleting_a_project_with_spend_needs_force_and_records_the_amount(tmp_path):
    """Usage rows are the accounting. They may go when the whole project goes,
    but not silently — the refusal names the amount and forcing records it."""
    client, home = _client(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    client.post("/api/teams", json={"id": "t1", "name": "T"})
    client.post("/api/projects", json={"id": "spendy", "repo_path": str(repo),
                                       "team_id": "t1"})
    client.post("/api/agents", json={"id": "a1", "name": "A",
                                     "executor_type": "claude-code"})
    db = Db(home.db_path)
    ts = now()
    db.write("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, spec_md, "
             "stage, status, created_at, updated_at) VALUES('j1','spendy','[]','t','s',"
             "'work','done',?,?)", (ts, ts))
    db.write("INSERT INTO runs(id, job_id, stage, attempt, agent_id, executor_type, "
             "status) VALUES('r1','j1','work',1,'a1','claude-code','succeeded')")
    db.write("INSERT INTO resources(id, kind, name, endpoint, api_flavor, "
             "created_at, updated_at) VALUES('res1','llm','up','https://x','anthropic',"
             "?,?)", (ts, ts))
    db.write("INSERT INTO usage_ledger(id, run_id, resource_id, cost_usd, at) "
             "VALUES('u1','r1','res1',1.25,?)", (ts,))
    db.close()

    refused = client.delete("/api/projects/spendy")
    assert refused.status_code == 409
    assert "1.2500" in refused.json()["detail"]     # the amount, not just "in use"

    forced = client.delete("/api/projects/spendy?force=true")
    assert forced.status_code == 200
    body = forced.json()
    assert (body["usage_rows"], body["usage_usd"]) == (1, 1.25)

    db = Db(home.db_path)
    detail = db.one("SELECT detail_json FROM audit_log WHERE action='project.delete'")
    assert '"usage_usd": 1.25' in detail["detail_json"]   # written off on the record
    assert db.query("SELECT * FROM jobs WHERE project_id='spendy'") == []
    assert db.query("SELECT * FROM runs WHERE job_id='j1'") == []
    db.close()


def test_a_running_project_is_not_deleted_by_accident(tmp_path):
    client, home = _client(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    client.post("/api/teams", json={"id": "t1", "name": "T"})
    client.post("/api/projects", json={"id": "busy", "repo_path": str(repo),
                                       "team_id": "t1"})
    db = Db(home.db_path)
    ts = now()
    db.write("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, spec_md, "
             "stage, status, created_at, updated_at) VALUES('j9','busy','[]','t','s',"
             "'work','in_progress',?,?)", (ts, ts))
    db.close()

    refused = client.delete("/api/projects/busy")

    assert refused.status_code == 409
    assert "j9" in refused.json()["detail"]      # which job is in the way
