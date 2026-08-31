"""A depleted agent leaves the rotation.

The live loop this closes: Grok1 answered `402 Payment Required: Grok Build
usage balance exhausted` in 30ms. None of the quota markers matched, so the card
merely "failed". The PM diagnosed it correctly and handed the stage to Agy — Agy
ran fine, the gate failed on real test failures, the rework cycle cleared the
one-shot override, role mapping dispatched Grok1 again, instant 402, blocked.
Twice, until the PM's whole intervention budget was gone and the card died
holding a problem no agent could have solved by trying harder.

Routing is the fix: an agent whose balance a vendor calls exhausted must stop
receiving work until a human tops it up.
"""

import json

import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os.db import now
from bastet_agent_os.executors.base import RunResult
from bastet_agent_os.quota_wait import is_credit_exhausted, is_quota_failure, parse_reset

GROK_402 = ('Internal error: {\n  "message": "API error (status 402 Payment '
            'Required): Grok Build usage balance exhausted",\n  "http_status": 402\n}')


# ---- telling the two conditions apart ---------------------------------------------

def test_a_depleted_balance_is_not_a_timer():
    """A session limit lifts on the vendor's clock; a balance lifts when
    somebody pays. Parking the second one waits forever."""
    assert is_credit_exhausted(GROK_402)
    assert parse_reset(GROK_402) is None, "a balance has no reset time to wait for"

    timed = "You've hit your session limit · resets 1:30am (Asia/Taipei)"
    assert is_quota_failure(timed)
    assert parse_reset(timed) is not None
    assert not is_credit_exhausted(timed), "a timed limit must not disable the agent"


def test_ordinary_failures_are_not_credit_exhaustion():
    for text in ("exit 1: assertion failed", "max turns reached",
                 "ConnectTimeout", "", "the account balance sheet parser broke"):
        assert not is_credit_exhausted(text), text


# ---- routing ----------------------------------------------------------------------

def _two_testers(db):
    """Grok1 (preferred) and Agy, both assigned the tester role."""
    for aid in ("grok1", "agy1"):
        db.write("INSERT INTO agents(id,amos_agent_id,name,executor_type,enabled,"
                 "config_json,created_at,updated_at) VALUES(?,?,?,'fake',1,'{}',?,?)",
                 (aid, aid, aid, now(), now()))
    db.write("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
             "VALUES('proj1','grok1','tester',100)")
    db.write("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
             "VALUES('proj1','agy1','tester',50)")


def test_dispatch_skips_a_depleted_agent(orch, seeded):
    from bastet_agent_os.workflow import parse_stages
    _two_testers(seeded)
    seeded.write("UPDATE jobs SET stage='test' WHERE id='job1'")
    stage = parse_stages([{"name": "test", "role": "tester", "gate": "auto"}])[0]
    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")

    assert orch._agent_for_stage(job, stage)["id"] == "grok1"   # preference wins

    seeded.write("UPDATE agents SET depleted_at=? WHERE id='grok1'", (now(),))
    assert orch._agent_for_stage(job, stage)["id"] == "agy1", \
        "work was routed to an agent the vendor says has no balance"


def test_even_an_explicit_override_cannot_dispatch_to_a_depleted_agent(orch, seeded):
    from bastet_agent_os.workflow import parse_stages
    _two_testers(seeded)
    seeded.write("UPDATE agents SET depleted_at=? WHERE id='grok1'", (now(),))
    seeded.write("UPDATE jobs SET stage='test', agent_override='grok1' WHERE id='job1'")
    stage = parse_stages([{"name": "test", "role": "tester", "gate": "auto"}])[0]
    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")
    assert orch._agent_for_stage(job, stage)["id"] == "agy1"


def test_alternate_agent_never_offers_a_depleted_stand_in(orch, seeded):
    _two_testers(seeded)
    seeded.write("UPDATE jobs SET stage='test', stages_snapshot_json=? WHERE id='job1'",
                 (json.dumps([{"name": "test", "role": "tester", "gate": "auto"}]),))
    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")
    assert orch._alternate_agent(job, "grok1") == "agy1"

    seeded.write("UPDATE agents SET depleted_at=? WHERE id='agy1'", (now(),))
    assert orch._alternate_agent(job, "grok1") == "", \
        "offered a depleted agent as the stand-in"


