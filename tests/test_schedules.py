"""Recurring workflows are durable, timezone-aware and dispatch exactly once."""

from datetime import UTC, datetime

import pytest

from bastet_agent_os import schedules
from bastet_agent_os.db import now
from bastet_agent_os.maintenance_mode import MaintenanceModeError


class StubOrchestrator:
    def __init__(self, db, *, maintenance: bool = False):
        self.db = db
        self.maintenance = maintenance
        self.requests = []

    def dispatch(self, request, actor=""):
        if self.maintenance:
            raise MaintenanceModeError("maintenance fence is active")
        self.requests.append((request, actor))
        job_id = f"scheduled-job-{len(self.requests)}"
        stamp = now()
        self.db.write(
            "INSERT INTO jobs(id,project_id,stages_snapshot_json,title,stage,status,"
            "created_at,updated_at) VALUES(?,?, '[]',?,'queued','in_progress',?,?)",
            (job_id, request.project_id, request.title, stamp, stamp))
        return job_id


@pytest.fixture
def schedule_db(seeded):
    seeded.write("DELETE FROM runs")
    seeded.write("DELETE FROM jobs")
    seeded.write(
        "INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
        "VALUES('proj1','ag1','engineer',10)")
    return seeded


def test_cron_supports_ranges_steps_sunday_and_timezone():
    assert schedules.parse_cron("5/10 9-17 * * 1-5")[0] == set(range(5, 60, 10))
    assert schedules.parse_cron("0 0 * * 7")[4] == {0}
    after = datetime(2026, 8, 31, 1, 1, tzinfo=UTC)  # 09:01 in Taipei, Monday
    assert schedules.next_run("*/15 9-17 * * 1-5", "Asia/Taipei", after) \
        == "2026-08-31T01:15:00+00:00"
    with pytest.raises(schedules.ScheduleError):
        schedules.parse_cron("0 25 * * *")
    with pytest.raises(schedules.ScheduleError):
        schedules.next_run("0 0 * * *", "Mars/Olympus", after)


def test_schedule_crud_recomputes_due_time(schedule_db):
    item = schedules.create(schedule_db, "proj1", {
        "name": "daily", "cron": "0 9 * * 1-5", "timezone": "Asia/Taipei",
        "prompt": "Run the daily verification", "role": "engineer",
    }, "alice")
    assert item["enabled"] is True
    assert item["timeout_s"] == 3600
    assert item["delivery"] == {"mode": "none"}
    old_due = item["next_run_at"]

    changed = schedules.update(schedule_db, item["id"], {
        "cron": "30 10 * * 1-5", "enabled": False, "agent_id": "ag1",
    }, "alice")
    assert changed["enabled"] is False
    assert changed["agent_id"] == "ag1"
    assert changed["next_run_at"] != old_due
    assert schedules.list_for_project(schedule_db, "proj1")[0]["id"] == item["id"]
    with pytest.raises(schedules.ScheduleError, match="not found"):
        schedules.update(schedule_db, item["id"], {"agent_id": "ghost"}, "alice")

    schedules.delete(schedule_db, item["id"], "alice")
    assert schedules.list_for_project(schedule_db, "proj1") == []


def test_due_occurrence_dispatches_once_and_restart_does_not_duplicate(schedule_db):
    item = schedules.create(schedule_db, "proj1", {
        "name": "check", "cron": "* * * * *", "timezone": "UTC",
        "prompt": "Verify", "role": "engineer",
    }, "alice")
    schedule_db.write("UPDATE workflow_schedules SET next_run_at=? WHERE id=?",
                      ("2026-08-31T00:00:00+00:00", item["id"]))
    orch = StubOrchestrator(schedule_db)
    first = schedules.WorkflowScheduler(schedule_db, orch).tick(
        "2026-08-31T00:00:30+00:00")
    second = schedules.WorkflowScheduler(schedule_db, orch).tick(
        "2026-08-31T00:00:40+00:00")

    assert first == ["scheduled-job-1"]
    assert second == []
    assert len(orch.requests) == 1
    request, actor = orch.requests[0]
    assert actor == "scheduler"
    assert request.origin == "schedule"
    assert request.plan_key == f"schedule:{item['id']}"
    assert request.task_id == "2026-08-31T00:00:00+00:00"
    assert schedule_db.one(
        "SELECT status FROM workflow_schedule_runs WHERE schedule_id=?",
        (item["id"],))["status"] == "dispatched"


