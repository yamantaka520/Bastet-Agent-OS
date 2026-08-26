"""What made the first real dispatch fail, pinned so it cannot come back.

The stored repo path was `~/Github/catswalker`. Nothing expanded it, something
created a literal `~` directory, git could not make a worktree there, the
orchestrator quietly ran the agent in that empty non-repo, and the executor
reported the failure with an empty string. Five separate holes.
"""

import json
import subprocess

import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os.config import check_repo_path, expand_repo_path, is_git_repo
from bastet_agent_os.executors.base import RunResult

# ---- path handling -----------------------------------------------------------------

def test_tilde_and_env_vars_are_expanded(monkeypatch, tmp_path):
    home = str(tmp_path / "home")
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("MYREPOS", str(tmp_path / "src"))
    assert expand_repo_path("~/Github/app") == f"{home}/Github/app"
    assert expand_repo_path("$MYREPOS/app") == f"{tmp_path / 'src'}/app"
    assert expand_repo_path("  /abs/path  ") == "/abs/path"
    assert expand_repo_path(None) == ""
    # the bug: an unexpanded path used verbatim creates a literal "~" directory
    assert "~" not in expand_repo_path("~/Github/app")


def test_relative_paths_are_refused_with_a_usable_message():
    with pytest.raises(ValueError, match="絕對路徑"):
        check_repo_path("Github/app")
    with pytest.raises(ValueError, match="絕對路徑"):
        check_repo_path("./app")
    with pytest.raises(ValueError):
        check_repo_path("")


def test_absoluteness_is_judged_on_this_platform(tmp_path):
    """A Windows path is absolute on Windows and meaningless on POSIX — judging
    it by the *server's* platform is the only correct answer, because that is
    where the path will be used."""
    import os
    if os.name == "nt":
        assert check_repo_path(r"C:\\Users\\you\\project")
    else:
        with pytest.raises(ValueError):
            check_repo_path(r"C:\\Users\\you\\project")
        assert check_repo_path(str(tmp_path)) == str(tmp_path)


def test_is_git_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not is_git_repo(plain)
    subprocess.run(["git", "init", "-q", str(plain)], check=True)
    assert is_git_repo(plain)


# ---- dispatch refuses to run in the wrong place -------------------------------------

async def test_missing_repo_fails_the_run_with_the_path_in_the_message(orch, seeded,
                                                                      tmp_path):
    seeded.write("UPDATE projects SET repo_path=? WHERE id='proj1'",
                 (str(tmp_path / "not-there"),))
    job_id = orch.dispatch(req())
    await orch.wait_idle()
    run = seeded.one("SELECT * FROM runs WHERE job_id=? ORDER BY started_at DESC "
                     "LIMIT 1", (job_id,))
    assert run["status"] == "failed"
    assert "not-there" in (run["error"] or "")        # says which path
    assert "不存在" in (run["error"] or "")


async def test_a_directory_that_is_not_a_git_repo_is_refused(orch, seeded, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    seeded.write("UPDATE projects SET repo_path=? WHERE id='proj1'", (str(plain),))
    job_id = orch.dispatch(req())
    await orch.wait_idle()
    run = seeded.one("SELECT * FROM runs WHERE job_id=? ORDER BY started_at DESC "
                     "LIMIT 1", (job_id,))
    assert run["status"] == "failed" and "git repo" in (run["error"] or "")


async def test_the_agent_runs_inside_the_expanded_repo(orch, seeded, repo, monkeypatch):
    """The whole point: with `~` in the DB the agent must still start in the real
    directory, not in a literal `~/…` that does not exist."""
    monkeypatch.setenv("HOME", str(repo.parent))
    seeded.write("UPDATE projects SET repo_path='~/repo' WHERE id='proj1'")
    captured = {}
    SCRIPT.append(lambda task: (captured.update(workdir=task.workdir)
                                or RunResult(status="succeeded")))
    orch.dispatch(req())
    await orch.wait_idle()
    assert "~" not in captured["workdir"]
    assert str(repo) in captured["workdir"]          # repo itself or its worktree


# ---- a failure always says something ------------------------------------------------

async def test_a_failed_run_never_records_an_empty_error(orch, seeded):
    """The first real failure showed `error: ""` — undebuggable."""
    SCRIPT.append(RunResult(status="failed", summary=""))
    job_id = orch.dispatch(req())
    await orch.wait_idle()
    run = seeded.one("SELECT * FROM runs WHERE job_id=?", (job_id,))
    assert run["status"] == "failed"
    assert (run["error"] or "").strip()
    assert "no output" in run["error"]


# ---- retry -------------------------------------------------------------------------

async def test_retry_reruns_the_stuck_stage(orch, seeded):
    SCRIPT.append(RunResult(status="failed", summary="boom"))
    job_id = orch.dispatch(req())
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] \
        == "blocked"

    SCRIPT.append(RunResult(status="succeeded", summary="fixed"))
    out = orch.retry(job_id, user="manfred")
    assert out["status"] == "in_progress"
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    attempts = seeded.query("SELECT attempt FROM runs WHERE job_id=? ORDER BY attempt",
                            (job_id,))
    assert len(attempts) >= 2                      # a second attempt really ran
    assert seeded.query("SELECT * FROM audit_log WHERE action='job.retry'")