# ---- the loop, end to end ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_402_takes_the_agent_out_and_the_card_moves_on(orch, seeded):
    """One dispatch to a dead agent, then never again — this is the loop that
    burned two PM interventions and killed a card."""
    _two_testers(seeded)
    add_template(seeded, "t", [{"name": "test", "role": "tester", "gate": "auto"}])
    SCRIPT.append(RunResult(status="failed", summary=GROK_402))     # grok1's 402
    SCRIPT.append(RunResult(status="succeeded", summary="tests green"))  # agy1 later

    job_id = orch.dispatch(req(agent_id="grok1", template_id="t"))
    await orch.wait_idle()

    row = seeded.one("SELECT depleted_at, depleted_reason FROM agents WHERE id='grok1'")
    assert row["depleted_at"], "the vendor said 402 and the agent stayed in rotation"
    assert "balance exhausted" in row["depleted_reason"]
    assert seeded.one("SELECT id FROM audit_log WHERE action='agent.depleted'")

    # the stall is now recoverable BY ROUTING — no PM judgement needed
    blocked = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    recoverable, reason = orch._recoverable_block(blocked)
    assert recoverable and reason == "agent balance exhausted"

    await orch.supervise_once()
    await orch.wait_idle()
    ran = [r["agent_id"] for r in seeded.query(
        "SELECT agent_id FROM runs WHERE job_id=? ORDER BY rowid", (job_id,))]
    assert ran[-1] == "agy1", f"retried into the depleted agent again: {ran}"


@pytest.mark.asyncio
async def test_with_no_stand_in_the_stall_is_not_pretend_recoverable(orch, seeded):
    """Honesty: with nobody to hand the work to, retrying is theatre. The card
    must stop and say a human has to top up."""
    seeded.write("INSERT INTO agents(id,amos_agent_id,name,executor_type,enabled,"
                 "config_json,created_at,updated_at) VALUES('solo','solo','solo',"
                 "'fake',1,'{}',?,?)", (now(), now()))
    seeded.write("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
                 "VALUES('proj1','solo','tester',100)")
    seeded.write("UPDATE agents SET depleted_at=? WHERE id='solo'", (now(),))
    seeded.write("UPDATE jobs SET status='blocked', stage='test', "
                 "stages_snapshot_json=? WHERE id='job1'",
                 (json.dumps([{"name": "test", "role": "tester", "gate": "auto"}]),))
    seeded.write("UPDATE runs SET status='failed', error=? WHERE id='run1'", (GROK_402,))

    recoverable, _ = orch._recoverable_block(
        seeded.one("SELECT * FROM jobs WHERE id='job1'"))
    assert not recoverable


# ---- clearing it: money is a human act -------------------------------------------

@pytest.mark.asyncio
async def test_only_a_human_retry_clears_the_flag(orch, seeded):
    _two_testers(seeded)
    seeded.write("UPDATE agents SET depleted_at=?, depleted_reason='402' WHERE id='grok1'",
                 (now(),))
    seeded.write("UPDATE jobs SET status='blocked', stage='test', "
                 "stages_snapshot_json=? WHERE id='job1'",
                 (json.dumps([{"name": "test", "role": "tester", "gate": "auto"}]),))

    # automation naming the agent must NOT clear it — the balance is still empty
    SCRIPT.append(RunResult(status="succeeded", summary="ok"))
    orch.retry("job1", agent_id="grok1", user="pm-supervisor:agy1")
    await orch.wait_idle()
    assert seeded.one("SELECT depleted_at FROM agents WHERE id='grok1'")["depleted_at"]

    seeded.write("UPDATE jobs SET status='blocked' WHERE id='job1'")
    SCRIPT.append(RunResult(status="succeeded", summary="ok"))
    orch.retry("job1", agent_id="grok1", user="root")
    await orch.wait_idle()
    assert seeded.one("SELECT depleted_at FROM agents WHERE id='grok1'")["depleted_at"] \
        is None, "a human naming the agent is them saying it has funds again"


def test_clear_depleted_is_idempotent_and_audited(orch, seeded):
    _two_testers(seeded)
    assert orch.clear_depleted("grok1") is False        # was not depleted
    seeded.write("UPDATE agents SET depleted_at=? WHERE id='grok1'", (now(),))
    assert orch.clear_depleted("grok1", user="root") is True
    assert orch.clear_depleted("grok1", user="root") is False
    assert seeded.one("SELECT actor FROM audit_log WHERE action='agent.undepleted'"
                      )["actor"] == "user:root"