def test_maintenance_claim_is_replayed_after_fence_opens(schedule_db):
    item = schedules.create(schedule_db, "proj1", {
        "name": "check", "cron": "* * * * *", "timezone": "UTC",
        "prompt": "Verify", "agent_id": "ag1",
    }, "alice")
    schedule_db.write("UPDATE workflow_schedules SET next_run_at=? WHERE id=?",
                      ("2026-08-31T00:00:00+00:00", item["id"]))
    orch = StubOrchestrator(schedule_db, maintenance=True)
    scheduler = schedules.WorkflowScheduler(schedule_db, orch)
    assert scheduler.tick("2026-08-31T00:00:30+00:00") == []
    assert schedule_db.one(
        "SELECT status FROM workflow_schedule_runs WHERE schedule_id=?",
        (item["id"],))["status"] == "claimed"

    orch.maintenance = False
    assert scheduler.tick("2026-08-31T00:00:40+00:00") == ["scheduled-job-1"]


def test_active_previous_job_skips_overlap(schedule_db):
    item = schedules.create(schedule_db, "proj1", {
        "name": "check", "cron": "* * * * *", "timezone": "UTC",
        "prompt": "Verify", "agent_id": "ag1",
    }, "alice")
    stamp = now()
    schedule_db.write(
        "INSERT INTO jobs(id,project_id,stages_snapshot_json,title,stage,status,"
        "created_at,updated_at) VALUES('prior','proj1','[]','prior','work',"
        "'in_progress',?,?)", (stamp, stamp))
    schedule_db.write(
        "UPDATE workflow_schedules SET last_job_id='prior',next_run_at=? WHERE id=?",
        ("2026-08-31T00:00:00+00:00", item["id"]))
    orch = StubOrchestrator(schedule_db)
    assert schedules.WorkflowScheduler(schedule_db, orch).tick(
        "2026-08-31T00:00:30+00:00") == []
    run = schedule_db.one(
        "SELECT status,error FROM workflow_schedule_runs WHERE schedule_id=?",
        (item["id"],))
    assert run["status"] == "skipped"
    assert "still active" in run["error"]


def test_schedule_api_validates_and_persists(tmp_path):
    from fastapi.testclient import TestClient

    from bastet_agent_os.config import Home
    from bastet_agent_os.server import create_app

    home = Home(tmp_path / "home")
    with TestClient(create_app(home), base_url="http://127.0.0.1") as client:
        client.headers["Authorization"] = f"Bearer {home.api_token()}"
        client.post("/api/teams", json={"id": "t1", "name": "T"})
        client.post("/api/projects", json={
            "id": "p1", "repo_path": str(tmp_path), "team_id": "t1"})
        bad = client.post("/api/projects/p1/schedules", json={
            "name": "bad", "cron": "tomorrow", "prompt": "x"})
        assert bad.status_code == 409
        made = client.post("/api/projects/p1/schedules", json={
            "name": "nightly", "cron": "0 2 * * *", "timezone": "Asia/Taipei",
            "prompt": "Run nightly checks", "enabled": False})
        assert made.status_code == 200, made.text
        schedule_id = made.json()["id"]
        assert client.get("/api/projects/p1/schedules").json()[0]["id"] == schedule_id
        changed = client.put(f"/api/schedules/{schedule_id}", json={"enabled": True})
        assert changed.status_code == 200
        assert changed.json()["enabled"] is True
        assert client.delete(f"/api/schedules/{schedule_id}").status_code == 200