async def test_retry_can_switch_agent_and_refuses_healthy_jobs(orch, seeded):
    seeded.write("INSERT INTO agents(id, amos_agent_id, name, executor_type, "
                 "created_at, updated_at) VALUES('other','other','Other','fake',"
                 "datetime('now'),datetime('now'))")
    SCRIPT.append(RunResult(status="failed", summary="boom"))
    job_id = orch.dispatch(req())
    await orch.wait_idle()

    SCRIPT.append(RunResult(status="succeeded"))
    orch.retry(job_id, agent_id="other")
    await orch.wait_idle()
    assert seeded.one("SELECT default_agent_id FROM jobs WHERE id=?",
                      (job_id,))["default_agent_id"] == "other"

    with pytest.raises(ValueError, match="not stuck"):
        orch.retry(job_id)                          # it is done now
    with pytest.raises(ValueError):
        orch.retry("job_ghost")


def test_job_detail_returns_the_failure_reason(tmp_path):
    """The drawer is where a stuck card gets diagnosed. Live finding: the API
    left `error` out of the runs, so the UI had nothing to show next to the
    retry button even though the DB knew exactly what was wrong."""
    from fastapi.testclient import TestClient

    from bastet_agent_os.config import Home
    from bastet_agent_os.db import Db, now
    from bastet_agent_os.server import create_app

    home = Home(tmp_path / "home")
    client = TestClient(create_app(home), base_url="http://127.0.0.1")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"
    db = Db(home.db_path)
    try:
        db.write("INSERT INTO projects(id, team_id, repo_path, created_at, updated_at) "
                 "VALUES('p','t','/x',?,?)", (now(), now()))
        db.write("INSERT INTO agents(id, amos_agent_id, name, executor_type, "
                 "created_at, updated_at) VALUES('a','a','A','fake',?,?)",
                 (now(), now()))
        db.write("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, stage, "
                 "status, created_at, updated_at) VALUES('j','p','[]','t','s',"
                 "'blocked',?,?)", (now(), now()))
        db.write("INSERT INTO runs(id, job_id, stage, agent_id, executor_type, status, "
                 "error) VALUES('r','j','s','a','fake','failed','repo path is not a "
                 "git repo')")
    finally:
        db.close()
    run = client.get("/api/jobs/j").json()["runs"][0]
    assert run["error"] == "repo path is not a git repo"
    assert run["executor_type"] == "fake"


# ---- the project tab and the board must agree ---------------------------------------

