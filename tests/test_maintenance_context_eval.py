from bastet_agent_os import collaboration, maintenance_mode
from bastet_agent_os.context_engine import build_context
from bastet_agent_os.context_eval import evaluate
from bastet_agent_os.db import now


async def test_human_approval_publishes_handoff_to_project_room(orch, seeded):
    from fake_executor import SCRIPT, add_template, req

    from bastet_agent_os.executors.base import RunResult

    add_template(seeded, "gated-handoff", [
        {"name": "plan", "gate": "human-approve"},
        {"name": "work", "gate": "auto"},
    ])
    SCRIPT.append(RunResult(status="succeeded", summary="approved design contract"))
    job_id = orch.dispatch(req(template_id="gated-handoff"))
    await orch.wait_idle()

    SCRIPT.append(RunResult(status="succeeded", summary="implementation complete"))
    orch.approve(job_id, approved=True, comment="lgtm", user="reviewer")
    await orch.wait_idle()

    handoff = seeded.one(
        "SELECT * FROM stage_handoffs WHERE job_id=? AND from_stage='plan'", (job_id,))
    assert handoff is not None
    assert handoff["to_stage"] == "work"
    assert handoff["summary"] == "approved design contract"
    room = collaboration.messages(seeded, "proj1")
    assert any(message["meta"].get("handoff_id") == handoff["id"] for message in room)


def test_maintenance_fence_tracks_drain_and_is_audited(seeded):
    initial = maintenance_mode.state(seeded)
    assert initial["enabled"] is False
    locked = maintenance_mode.enter(seeded, "test-admin", "release")
    assert locked["enabled"] is True
    assert locked["drained"] is False
    assert locked["active_jobs"] == 1 and locked["active_runs"] == 1

    try:
        maintenance_mode.require_dispatch_allowed(seeded)
        raise AssertionError("dispatch should be fenced")
    except maintenance_mode.MaintenanceModeError:
        pass

    seeded.write("UPDATE runs SET status='succeeded' WHERE id='run1'")
    seeded.write("UPDATE jobs SET status='done' WHERE id='job1'")
    assert maintenance_mode.require_drained(seeded)["drained"] is True
    released = maintenance_mode.leave(seeded, "test-admin")
    assert released["enabled"] is False
    actions = [r["action"] for r in seeded.query(
        "SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["maintenance.enter", "maintenance.leave"]


def test_handoff_delivery_ack_and_context_golden_case(seeded):
    handoff_id = collaboration.record_handoff(
        seeded, project_id="proj1", job_id="job1", run_id="run1",
        from_stage="implement", to_stage="review", agent_id="ag1",
        summary="Implemented selective context", paths=["src/context.py"])
    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")
    text, _ = build_context(seeded, job, "review", stage_role="reviewer",
                            agent_id="ag1")
    assert "Implemented selective context" in text
    delivered = seeded.one("SELECT * FROM stage_handoffs WHERE id=?", (handoff_id,))
    assert delivered["delivered_to_agent_id"] == "ag1"
    assert delivered["delivered_at"]

    ack = collaboration.acknowledge_handoff(
        seeded, handoff_id, agent_id="ag1",
        acknowledgement="Understood changed scope", questions=["migration needed?"])
    assert ack["acknowledged_by"] == "ag1"
    assert ack["acknowledged_at"]

    result = evaluate(
        seeded, job_id="job1", stage="review", role="reviewer",
        expected_buckets=["handoff"], expected_terms=["selective context"],
        forbidden_terms=["secret-token"])
    assert result["passed"] is True
    assert seeded.one("SELECT passed FROM context_evaluations WHERE id=?",
                      (result["id"],))["passed"] == 1


def test_replacement_agent_gets_own_receipt_and_completion_ack(seeded):
    handoff_id = collaboration.record_handoff(
        seeded, project_id="proj1", job_id="job1", run_id="run1",
        from_stage="plan", to_stage="work", agent_id="ag1",
        summary="Use the approved baseline", paths=["spec.md"])
    collaboration.deliver_handoffs(seeded, "job1", "work", "ag1")
    seeded.write("INSERT INTO agents(id,amos_agent_id,name,executor_type,created_at,"
                 "updated_at) VALUES('ag2','ag2','Agent Two','fake',?,?)",
                 (now(), now()))
    collaboration.deliver_handoffs(seeded, "job1", "work", "ag2")

    receipts = seeded.query("SELECT agent_id FROM handoff_receipts "
                            "WHERE handoff_id=? ORDER BY agent_id", (handoff_id,))
    assert [row["agent_id"] for row in receipts] == ["ag1", "ag2"]
    collaboration.acknowledge_delivered_handoffs(
        seeded, job_id="job1", stage="work", agent_id="ag2",
        summary="Understood baseline; implemented against it")
    receipt = seeded.one("SELECT * FROM handoff_receipts WHERE handoff_id=? "
                         "AND agent_id='ag2'", (handoff_id,))
    assert receipt["acknowledged_at"] and "implemented" in receipt["acknowledgement"]
    room = seeded.one("SELECT content FROM room_messages WHERE kind='handoff_ack' "
                      "ORDER BY rowid DESC LIMIT 1")
    assert "implemented" in room["content"]
