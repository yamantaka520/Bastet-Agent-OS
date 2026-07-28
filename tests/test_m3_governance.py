"""M3: context engine, container args, queue policy, multi-project isolation."""


import pytest
from test_workflow import SCRIPT, add_template, req  # fake executor fixtures

from bastet_agent_os.container import ContainerSpec, rewrite_gateway_url, wrap_command
from bastet_agent_os.context_engine import build_context
from bastet_agent_os.db import now
from bastet_agent_os.executors.base import RunResult
from bastet_agent_os.governance import QuotaError, dispatch_check, resolve_grant

# ---- context engine ----------------------------------------------------------


def test_context_engine_budget_and_report(seeded):
    seeded.write("UPDATE jobs SET spec_md=? WHERE id='job1'", ("x" * 40_000,))
    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")
    text, report = build_context(seeded, job, "work", budget_tokens=1000)
    assert len(text) <= 1000 * 4 + 200  # clipped to budget
    spec_section = next(s for s in report.sections if s["bucket"] == "spec")
    assert spec_section["included"] and spec_section["note"] == "clipped"


def test_context_engine_skip_and_history(seeded):
    seeded.write("INSERT INTO runs(id, job_id, stage, agent_id, executor_type, status, "
                 "finished_at) VALUES('r-hist','job1','plan','ag1','fake','succeeded',?)",
                 (now(),))
    seeded.write("INSERT INTO gate_results(id, run_id, gate_type, verdict, reviewer_kind, "
                 "reviewer_id, detail_md, at) VALUES('g-hist','r-hist','human-approve',"
                 "'passed','user','manfred','ship it',?)", (now(),))
    job = seeded.one("SELECT * FROM jobs WHERE id='job1'")
    text, report = build_context(seeded, job, "work", skip=frozenset({"spec"}))
    assert "Pipeline history" in text and "ship it" in text
    spec_section = next(s for s in report.sections if s["bucket"] == "spec")
    assert not spec_section["included"] and spec_section["note"] == "skipped by caller"


# ---- container isolation -----------------------------------------------------


def test_container_wrap_mount_and_hardening():
    args = wrap_command(["claude", "-p", "hi"], ContainerSpec(
        workdir="/tmp/wt", git_common_dir="/repo/.git",
        env={"ANTHROPIC_BASE_URL": "http://host.docker.internal:8890"}))
    joined = " ".join(args)
    assert "--user 1000:1000" in joined and "--rm" in joined
    assert "-v /tmp/wt:/work" in joined
    assert "-v /repo/.git:/repo/.git:ro" in joined      # main .git never writable
    assert "--add-host host.docker.internal:host-gateway" in joined
    assert "--security-opt no-new-privileges" in joined
    assert args[-3:] == ["claude", "-p", "hi"]
    assert "docker.sock" not in joined                   # no docker socket mount


def test_gateway_url_rewrite():
    assert rewrite_gateway_url("http://127.0.0.1:8890") == "http://host.docker.internal:8890"


# ---- multi-project quota isolation (M3 acceptance) -----------------------------


@pytest.fixture
def two_projects(seeded):
    ts = now()
    seeded.write_many([
        ("INSERT INTO projects(id, team_id, repo_path, created_at, updated_at) "
         "VALUES('proj2','team2','/tmp/repo2',?,?)", (ts, ts)),
        ("INSERT INTO grants(id, resource_id, scope_type, scope_id, budget_usd, "
         "max_concurrency, created_at) VALUES('grt2','res1','project','proj2',5.0,1,?)",
         (ts,)),
        ("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, stage, status, "
         "created_at, updated_at) VALUES('job2','proj2','[]','t2','work','in_progress',?,?)",
         (ts, ts)),
        ("INSERT INTO runs(id, job_id, stage, agent_id, executor_type, resource_id, status) "
         "VALUES('run2','job2','work','ag1','claude-code','res1','succeeded')", ()),
    ])
    return seeded


def test_projects_have_independent_budgets(two_projects):
    db = two_projects
    # burn project 1's entire budget (grant grt1: 10 USD)
    db.write("INSERT INTO usage_ledger(id, run_id, resource_id, cost_usd, at) "
             "VALUES('burn','run1','res1',10.0,datetime('now'))")
    grant1 = resolve_grant(db, "res1", "proj1", "ag1")
    grant2 = resolve_grant(db, "res1", "proj2", "ag1")
    assert grant1.id == "grt1" and grant2.id == "grt2"
    with pytest.raises(QuotaError):
        dispatch_check(db, grant1)          # proj1 is exhausted...
    dispatch_check(db, grant2)              # ...proj2 is untouched


# ---- queue policy (on_exceed=queue waits instead of failing) --------------------


async def test_queue_policy_waits_for_budget(orch, seeded):
    seeded.write("UPDATE grants SET budget_usd=0.01, on_exceed='queue' WHERE id='grt1'")
    seeded.write("INSERT INTO usage_ledger(id, run_id, resource_id, cost_usd, at) "
                 "VALUES('full','run1','res1',0.02,datetime('now'))")  # budget exhausted
    orch.queue_poll_s = 0.05
    add_template(seeded, "q", [{"name": "work", "gate": "auto"}])
    SCRIPT.append(RunResult(status="succeeded"))
    job_id = orch.dispatch(req(template_id="q", resource_id="res1", timeout_s=5))

    # free the budget while the run is queued
    import asyncio
    await asyncio.sleep(0.1)
    seeded.write("DELETE FROM usage_ledger WHERE id='full'")
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"


async def test_block_policy_still_blocks(orch, seeded):
    seeded.write("UPDATE grants SET budget_usd=0.01, on_exceed='block' WHERE id='grt1'")
    seeded.write("INSERT INTO usage_ledger(id, run_id, resource_id, cost_usd, at) "
                 "VALUES('full','run1','res1',0.02,datetime('now'))")
    with pytest.raises(QuotaError):
        orch.dispatch(req(resource_id="res1"))