def test_link_job_matches_a_planned_task_before_appending(db, tmp_path):
    """The project tab and the board must be one account of the work: a job for
    a task the PM already proposed lights up *that* row."""
    from bastet_agent_os import project_lifecycle as lifecycle
    from bastet_agent_os.db import now as _now

    db.write("INSERT INTO projects(id, team_id, repo_path, created_at, updated_at) "
             "VALUES('p','t','/x',?,?)", (_now(), _now()))
    lifecycle.save_task_plan(db, "p", [{"title": "後端 API", "spec": "s"},
                                       {"title": "前端頁面", "spec": "s"}],
                             by="pm", confirmed=True)

    lifecycle.link_job(db, "p", "job1", "前端頁面", "spec", origin="chat")
    tasks = lifecycle.task_plan(db, "p")["tasks"]
    assert len(tasks) == 2                       # matched, did not append
    assert tasks[1]["job_id"] == "job1" and tasks[1]["origin"] == "chat"

    lifecycle.link_job(db, "p", "job1", "前端頁面", "spec")   # idempotent
    assert len(lifecycle.task_plan(db, "p")["tasks"]) == 2

    lifecycle.link_job(db, "p", "job2", "臨時加的事", "spec", origin="chat")
    tasks = lifecycle.task_plan(db, "p")["tasks"]
    assert len(tasks) == 3 and tasks[2]["job_id"] == "job2"


async def test_dispatch_links_the_job_and_moves_the_light(orch, seeded):
    """Live finding: a job was executing while the project card still read
    規劃中, and the plan had no idea the job existed."""
    from bastet_agent_os import project_lifecycle as lifecycle

    seeded.write("UPDATE projects SET status='planning' WHERE id='proj1'")
    seeded.write("DELETE FROM runs")
    seeded.write("DELETE FROM jobs")
    lifecycle.save_task_plan(seeded, "proj1", [{"title": "做登入頁", "spec": "spec"}],
                             by="pm", confirmed=True)

    SCRIPT.append(RunResult(status="succeeded", summary="ok"))
    job_id = orch.dispatch(req(title="做登入頁"), actor="user:manfred")
    # the light moves the moment work exists, not when someone remembers to click
    assert lifecycle.status_of(seeded, "proj1") == lifecycle.RUNNING
    tasks = lifecycle.task_plan(seeded, "proj1")["tasks"]
    assert tasks[0]["job_id"] == job_id           # the planned row, not a new one

    await orch.wait_idle()
    # every planned task has a finished job → awaiting acceptance
    assert lifecycle.status_of(seeded, "proj1") == lifecycle.MAINTENANCE
    plan = lifecycle.plan_with_jobs(seeded, "proj1")
    assert plan["tasks"][0]["job_status"] == "done"


async def test_the_light_does_not_finish_a_project_mid_plan(orch, seeded):
    """One task done out of three is not "finished" — that would stop a run."""
    from bastet_agent_os import project_lifecycle as lifecycle

    seeded.write("UPDATE projects SET status='planning' WHERE id='proj1'")
    seeded.write("DELETE FROM runs")
    seeded.write("DELETE FROM jobs")
    lifecycle.save_task_plan(seeded, "proj1",
                             [{"title": "A", "spec": "a"}, {"title": "B", "spec": "b"},
                              {"title": "C", "spec": "c"}], by="pm", confirmed=True)
    SCRIPT.append(RunResult(status="succeeded"))
    orch.dispatch(req(title="A"))
    await orch.wait_idle()
    assert lifecycle.status_of(seeded, "proj1") == lifecycle.RUNNING   # B and C remain


async def test_retry_picks_up_the_projects_current_workflow_and_spec(orch, seeded):
    """Retrying with the state that already failed just fails again."""
    from fake_executor import add_template

    add_template(seeded, "old", [{"name": "work", "gate": "auto"}])
    add_template(seeded, "new", [{"name": "work", "gate": "auto"},
                                 {"name": "review", "gate": "auto"}])
    seeded.write("UPDATE projects SET default_template_id='old' WHERE id='proj1'")
    SCRIPT.append(RunResult(status="failed", summary="wrong spec"))
    job_id = orch.dispatch(req(template_id="old"))
    await orch.wait_idle()

    # operator fixes the plan: new workflow, corrected spec
    seeded.write("UPDATE projects SET default_template_id='new' WHERE id='proj1'")
    captured = {}
    SCRIPT.append(lambda task: (captured.update(prompt=task.prompt)
                                or RunResult(status="succeeded")))
    SCRIPT.append(RunResult(status="succeeded"))     # the new second stage
    out = orch.retry(job_id, spec="corrected spec with the real requirements")
    assert out["workflow_refreshed"] == "new v1"
    await orch.wait_idle()

    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["template_id"] == "new"
    assert "corrected spec" in job["spec_md"]
    assert "corrected spec" in captured["prompt"]    # the agent saw the new spec
    assert "review" in job["stages_snapshot_json"]   # and the new pipeline
    assert job["status"] == "done"


