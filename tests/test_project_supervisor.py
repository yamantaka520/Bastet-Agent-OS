"""Project-level supervision closes the gap between a heartbeat and ownership."""

import json

import pytest

from bastet_agent_os.db import now


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
