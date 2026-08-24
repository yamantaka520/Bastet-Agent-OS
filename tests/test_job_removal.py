"""Getting a finished card off the board without losing operational history.

Archive and the compatibility DELETE endpoint are both reversible. Jobs, runs,
task-plan links and accounting must survive every board-removal operation.
"""

import pytest
from fake_executor import SCRIPT, req

from bastet_agent_os import project_lifecycle as lifecycle
from bastet_agent_os.db import now
from bastet_agent_os.executors.base import RunResult


@pytest.fixture
def job(orch, seeded):
    seeded.write("DELETE FROM runs")
    seeded.write("DELETE FROM jobs")
    seeded.write("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, stage, "
                 "status, spec_md, created_at, updated_at) VALUES('j1','proj1','[]',"
                 "'一次失敗的嘗試','work','cancelled','spec',?,?)", (now(), now()))
    seeded.write("INSERT INTO runs(id, job_id, stage, agent_id, executor_type, status) "
                 "VALUES('r1','j1','work','ag1','fake','cancelled')")
    return orch, seeded


# ---- archive ---------------------------------------------------------------------

async def test_archiving_hides_the_card_and_keeps_everything(job):
    orch, db = job
    orch.archive_job("j1", True, actor="u")
    assert db.one("SELECT archived FROM jobs WHERE id='j1'")["archived"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM runs WHERE job_id='j1'")["n"] == 1
    assert db.query("SELECT * FROM audit_log WHERE action='job.archive'")

    orch.archive_job("j1", False, actor="u")                          # reversible
    assert db.one("SELECT archived FROM jobs WHERE id='j1'")["archived"] == 0


def test_the_board_endpoint_hides_archived_cards(tmp_path):
    """The board must not show them; include_archived brings them back."""
    from fastapi.testclient import TestClient

    from bastet_agent_os.config import Home
    from bastet_agent_os.db import Db
    from bastet_agent_os.server import create_app

    home = Home(tmp_path / "api-home")
    app = create_app(home)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"
    db = Db(home.db_path)
    try:
        ts = now()
        db.write("INSERT INTO projects(id, team_id, repo_path, created_at, updated_at) "
                 "VALUES('p','t','/x',?,?)", (ts, ts))
        for job_id, archived in (("visible", 0), ("hidden", 1)):
            db.write("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, "
                     "stage, status, archived, created_at, updated_at) "
                     "VALUES(?,'p','[]',?,'work','cancelled',?,?,?)",
                     (job_id, job_id, archived, ts, ts))
    finally:
        db.close()
    assert [j["id"] for j in client.get("/api/jobs?project_id=p").json()] == ["visible"]
    both = {j["id"] for j in
            client.get("/api/jobs?project_id=p&include_archived=true").json()}
    assert both == {"visible", "hidden"}


async def test_a_running_card_cannot_be_archived(job):
    orch, db = job
    db.write("UPDATE jobs SET status='in_progress' WHERE id='j1'")
    with pytest.raises(ValueError, match="已結束"):
        orch.archive_job("j1", True)


# ---- delete ----------------------------------------------------------------------

async def test_deleting_a_cancelled_card_archives_it_and_keeps_history(job):
    orch, db = job
    lifecycle.save_task_plan(db, "proj1", [
        {"title": "PM 規劃的事", "spec": "s", "job_id": "j1"}], by="pm", confirmed=True)

    out = orch.delete_job("j1", actor="u")
    assert out == {"deleted": "j1", "archived": True,
                   "recoverable": True, "runs": 1}
    assert db.one("SELECT archived FROM jobs WHERE id='j1'")["archived"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM runs WHERE job_id='j1'")["n"] == 1
    # The task-plan link survives too, so unarchive restores the same card.
    tasks = lifecycle.task_plan(db, "proj1")["tasks"]
    assert len(tasks) == 1 and tasks[0]["job_id"] == "j1"
    assert db.query("SELECT * FROM audit_log WHERE action='job.delete'")


async def test_deleting_a_card_the_dispatch_created_keeps_its_plan_row(job):
    orch, db = job
    lifecycle.save_task_plan(db, "proj1", [
        {"title": "一次失敗的嘗試", "spec": "s", "job_id": "j1", "origin": "chat"}],
        by="pm", confirmed=True)
    assert orch.delete_job("j1")["recoverable"] is True
    assert lifecycle.task_plan(db, "proj1")["tasks"][0]["job_id"] == "j1"


async def test_delete_with_spend_is_recoverable_and_keeps_accounting(job):
    orch, db = job
    db.write("INSERT INTO usage_ledger(id, run_id, resource_id, model, tokens_in, "
             "tokens_out, cost_usd, at) VALUES('u1','r1','res1','m',10,5,0.42,?)",
             (now(),))
    assert orch.delete_job("j1")["recoverable"] is True
    assert db.one("SELECT archived FROM jobs WHERE id='j1'")["archived"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM usage_ledger")["n"] == 1


async def test_a_running_card_cannot_be_deleted(job):
    orch, db = job
    db.write("UPDATE jobs SET status='in_progress' WHERE id='j1'")
    with pytest.raises(ValueError, match="已結束"):
        orch.delete_job("j1")


async def test_deleting_the_last_job_keeps_project_history(orch, seeded):
    """Removing the last visible card must not erase project progress."""
    seeded.write("DELETE FROM runs")
    seeded.write("DELETE FROM jobs")
    lifecycle.save_task_plan(seeded, "proj1", [{"title": "只有這件事", "spec": "s"}],
                             by="pm", confirmed=True)
    SCRIPT.append(RunResult(status="succeeded"))
    job_id = orch.dispatch(req(title="只有這件事"))
    await orch.wait_idle()
    seeded.write("UPDATE jobs SET status='cancelled' WHERE id=?", (job_id,))
    orch.delete_job(job_id, actor="u")
    progress = lifecycle.job_progress(seeded, "proj1")
    assert progress["total"] == 1
    assert progress["cancelled"] == 1