def test_a_declared_role_never_silently_degrades_to_an_unrelated_agent(orch, seeded):
    """A capable executor is not automatically qualified for every project role.
    Same-role assignments are the ordered backup chain; cross-role takeover must
    be an explicit override so the audit trail says who made that judgement."""
    from bastet_agent_os.workflow import parse_stages
    _two_testers(seeded)
    seeded.write("DELETE FROM project_agent_roles WHERE agent_id='agy1'")
    seeded.write("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
                 "VALUES('proj1','agy1','reviewer',10)")     # a different role
    seeded.write("UPDATE agents SET depleted_at=? WHERE id='grok1'", (now(),))
    seeded.write("UPDATE jobs SET stage='test', default_agent_id='grok1' WHERE id='job1'")
    stage = parse_stages([{"name": "test", "role": "tester", "gate": "auto"}])[0]

    with pytest.raises(ValueError, match="same-role backup"):
        orch._agent_for_stage(seeded.one("SELECT * FROM jobs WHERE id='job1'"), stage)


def test_cross_role_takeover_requires_an_explicit_override(orch, seeded):
    from bastet_agent_os.workflow import parse_stages
    _two_testers(seeded)
    seeded.write("DELETE FROM project_agent_roles WHERE agent_id='agy1'")
    seeded.write("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
                 "VALUES('proj1','agy1','reviewer',10)")
    seeded.write("UPDATE agents SET depleted_at=? WHERE id='grok1'", (now(),))
    seeded.write("UPDATE jobs SET stage='test', default_agent_id='grok1',"
                 "agent_override='agy1' WHERE id='job1'")
    stage = parse_stages([{"name": "test", "role": "tester", "gate": "auto"}])[0]
    picked = orch._agent_for_stage(seeded.one("SELECT * FROM jobs WHERE id='job1'"), stage)
    assert picked["id"] == "agy1"


def test_when_every_agent_is_depleted_the_error_says_what_to_do(orch, seeded):
    from bastet_agent_os.workflow import parse_stages
    _two_testers(seeded)
    seeded.write("UPDATE agents SET depleted_at=?", (now(),))
    seeded.write("UPDATE jobs SET stage='test', default_agent_id='grok1' WHERE id='job1'")
    stage = parse_stages([{"name": "test", "role": "tester", "gate": "auto"}])[0]
    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")

    with pytest.raises(ValueError, match="額度都用盡"):
        orch._agent_for_stage(job, stage)


def test_only_the_failing_runs_own_error_counts_as_a_balance_problem(orch, seeded):
    """The rework note quotes earlier failures. Pooling it with the live error
    made every later failure on that card read as "balance exhausted" — and the
    handover then dispatched the one agent that really had none."""
    _two_testers(seeded)
    seeded.write("UPDATE jobs SET status='blocked', stage='test', "
                 "rework_note=?, stages_snapshot_json=? WHERE id='job1'",
                 (f"上一輪的失敗輸出：{GROK_402}",
                  json.dumps([{"name": "test", "role": "tester", "gate": "auto"}])))
    seeded.write("UPDATE runs SET status='failed', error='exit 1: assertion failed', "
                 "agent_id='agy1' WHERE id='run1'")

    recoverable, reason = orch._recoverable_block(
        seeded.one("SELECT * FROM jobs WHERE id='job1'"))
    assert not recoverable, f"an unrelated failure was blamed on a balance: {reason}"


@pytest.mark.asyncio
async def test_an_exhausted_supervisor_hands_over_instead_of_going_silent(orch, seeded):
    """Recoverable + no attempts left used to fall through to nobody: the sweep
    skipped it and the PM was only offered non-recoverable stalls."""
    from bastet_agent_os import pm_supervisor
    _two_testers(seeded)
    seeded.write("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
                 "VALUES('proj1','agy1','pm',100)")
    seeded.write("UPDATE jobs SET status='blocked', stage='test', "
                 "stages_snapshot_json=? WHERE id='job1'",
                 (json.dumps([{"name": "test", "role": "tester", "gate": "auto"}]),))
    seeded.write("UPDATE runs SET status='failed', error=? WHERE id='run1'", (GROK_402,))
    for _ in range(orch.MAX_SUPERVISOR_RETRIES):        # budget already spent
        seeded.audit("supervisor", "job.supervisor_retry", "job", "job1", {})
    SCRIPT.append(RunResult(status="succeeded",
                            summary='{"action": "escalate", "reason": "需要充值"}'))

    assert orch._recoverable_block(
        seeded.one("SELECT * FROM jobs WHERE id='job1'"))[0] is True
    await orch.supervise_once()
    await orch.wait_idle()

    assert pm_supervisor.intervention_count(seeded, "job1") == 1, \
        "the card fell through to nobody"
