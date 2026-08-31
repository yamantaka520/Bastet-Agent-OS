"""A project cost ceiling parks new work and resumes on the next local day."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from fake_executor import SCRIPT, req
from fastapi.testclient import TestClient

from bastet_agent_os import project_budget, project_lifecycle, project_runner
from bastet_agent_os.config import Home
from bastet_agent_os.db import Db, now
from bastet_agent_os.executors.base import RunResult
from bastet_agent_os.governance import QuotaError
from bastet_agent_os.server import create_app


def _configure(db, limit=1.0, timezone="Asia/Taipei"):
    db.write("UPDATE projects SET config_json=? WHERE id='proj1'",
             (json.dumps({"daily_cost_limit_usd": limit,
                          "daily_cost_timezone": timezone}),))


def test_daily_project_spend_combines_gateway_and_reported_runs(seeded):
    _configure(seeded, 2.0)
    seeded.write("UPDATE runs SET status='succeeded',accounting_precision='gateway',"
                 "cost_usd=99,started_at='2026-08-31T16:05:00+00:00',"
                 "finished_at='2026-08-31T16:06:00+00:00' WHERE id='run1'")
    seeded.write("INSERT INTO usage_ledger(id,run_id,resource_id,cost_usd,at) "
                 "VALUES('u1','run1','res1',0.7,'2026-08-31T16:05:00+00:00')")
    seeded.write("INSERT INTO runs(id,job_id,stage,agent_id,executor_type,resource_id,"
                 "status,cost_usd,accounting_precision,started_at,finished_at) VALUES(" 
                 "'reported','job1','work','ag1','fake','res1','succeeded',0.6,"
                 "'reported','2026-08-31T16:10:00+00:00','2026-08-31T16:11:00+00:00')")

    value = project_budget.status(
        seeded, "proj1", at=datetime(2026, 8, 31, 16, 30, tzinfo=UTC))
    assert value["period_start"] == "2026-08-31T16:00:00+00:00"
    assert value["resets_at"] == "2026-09-01T16:00:00+00:00"
    assert value["spent_usd"] == pytest.approx(1.3)
    assert value["remaining_usd"] == pytest.approx(0.7)
    assert value["exceeded"] is False


def test_pause_receipt_is_once_only_and_next_day_resumes(seeded):
    _configure(seeded, 0.5, "UTC")
    seeded.write("UPDATE runs SET status='succeeded',accounting_precision='reported',"
                 "cost_usd=0.6,started_at='2026-08-31T08:00:00+00:00',"
                 "finished_at='2026-08-31T08:01:00+00:00' WHERE id='run1'")
    today = datetime(2026, 8, 31, 12, tzinfo=UTC)

    first, event = project_budget.sync_pause(seeded, "proj1", at=today)
    again, duplicate_event = project_budget.sync_pause(seeded, "proj1", at=today)
    assert first["paused"] is True and again["paused"] is True
    assert event == "budget.exceeded"
    assert duplicate_event == ""
    assert seeded.one("SELECT COUNT(*) AS n FROM project_cost_pauses")["n"] == 1
    assert seeded.one("SELECT COUNT(*) AS n FROM audit_log WHERE "
                      "action='project.budget_paused'")["n"] == 1

    tomorrow = datetime(2026, 9, 1, 12, tzinfo=UTC)
    resumed, event = project_budget.sync_pause(seeded, "proj1", at=tomorrow)
    assert resumed["spent_usd"] == 0
    assert resumed["paused"] is False
    assert event == "budget.resumed"
    assert seeded.one("SELECT resumed_at FROM project_cost_pauses")["resumed_at"]


def test_new_dispatch_is_refused_without_mutating_the_existing_job(
        orch, seeded):
    _configure(seeded, 0.5, "UTC")
    seeded.write("UPDATE runs SET status='succeeded',accounting_precision='reported',"
                 "cost_usd=0.6,started_at=datetime('now'),finished_at=datetime('now') "
                 "WHERE id='run1'")
    before = seeded.one("SELECT COUNT(*) AS n FROM jobs")["n"]

    with pytest.raises(QuotaError, match="daily cost ceiling reached") as caught:
        orch.dispatch(req())
    assert caught.value.policy == "queue"
    assert seeded.one("SELECT COUNT(*) AS n FROM jobs")["n"] == before


async def test_project_runner_waits_without_failing_then_resumes(
        orch, seeded, monkeypatch):
    monkeypatch.setattr(project_runner, "POLL_S", 0.01)
    _configure(seeded, 0.5, "UTC")
    seeded.write("DELETE FROM runs")
    seeded.write("DELETE FROM jobs")
    stamp = now()
    seeded.write_many([
        ("INSERT INTO jobs(id,project_id,stages_snapshot_json,title,stage,status,"
         "created_at,updated_at) VALUES('spent','proj1','[]','Spent','work','done',?,?)",
         (stamp, stamp)),
        ("INSERT INTO runs(id,job_id,stage,agent_id,executor_type,status,cost_usd,"
         "accounting_precision,started_at,finished_at) VALUES('spent-run','spent','work',"
         "'fakebot','fake','succeeded',0.6,'reported',?,?)", (stamp, stamp)),
    ])
    project_lifecycle.save_task_plan(seeded, "proj1", [
        {"id": "spent-task", "title": "Spent", "spec": "done", "needs": [],
         "job_id": "spent", "delivery": {"mode": "none"}},
        {"id": "next-task", "title": "Next", "spec": "continue",
         "needs": ["spent-task"], "delivery": {"mode": "none"}},
    ], by="test", confirmed=True)
    seeded.write("UPDATE projects SET status='running' WHERE id='proj1'")
    SCRIPT.append(lambda task: RunResult(status="succeeded", summary="continued"))
    runner = project_runner.ProjectRunner(seeded, orch)
    runner.start("proj1", "fakebot", actor="test")

    await asyncio.sleep(0.05)
    assert seeded.one("SELECT COUNT(*) AS n FROM jobs")["n"] == 1
    assert runner.is_active("proj1") is True
    assert seeded.one("SELECT status FROM projects WHERE id='proj1'")["status"] == "running"

    seeded.write("UPDATE runs SET cost_usd=0 WHERE id='spent-run'")
    for _ in range(100):
        if seeded.one("SELECT COUNT(*) AS n FROM jobs")["n"] == 2:
            break
        await asyncio.sleep(0.01)
    await orch.wait_idle()
    assert seeded.one("SELECT COUNT(*) AS n FROM jobs")["n"] == 2
    assert seeded.one("SELECT COUNT(*) AS n FROM audit_log WHERE "
                      "action='project.budget_resumed'")["n"] == 1


@pytest.mark.parametrize(("limit", "timezone", "message"), [
    (0, "UTC", "greater than zero"),
    (float("nan"), "UTC", "greater than zero"),
    (1, "Moon/Base", "unknown IANA timezone"),
])
def test_invalid_budget_configuration_fails_closed(limit, timezone, message):
    with pytest.raises(project_budget.ProjectBudgetError, match=message):
        project_budget.validate(limit, timezone)


def test_project_api_configures_reports_and_disables_the_ceiling(tmp_path):
    home = Home(tmp_path / "home")
    client = TestClient(create_app(home), base_url="http://127.0.0.1")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"
    db = Db(home.db_path)
    stamp = now()
    try:
        db.write("INSERT INTO projects(id,team_id,repo_path,created_at,updated_at) "
                 "VALUES('p','t','',?,?)", (stamp, stamp))
    finally:
        db.close()

    configured = client.put("/api/projects/p", json={
        "daily_cost_limit_usd": 3.5,
        "daily_cost_timezone": "Asia/Taipei",
    })
    assert configured.status_code == 200
    row = next(p for p in client.get("/api/projects").json() if p["id"] == "p")
    assert row["budget"]["enabled"] is True
    assert row["budget"]["limit_usd"] == 3.5
    assert row["budget"]["timezone"] == "Asia/Taipei"

    disabled = client.put("/api/projects/p", json={"daily_cost_limit_usd": None})
    assert disabled.status_code == 200
    row = next(p for p in client.get("/api/projects").json() if p["id"] == "p")
    assert row["budget"]["enabled"] is False