async def test_retry_keeps_its_snapshot_when_the_new_workflow_lacks_the_stage(orch,
                                                                             seeded):
    """A template swap that drops the stage we are parked on must not strand the
    job — better to finish on the snapshot it started with."""
    from fake_executor import add_template

    add_template(seeded, "orig", [{"name": "work", "gate": "auto"}])
    add_template(seeded, "elsewhere", [{"name": "totally-different", "gate": "auto"}])
    seeded.write("UPDATE projects SET default_template_id='orig' WHERE id='proj1'")
    SCRIPT.append(RunResult(status="failed", summary="boom"))
    job_id = orch.dispatch(req(template_id="orig"))
    await orch.wait_idle()

    seeded.write("UPDATE projects SET default_template_id='elsewhere' WHERE id='proj1'")
    SCRIPT.append(RunResult(status="succeeded"))
    out = orch.retry(job_id)
    assert out["workflow_refreshed"] is None
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"


async def test_reconcile_heals_a_project_whose_state_predates_the_fix(orch, seeded):
    """Event-driven sync only helps work dispatched after it shipped. A job that
    already existed — or a control plane restarted mid-run — must heal too."""
    from bastet_agent_os import project_lifecycle as lifecycle
    from bastet_agent_os.db import now as _now

    seeded.write("DELETE FROM runs")
    seeded.write("DELETE FROM jobs")
    seeded.write("UPDATE projects SET status='planning' WHERE id='proj1'")
    lifecycle.save_task_plan(seeded, "proj1", [{"title": "既有任務", "spec": "s"}],
                             by="pm", confirmed=True)
    # a job created behind the lifecycle's back, exactly like the live project
    seeded.write("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, stage, "
                 "status, spec_md, created_at, updated_at) VALUES('old','proj1','[]',"
                 "'既有任務','work','in_progress','spec',?,?)", (_now(), _now()))

    result = lifecycle.reconcile(seeded, "proj1", actor="server")
    assert result["linked"] == 1 and result["status"] == lifecycle.RUNNING
    tasks = lifecycle.task_plan(seeded, "proj1")["tasks"]
    assert len(tasks) == 1 and tasks[0]["job_id"] == "old"   # matched, not appended

    again = lifecycle.reconcile(seeded, "proj1")
    assert again == {"linked": 0, "status": None}            # idempotent


async def test_retry_picks_up_an_edited_template_not_just_a_swapped_one(orch, seeded):
    """Live case: the E2E stage's test command was fixed in place (same template,
    new version) and the retry kept running the old command, because the refresh
    only triggered when the project switched to a *different* template."""
    # on_fail: block keeps this test about the template refresh; the rework loop
    # has its own tests (tests/test_rework.py)
    add_template(seeded, "web", [{"name": "e2e", "gate": "tests-pass",
                                  "on_fail": "block",
                                  "gate_config": {"command": "npm run test:e2e"}}])
    seeded.write("UPDATE projects SET default_template_id='web' WHERE id='proj1'")
    SCRIPT.append(RunResult(status="succeeded"))
    job_id = orch.dispatch(req(template_id="web"))
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] \
        == "blocked"                       # npm is not there: config error

    # fix the command in place, exactly as the Templates tab now allows
    # (same id, version + 1 — what POST /api/templates does)
    seeded.write("INSERT OR REPLACE INTO workflow_templates(id, name, version, "
                 "stages_json) VALUES('web','web',2,?)",
                 (json.dumps([{"name": "e2e", "gate": "tests-pass",
                               "on_fail": "block",
                               "gate_config": {"command": "exit 0"}}]),))
    SCRIPT.append(RunResult(status="succeeded"))
    out = orch.retry(job_id)
    assert out["workflow_refreshed"] == "web v2"     # it noticed the stages changed
    await orch.wait_idle()

    snapshot = json.loads(seeded.one("SELECT stages_snapshot_json FROM jobs WHERE id=?",
                                     (job_id,))["stages_snapshot_json"])
    assert snapshot[0]["gate_config"]["command"] == "exit 0"
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"


