"""Project-level supervision closes the gap between a heartbeat and ownership."""

import json

import pytest

from bastet_agent_os import maintenance_mode
from bastet_agent_os.db import now
from bastet_agent_os.orchestrator import DispatchRequest


@pytest.mark.asyncio
async def test_maintenance_parks_before_the_next_run_and_resumes_after_release(
        orch, seeded):
    """A drain entered at a stage boundary must not create a ghost running run."""
    seeded.write("UPDATE jobs SET stages_snapshot_json=?, stage='build', "
                 "status='in_progress', default_agent_id='fakebot', resource_id=NULL "
                 "WHERE id='job1'", (json.dumps([
                     {"name": "build", "role": "engineer", "gate": "auto"},
                     {"name": "review", "role": "reviewer", "gate": "auto"},
                 ]),))
    seeded.write("UPDATE runs SET status='succeeded' WHERE id='run1'")
    maintenance_mode.enter(seeded, "admin", "deploy")
    req = DispatchRequest(project_id="proj1", prompt="x", title="t",
                          agent_id="fakebot")

    await orch._drive_job("job1", req)

    job = seeded.one("SELECT status,rework_note FROM jobs WHERE id='job1'")
    assert job["status"] == "blocked" and "maintenance drain" in job["rework_note"]
    assert seeded.one("SELECT COUNT(*) AS n FROM runs WHERE job_id='job1'")["n"] == 1
    assert maintenance_mode.state(seeded)["drained"] is True

    maintenance_mode.leave(seeded, "admin")
    called = []
    orch.retry = lambda job_id, **kw: called.append((job_id, kw)) or {}
    result = await orch.supervise_once()
    assert result["resumed"] == ["job1"]
    assert called == [("job1", {"user": "server:maintenance-release"})]


@pytest.mark.asyncio
async def test_supervisor_retries_max_turns_with_an_alternate_agent(orch, seeded):
    seeded.write("INSERT INTO agents(id,amos_agent_id,name,executor_type,enabled,"
                 "config_json,created_at,updated_at) "
                 "VALUES('backup','backup','Backup','fake',1,'{}',?,?)", (now(), now()))
    seeded.write("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
                 "VALUES('proj1','backup','engineer',50)")
    seeded.write("UPDATE jobs SET status='blocked', stage='build', "
                 "stages_snapshot_json=? WHERE id='job1'",
                 (json.dumps([{"name": "build", "role": "engineer", "gate": "auto"}]),))
    seeded.write("UPDATE runs SET status='failed', error='Error: max turns reached', "
                 "agent_id='fakebot' WHERE id='run1'")
    called = []
    orch.retry = lambda job_id, agent_id="", user="": called.append(
        (job_id, agent_id, user)) or {"job_id": job_id}

    result = await orch.supervise_once()

    assert result["retried"] == ["job1"]
    assert called == [("job1", "backup", "supervisor")]
    audit = seeded.one("SELECT detail_json FROM audit_log "
                       "WHERE action='job.supervisor_retry'")
    assert "max turns reached" in audit["detail_json"]


@pytest.mark.asyncio
async def test_supervisor_never_approves_or_retries_a_human_gate(orch, seeded):
    seeded.write("UPDATE jobs SET status='blocked' WHERE id='job1'")
    seeded.write("UPDATE runs SET status='succeeded', error=NULL WHERE id='run1'")
    seeded.write("INSERT INTO gate_results(id,run_id,gate_type,verdict,reviewer_kind," 
                 "reviewer_id,detail_md,at) VALUES('g1','run1','human-approve',"
                 "'pending','agent','fakebot','waiting',?)", (now(),))
    called = []
    orch.retry = lambda *a, **k: called.append((a, k))

    result = await orch.supervise_once()

    assert result["retried"] == []
    assert called == []


@pytest.mark.asyncio
async def test_old_pending_gate_does_not_hide_a_later_executor_failure(orch, seeded):
    seeded.write("UPDATE jobs SET status='blocked' WHERE id='job1'")
    seeded.write("UPDATE runs SET status='failed', error='Error: max turns reached' "
                 "WHERE id='run1'")
    seeded.write("INSERT INTO gate_results(id,run_id,gate_type,verdict,reviewer_kind,"
                 "reviewer_id,detail_md,at) VALUES('g1','run1','human-approve',"
                 "'pending','agent','fakebot','waiting','2026-01-01T00:00:00+00:00')")
    seeded.write("INSERT INTO gate_results(id,run_id,gate_type,verdict,reviewer_kind,"
                 "reviewer_id,detail_md,at) VALUES('g2','run1','human-approve',"
                 "'passed','user','root','approved','2026-01-01T00:01:00+00:00')")
    called = []
    orch.retry = lambda job_id, agent_id="", user="": called.append(job_id)

    await orch.supervise_once()

    assert called == ["job1"]


@pytest.mark.asyncio
async def test_supervisor_does_not_duplicate_a_driver_while_gate_runs(orch, seeded):
    seeded.write("UPDATE runs SET status='succeeded' WHERE id='run1'")
    orch._driving_jobs.add("job1")
    spawned = []
    orch._spawn = lambda coro: spawned.append(coro)

    await orch.supervise_once()

    assert spawned == []


@pytest.mark.asyncio
async def test_a_quiet_but_alive_run_is_left_alone(orch, seeded):
    """The live execution: an agent reported "FPS bench is still running.
    Waiting for it (20 levels × 60s)", beat every 20 seconds to prove it was
    alive, and was killed at the 15-minute silence mark — four times, growing
    to 42 minutes, across two agents. A 20-minute test can never finish inside
    a 15-minute patience."""
    from datetime import UTC, datetime, timedelta

    long_ago = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    just_now = datetime.now(UTC).isoformat()
    seeded.write("UPDATE runs SET status='running', started_at=?, heartbeat_at=?, "
                 "progress_at=?, progress_text=? WHERE id='run1'",
                 (long_ago, just_now, long_ago,
                  "FPS bench is still running. Waiting for it (20 levels × 60s)"))
    cancelled = []

    class Handle:
        pass

    class Exec:
        async def cancel(self, handle):
            cancelled.append(True)

    orch._live["run1"] = (Exec(), Handle())
    result = await orch.supervise_once()

    assert cancelled == [], "a run that is alive and honest about waiting was killed"
    assert result["interrupted"] == []


@pytest.mark.asyncio
async def test_a_run_whose_heartbeat_stopped_is_interrupted(orch, seeded):
    """The other half: a genuinely dead run must still be reclaimed."""
    from datetime import UTC, datetime, timedelta

    long_ago = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    seeded.write("UPDATE runs SET status='running', started_at=?, heartbeat_at=?, "
                 "progress_at=? WHERE id='run1'", (long_ago, long_ago, long_ago))
    cancelled = []

    class Exec:
        async def cancel(self, handle):
            cancelled.append(True)

    orch._live["run1"] = (Exec(), object())
    result = await orch.supervise_once()

    assert result["interrupted"] == ["run1"] and cancelled == [True]
    detail = seeded.one("SELECT detail_json FROM audit_log "
                        "WHERE action='run.stalled_interrupted'")["detail_json"]
    assert "last_alive" in detail      # the record says which fact decided it
