import pytest
from fake_executor import SCRIPT

from bastet_agent_os import chat, planning_rounds, project_lifecycle
from bastet_agent_os.db import now
from bastet_agent_os.executors.base import RunResult


def _session(db):
    return chat.create_session(db, scope_type="project", scope_id="proj1",
                               responder_kind="agent", responder_id="ag1",
                               title="Round 1")


def test_round_freezes_source_session_and_rejects_more_messages(seeded):
    session = _session(seeded)
    round_id = planning_rounds.start(seeded, "proj1", session, actor="u")
    planning_rounds.propose(
        seeded, round_id, solution="Build the agreed system",
        negotiation=[{"pm": "scope", "system_analyst": "accepted"}], actor="u")
    planning_rounds.approve(
        seeded, "proj1", [{"id": "build", "title": "Build", "needs": []}], actor="u")

    assert chat.get_session(seeded, session)["state"] == "frozen"
    assert planning_rounds.current(seeded, "proj1")["state"] == "frozen"
    with pytest.raises(chat.ChatError, match="等待區"):
        chat.add_message(seeded, session, role="user", content="one more idea")


def test_intake_is_consumed_into_the_next_round(seeded):
    first = _session(seeded)
    first_round = planning_rounds.start(seeded, "proj1", first)
    planning_rounds.propose(seeded, first_round, solution="v1", negotiation=[])
    planning_rounds.approve(seeded, "proj1", [{"id": "a", "title": "A", "needs": []}])
    seeded.write("UPDATE planning_rounds SET state='accepted' WHERE id=?", (first_round,))
    planning_rounds.add_intake(seeded, "proj1", kind="defect", content="fix keyboard")

    second = chat.create_session(seeded, scope_type="project", scope_id="proj1",
                                 responder_kind="agent", responder_id="ag1",
                                 title="Round 2")
    planning_rounds.start(seeded, "proj1", second)
    body = planning_rounds.overview(seeded, "proj1")
    assert body["round"]["ordinal"] == 2 and body["intake"] == []
    assert "fix keyboard" in chat.messages(seeded, second)[0]["content"]


def test_round_requires_proposal_and_bounded_negotiation(seeded):
    session = _session(seeded)
    round_id = planning_rounds.start(seeded, "proj1", session)
    with pytest.raises(planning_rounds.PlanningRoundError, match="方案"):
        planning_rounds.approve(seeded, "proj1", [])
    with pytest.raises(planning_rounds.PlanningRoundError, match="five"):
        planning_rounds.propose(seeded, round_id, solution="x",
                                negotiation=[{} for _ in range(6)])


def test_project_start_and_acceptance_advance_the_round(seeded):
    session = _session(seeded)
    round_id = planning_rounds.start(seeded, "proj1", session)
    planning_rounds.propose(seeded, round_id, solution="v1", negotiation=[])
    planning_rounds.approve(seeded, "proj1", [{"id": "a", "title": "A", "needs": []}])
    project_lifecycle.apply(seeded, "proj1", "confirm_plan")
    project_lifecycle.apply(seeded, "proj1", "start")
    assert planning_rounds.current(seeded, "proj1")["state"] == "executing"
    project_lifecycle.apply(seeded, "proj1", "stop")
    project_lifecycle.apply(seeded, "proj1", "close")
    assert planning_rounds.current(seeded, "proj1")["state"] == "accepted"


async def test_pm_and_system_analyst_negotiate_visibly_within_five_rounds(
        orch, seeded, tmp_path):
    del orch
    ts = now()
    seeded.write("INSERT INTO agents(id, amos_agent_id, name, executor_type, "
                 "created_at, updated_at) VALUES('sabot','sabot','System Analyst',"
                 "'fake',?,?)", (ts, ts))
    seeded.write_many([
        ("INSERT INTO project_agent_roles(project_id, agent_id, role, preference) "
         "VALUES('proj1','fakebot','pm',10)", ()),
        ("INSERT INTO project_agent_roles(project_id, agent_id, role, preference) "
         "VALUES('proj1','sabot','system-analyst',10)", ()),
    ])
    session = chat.create_session(seeded, scope_type="project", scope_id="proj1",
                                  responder_kind="agent", responder_id="fakebot")
    chat.add_message(seeded, session, role="user", content="Build a booking system")
    round_id = planning_rounds.start(seeded, "proj1", session)
    SCRIPT.extend([
        RunResult(status="succeeded", summary=(
            '{"solution":"v1 without rollback","response":"initial"}')),
        RunResult(status="succeeded", summary=(
            '{"verdict":"challenge","response":"missing rollback",'
            '"issues":["define rollback"]}')),
        RunResult(status="succeeded", summary=(
            '{"solution":"v2 with rollback and evidence","response":"added rollback"}')),
        RunResult(status="succeeded", summary=(
            '{"verdict":"accept","response":"contracts are testable","issues":[]}')),
    ])

    result = await planning_rounds.negotiate(seeded, tmp_path, round_id, actor="u")
    assert result["state"] == "proposed" and len(result["negotiation"]) == 2
    assert planning_rounds.current(seeded, "proj1")["state"] == "proposed"
    visible = chat.messages(seeded, session)
    planning_messages = [item for item in visible if item["meta"].get("planning_role")]
    assert [item["meta"]["planning_role"] for item in planning_messages] == [
        "pm", "system-analyst", "pm", "system-analyst"]
    assert len(seeded.query("SELECT * FROM audit_log WHERE "
                            "action='planning.negotiation.exchange'")) == 2