async def test_retry_refuses_a_stale_compatible_execution_contract(orch, seeded):
    """Opting out of refresh must not bypass a newly deployed capability path."""
    add_template(seeded, "web", [{"name": "review", "gate": "agent-review",
                                   "read_only": True, "on_fail": "block"}])
    seeded.write("UPDATE projects SET default_template_id='web' WHERE id='proj1'")
    SCRIPT.append(RunResult(status="failed", summary="old environment"))
    job_id = orch.dispatch(req(template_id="web"))
    await orch.wait_idle()

    seeded.write("INSERT OR REPLACE INTO workflow_templates(id,name,version,stages_json) "
                 "VALUES('web','web',2,?)", (json.dumps([{
                     "name": "review", "gate": "agent-review", "read_only": True,
                     "on_fail": "block", "requires": ["browser.playwright"],
                     "gate_config": {"precheck_command": "exit 0"},
                 }]),))

    with pytest.raises(ValueError, match="refresh_workflow=true"):
        orch.retry(job_id, refresh_workflow=False)
    job = seeded.one("SELECT status,stages_snapshot_json FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "blocked"
    assert "browser.playwright" not in job["stages_snapshot_json"]


async def test_retry_renews_recovery_budget_only_when_explicit(orch, seeded):
    add_template(seeded, "dev", [{"name": "work", "gate": "auto"}])
    SCRIPT.append(RunResult(status="failed", summary="boom"))
    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()
    seeded.write("UPDATE jobs SET rework_count=3 WHERE id=?", (job_id,))

    SCRIPT.append(RunResult(status="failed", summary="still broken"))
    out = orch.retry(job_id)
    assert out["recovery_lease_renewed"] is False
    await orch.wait_idle()
    assert seeded.one("SELECT rework_count FROM jobs WHERE id=?", (job_id,))[
        "rework_count"] == 3

    SCRIPT.append(RunResult(status="succeeded", summary="fixed"))
    out = orch.retry(job_id, renew_recovery_lease=True)
    assert out["recovery_lease_renewed"] is True
    await orch.wait_idle()
    assert seeded.one("SELECT rework_count FROM jobs WHERE id=?", (job_id,))[
        "rework_count"] == 0


async def test_ruling_retry_starts_at_the_writable_rework_target(orch, seeded):
    """A ruling is work for the implementer, not another look at the old diff."""
    add_template(seeded, "dev", [
        {"name": "implement", "gate": "auto"},
        {"name": "review", "gate": "agent-review", "read_only": True,
         "rework_target": "implement", "on_fail": "block"},
    ])
    SCRIPT.append(RunResult(status="succeeded", summary="old implementation"))
    SCRIPT.append(RunResult(
        status="succeeded", summary="reject",
        structured_verdict={"verdict": "reject", "reasons": ["change script"]}))
    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()
    assert seeded.one("SELECT stage,status FROM jobs WHERE id=?", (job_id,))[
        "status"] == "blocked"

    SCRIPT.append(RunResult(status="succeeded", summary="ruling implemented"))
    SCRIPT.append(RunResult(
        status="succeeded", summary="approve",
        structured_verdict={"verdict": "approve", "reasons": []}))
    out = orch.retry(job_id, restart_from_rework_target=True)
    assert out["stage"] == "implement"
    assert out["restart_from_rework_target"] is True
    await orch.wait_idle()

    rows = seeded.query(
        "SELECT stage FROM runs WHERE job_id=? ORDER BY rowid", (job_id,))
    assert [row["stage"] for row in rows] == [
        "implement", "review", "implement", "review"]
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))[
        "status"] == "done"
