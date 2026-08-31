"""Workflow engine: stage pipelines, gate protocol, retries, human approval."""

import json
import pathlib
import subprocess

import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os.executors.base import RunResult
from bastet_agent_os.workflow import (
    evaluate_gate,
    is_linear_stage_graph,
    parse_stages,
    ready_stages,
    refresh_ready_nodes,
    seed_stage_nodes,
)

# ---- stage/template validation ---------------------------------------------------

def test_parse_stages_validates():
    with pytest.raises(ValueError):
        parse_stages([])
    with pytest.raises(ValueError):
        parse_stages([{"name": "a", "gate": "nonsense"}])
    with pytest.raises(ValueError):
        parse_stages([{"name": "a"}, {"name": "a"}])  # duplicate
    with pytest.raises(ValueError):
        parse_stages([{"name": "a", "gate": "tests-pass"}])  # no command


def test_legacy_stages_become_a_linear_graph():
    stages = parse_stages([{"name": "plan"}, {"name": "build"}, {"name": "verify"}])
    assert [stage.needs for stage in stages] == [[], ["plan"], ["build"]]
    assert [stage.name for stage in ready_stages(stages, set())] == ["plan"]


def test_graph_stages_expose_parallel_roots_and_join_contract():
    stages = parse_stages([
        {"name": "architecture", "needs": [], "read_only": True,
         "produces": ["system-contract"]},
        {"name": "ux", "needs": ["architecture"], "workspace": "isolated",
         "consumes": ["system-contract"], "produces": ["ux-spec"]},
        {"name": "core", "needs": ["architecture"], "workspace": "isolated",
         "consumes": ["system-contract"], "produces": ["core-api"]},
        {"name": "integration", "needs": ["ux", "core"],
         "consumes": ["ux-spec", "core-api"]},
    ])
    assert [s.name for s in ready_stages(stages, {"architecture"})] == ["ux", "core"]
    assert [s.name for s in ready_stages(stages, {"architecture", "ux"})] == ["core"]
    assert [s.name for s in ready_stages(stages, {"architecture", "ux", "core"})] == [
        "integration"]
    assert is_linear_stage_graph(stages) is False


def test_explicit_linear_graph_remains_compatible_with_the_v1_driver():
    stages = parse_stages([
        {"name": "plan", "needs": []},
        {"name": "build", "needs": ["plan"]},
        {"name": "verify", "needs": ["build"]},
    ])
    assert is_linear_stage_graph(stages) is True


async def test_branching_stage_graph_runs_ready_nodes_and_reaches_join(orch, seeded):
    seeded.write_many([
        ("INSERT INTO agents(id,amos_agent_id,name,executor_type,created_at,updated_at) "
         "VALUES('ui-agent','ui-agent','UI','fake',datetime('now'),datetime('now'))", ()),
        ("INSERT INTO agents(id,amos_agent_id,name,executor_type,created_at,updated_at) "
         "VALUES('core-agent','core-agent','Core','fake',datetime('now'),datetime('now'))", ()),
        ("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
         "VALUES('proj1','ui-agent','ui-designer',10)", ()),
        ("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
         "VALUES('proj1','core-agent','backend',10)", ()),
    ])
    add_template(seeded, "branching", [
        {"name": "plan", "needs": [], "read_only": True},
        {"name": "ui", "role": "ui-designer", "needs": ["plan"],
         "workspace": "isolated", "challenge": False},
        {"name": "core", "role": "backend", "needs": ["plan"],
         "workspace": "isolated", "challenge": False},
        {"name": "join", "needs": ["ui", "core"], "challenge": False},
    ])
    def write_output(name):
        def run(spec):
            (pathlib.Path(spec.workdir) / f"{name}.txt").write_text(f"{name}\n")
            return RunResult(status="succeeded", summary=name)
        return run

    def integrate(spec):
        root = pathlib.Path(spec.workdir)
        assert (root / "ui.txt").read_text() == "ui\n"
        assert (root / "core.txt").read_text() == "core\n"
        (root / "integrated.txt").write_text("joined\n")
        return RunResult(status="succeeded", summary="join")

    SCRIPT.extend([RunResult(status="succeeded", summary="plan"),
                   write_output("ui"), write_output("core"), integrate])
    job_id = orch.dispatch(req(template_id="branching"))
    await orch.wait_idle()
    nodes = {row["stage"]: row["status"] for row in seeded.query(
        "SELECT stage,status FROM job_stage_nodes WHERE job_id=?", (job_id,))}
    audit = [dict(row) for row in seeded.query(
        "SELECT action,detail_json FROM audit_log WHERE target_id=? ORDER BY id", (job_id,))]
    assert nodes == {"plan": "passed", "ui": "passed",
                     "core": "passed", "join": "passed"}, audit
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    branch_runs = seeded.query(
        "SELECT stage,workdir FROM runs WHERE job_id=? AND stage IN ('ui','core')",
        (job_id,))
    assert len(branch_runs) == 2
    assert len({row["workdir"] for row in branch_runs}) == 2
    assert {row["stage"] for row in branch_runs} == {"ui", "core"}
    project = seeded.one("SELECT repo_path FROM projects WHERE id='proj1'")
    integrated = subprocess.run(
        ["git", "-C", project["repo_path"], "show",
         f"bastet/{job_id}:integrated.txt"], capture_output=True, text=True, check=True)
    assert integrated.stdout == "joined\n"


def test_branching_graph_requires_one_shared_terminal_join(orch, seeded):
    add_template(seeded, "bad-sink", [
        {"name": "left", "needs": [], "workspace": "isolated"},
        {"name": "right", "needs": [], "workspace": "isolated"},
    ])
    with pytest.raises(ValueError, match="terminal join"):
        orch.dispatch(req(template_id="bad-sink"))


async def test_graph_retry_preserves_passed_sibling_and_resumes_failed_branch(orch, seeded):
    add_template(seeded, "retry-graph", [
        {"name": "plan", "needs": [], "read_only": True},
        {"name": "ui", "needs": ["plan"], "workspace": "isolated",
         "challenge": False},
        {"name": "core", "needs": ["plan"], "workspace": "isolated",
         "challenge": False},
        {"name": "join", "needs": ["ui", "core"], "challenge": False},
    ])
    SCRIPT.extend([
        RunResult(status="succeeded", summary="plan"),
        RunResult(status="failed", summary="ui failed"),
        RunResult(status="succeeded", summary="core passed"),
    ])
    job_id = orch.dispatch(req(template_id="retry-graph"))
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "blocked"
    assert seeded.one("SELECT status FROM job_stage_nodes WHERE job_id=? AND stage='core'",
                      (job_id,))["status"] == "passed"

    SCRIPT.extend([RunResult(status="succeeded", summary="ui repaired"),
                   RunResult(status="succeeded", summary="joined")])
    orch.retry(job_id)
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    core_runs = seeded.one("SELECT COUNT(*) n FROM runs WHERE job_id=? AND stage='core'",
                           (job_id,))["n"]
    assert core_runs == 1


async def test_graph_gate_reworks_its_writable_branch_without_replaying_sibling(
        orch, seeded):
    add_template(seeded, "gate-rework-graph", [
        {"name": "plan", "needs": [], "read_only": True},
        {"name": "writer", "needs": ["plan"], "workspace": "isolated",
         "challenge": False},
        {"name": "sibling", "needs": ["plan"], "workspace": "isolated",
         "challenge": False},
        {"name": "review", "needs": ["writer"], "workspace": "isolated",
         "read_only": True, "gate": "agent-review", "challenge": False,
         "rework_target": "writer", "max_cycles": 2},
        {"name": "join", "needs": ["review", "sibling"], "challenge": False},
    ])
    SCRIPT.extend([
        RunResult(status="succeeded", summary="plan"),
        RunResult(status="succeeded", summary="first draft"),
        RunResult(status="succeeded", summary="sibling done"),
        RunResult(status="succeeded", summary="missing case",
                  structured_verdict={"verdict": "reject", "reasons": ["missing"]}),
        RunResult(status="succeeded", summary="repaired draft"),
        RunResult(status="succeeded", summary="accepted",
                  structured_verdict={"verdict": "approve"}),
        RunResult(status="succeeded", summary="joined"),
    ])
    job_id = orch.dispatch(req(template_id="gate-rework-graph"))
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    counts = {row["stage"]: row["n"] for row in seeded.query(
        "SELECT stage,COUNT(*) n FROM runs WHERE job_id=? GROUP BY stage", (job_id,))}
    assert counts["writer"] == counts["review"] == 2
    assert counts["sibling"] == 1
    handback = seeded.one(
        "SELECT detail_json FROM audit_log WHERE target_id=? AND action='job.rework'",
        (job_id,))
    assert json.loads(handback["detail_json"])["back_to"] == "writer"


async def test_parallel_human_gates_are_approved_by_explicit_stage(orch, seeded):
    add_template(seeded, "approval-graph", [
        {"name": "plan", "needs": [], "read_only": True},
        {"name": "ux-signoff", "needs": ["plan"], "workspace": "isolated",
         "gate": "human-approve", "challenge": False},
        {"name": "api-signoff", "needs": ["plan"], "workspace": "isolated",
         "gate": "human-approve", "challenge": False},
        {"name": "join", "needs": ["ux-signoff", "api-signoff"],
         "challenge": False},
    ])
    SCRIPT.extend([
        RunResult(status="succeeded", summary="plan"),
        RunResult(status="succeeded", summary="ux ready"),
        RunResult(status="succeeded", summary="api ready"),
    ])
    job_id = orch.dispatch(req(template_id="approval-graph"))
    await orch.wait_idle()
    nodes = {row["stage"]: row["status"] for row in seeded.query(
        "SELECT stage,status FROM job_stage_nodes WHERE job_id=?", (job_id,))}
    assert nodes["ux-signoff"] == nodes["api-signoff"] == "blocked"
    with pytest.raises(ValueError, match="specify stage"):
        orch.approve(job_id, True, "ambiguous")

    first = orch.approve(job_id, True, "UX accepted", stage_name="ux-signoff")
    assert first == {"job_id": job_id, "status": "blocked", "stage": "api-signoff"}
    assert seeded.one(
        "SELECT status FROM job_stage_nodes WHERE job_id=? AND stage='ux-signoff'",
        (job_id,))["status"] == "passed"

    SCRIPT.append(RunResult(status="succeeded", summary="joined"))
    second = orch.approve(job_id, True, "API accepted", stage_name="api-signoff")
    assert second["status"] == "in_progress"
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    assert seeded.one(
        "SELECT status FROM job_stage_nodes WHERE job_id=? AND stage='join'",
        (job_id,))["status"] == "passed"
    handoffs = seeded.query(
        "SELECT from_stage,to_stage FROM stage_handoffs WHERE job_id=? "
        "AND from_stage IN ('ux-signoff','api-signoff') ORDER BY from_stage", (job_id,))
    assert [(row["from_stage"], row["to_stage"]) for row in handoffs] == [
        ("api-signoff", "join"), ("ux-signoff", "join")]


async def test_graph_receiver_reviews_handoff_before_stage_execution(orch, seeded):
    seeded.write_many([
        ("INSERT INTO agents(id,amos_agent_id,name,executor_type,created_at,updated_at) "
         "VALUES('review-agent','review-agent','Reviewer','fake',datetime('now'),"
         "datetime('now'))", ()),
        ("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
         "VALUES('proj1','review-agent','reviewer',10)", ()),
    ])
    add_template(seeded, "review-graph", [
        {"name": "plan", "needs": [], "read_only": True},
        {"name": "reviewed", "role": "reviewer", "needs": ["plan"],
         "workspace": "isolated", "challenge": True},
        {"name": "sibling", "needs": ["plan"], "workspace": "isolated",
         "challenge": False},
        {"name": "join", "needs": ["reviewed", "sibling"], "challenge": False},
    ])
    SCRIPT.extend([
        RunResult(status="succeeded", summary="plan evidence"),
        RunResult(status="succeeded", summary=json.dumps({
            "verdict": "accept", "response": "evidence is sufficient"})),
        RunResult(status="succeeded", summary="reviewed stage ran"),
        RunResult(status="succeeded", summary="sibling ran"),
        RunResult(status="succeeded", summary="joined"),
    ])
    job_id = orch.dispatch(req(template_id="review-graph"))
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    receipt = seeded.one(
        "SELECT hr.acknowledgement FROM handoff_receipts hr "
        "JOIN stage_handoffs h ON h.id=hr.handoff_id WHERE h.job_id=? "
        "AND h.to_stage='reviewed'", (job_id,))
    assert receipt and "sufficient" in receipt["acknowledgement"]
    review_audit = seeded.one(
        "SELECT detail_json FROM audit_log WHERE target_id=? "
        "AND action='stage.handoff_review'", (job_id,))
    assert json.loads(review_audit["detail_json"])["status"] == "accepted"


@pytest.mark.parametrize("raw,match", [
    ([{"name": "a", "needs": ["missing"]}], "unknown dependencies"),
    ([{"name": "a", "needs": ["b"]}, {"name": "b", "needs": ["a"]}], "cycle"),
    ([{"name": "a", "needs": [], "produces": ["x"]},
      {"name": "b", "needs": [], "consumes": ["x"]}], "not produced by a dependency"),
    ([{"name": "a", "needs": []}, {"name": "b", "needs": []}],
     "parallel stages"),
])
def test_graph_contract_rejects_unsafe_or_unsatisfied_dags(raw, match):
    with pytest.raises(ValueError, match=match):
        parse_stages(raw)


def test_stage_node_state_is_durable_and_promotes_join_only_after_all_needs(seeded):
    stages = parse_stages([
        {"name": "plan", "needs": [], "read_only": True},
        {"name": "ui", "needs": ["plan"], "workspace": "isolated"},
        {"name": "core", "needs": ["plan"], "workspace": "isolated"},
        {"name": "join", "needs": ["ui", "core"]},
    ])
    nodes = seed_stage_nodes(seeded, "job1", stages)
    assert [(node["stage"], node["status"]) for node in nodes] == [
        ("plan", "ready"), ("ui", "pending"), ("core", "pending"), ("join", "pending")]
    seeded.write("UPDATE job_stage_nodes SET status='passed' WHERE job_id='job1' "
                 "AND stage='plan'")
    assert refresh_ready_nodes(seeded, "job1", stages) == ["ui", "core"]
    seeded.write("UPDATE job_stage_nodes SET status='passed' WHERE job_id='job1' "
                 "AND stage='ui'")
    assert refresh_ready_nodes(seeded, "job1", stages) == []
    seeded.write("UPDATE job_stage_nodes SET status='passed' WHERE job_id='job1' "
                 "AND stage='core'")
    assert refresh_ready_nodes(seeded, "job1", stages) == ["join"]


def test_tests_pass_gate_runs_command(tmp_path):
    stage = parse_stages([{"name": "t", "gate": "tests-pass",
                           "gate_config": {"command": "exit 0"}}])[0]
    assert evaluate_gate(stage, str(tmp_path), None).verdict == "passed"
    stage.gate_config["command"] = "exit 3"
    assert evaluate_gate(stage, str(tmp_path), None).verdict == "failed"


def test_agent_review_gate_requires_structured_verdict(tmp_path):
    stage = parse_stages([{"name": "r", "gate": "agent-review"}])[0]
    assert evaluate_gate(stage, str(tmp_path), None).verdict == "failed"  # missing => reject
    assert evaluate_gate(stage, str(tmp_path), {"verdict": "approve"}).verdict == "passed"
    out = evaluate_gate(stage, str(tmp_path), {"verdict": "reject", "reasons": ["bad"]})
    assert out.verdict == "failed" and "bad" in out.detail


# ---- full pipeline behavior -------------------------------------------------------

async def test_single_stage_auto_gate_completes_job(orch, seeded):
    SCRIPT.append(RunResult(status="succeeded", summary="ok"))
    job_id = orch.dispatch(req())
    await orch.wait_idle()
    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "done"


async def test_multi_stage_advances_through_review(orch, seeded):
    add_template(seeded, "dev", [
        {"name": "work", "gate": "auto"},
        {"name": "review", "gate": "agent-review", "read_only": True},
    ])
    SCRIPT.append(RunResult(status="succeeded", summary="implemented"))
    SCRIPT.append(RunResult(status="succeeded", summary="looks good",
                            structured_verdict={"verdict": "approve"}))
    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()
    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "done"
    gates = seeded.query("SELECT verdict FROM gate_results g JOIN runs r ON r.id=g.run_id "
                         "WHERE r.job_id=? ORDER BY g.at", (job_id,))
    assert [g["verdict"] for g in gates] == ["passed", "passed"]


async def test_review_without_verdict_does_not_pass_the_gate(orch, seeded):
    """Prose never approves. What happens next is the rework loop's business
    (tests/test_rework.py); what matters here is that the gate said no."""
    add_template(seeded, "dev", [
        {"name": "work", "gate": "auto", "on_fail": "block"},
        {"name": "review", "gate": "agent-review", "on_fail": "block"},
    ])
    SCRIPT.append(RunResult(status="succeeded"))
    SCRIPT.append(RunResult(status="succeeded", summary="APPROVED!!"))  # prose only — no verdict
    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()
    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "blocked"  # free text never passes an agent-review gate
    verdicts = seeded.query("SELECT verdict FROM gate_results g JOIN runs r ON r.id=g.run_id "
                            "WHERE r.job_id=? ORDER BY g.at", (job_id,))
    assert verdicts[-1]["verdict"] == "failed"


async def test_execution_failure_retries_then_blocks(orch, seeded):
    add_template(seeded, "retry", [
        {"name": "work", "gate": "auto", "max_retries": 1},
    ])
    SCRIPT.append(RunResult(status="failed", summary="boom"))
    SCRIPT.append(RunResult(status="succeeded"))
    job_id = orch.dispatch(req(template_id="retry"))
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    attempts = seeded.query("SELECT attempt, status FROM runs WHERE job_id=? ORDER BY attempt",
                            (job_id,))
    assert [(a["attempt"], a["status"]) for a in attempts] == [(1, "failed"), (2, "succeeded")]


async def test_human_approve_pauses_then_resumes(orch, seeded):
    add_template(seeded, "gated", [
        {"name": "plan", "gate": "human-approve"},
        {"name": "work", "gate": "auto"},
    ])
    SCRIPT.append(RunResult(status="succeeded", summary="the plan"))
    job_id = orch.dispatch(req(template_id="gated"))
    await orch.wait_idle()
    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert (job["status"], job["stage"]) == ("blocked", "plan")  # waiting for a human

    SCRIPT.append(RunResult(status="succeeded", summary="did the work"))
    orch.approve(job_id, approved=True, comment="lgtm")
    await orch.wait_idle()
    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert (job["status"], job["stage"]) == ("done", "work")


async def test_human_reject_keeps_job_blocked(orch, seeded):
    add_template(seeded, "gated", [{"name": "plan", "gate": "human-approve"}])
    SCRIPT.append(RunResult(status="succeeded"))
    job_id = orch.dispatch(req(template_id="gated"))
    await orch.wait_idle()
    orch.approve(job_id, approved=False, comment="not like this")
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "blocked"
    with pytest.raises(ValueError):
        orch.approve("job_nonexistent", approved=True, comment="")


async def test_role_routing_picks_role_agent(orch, seeded):
    seeded.write("INSERT INTO agents(id, amos_agent_id, name, executor_type, created_at, "
                 "updated_at) VALUES('reviewer-bot','rb','R','fake',datetime('now'),"
                 "datetime('now'))")
    seeded.write("INSERT INTO project_agent_roles(project_id, agent_id, role, preference) "
                 "VALUES('proj1','reviewer-bot','reviewer',10)")
    add_template(seeded, "dev", [
        {"name": "work", "gate": "auto"},
        {"name": "review", "role": "reviewer", "gate": "agent-review"},
    ])
    SCRIPT.append(RunResult(status="succeeded"))
    SCRIPT.append(RunResult(status="succeeded", structured_verdict={"verdict": "approve"}))
    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()
    review_run = seeded.one("SELECT agent_id FROM runs WHERE job_id=? AND stage='review'",
                            (job_id,))
    assert review_run["agent_id"] == "reviewer-bot"  # role match beats job default


# ---- built-in presets (workflow_presets) ------------------------------------------

def test_all_presets_parse_and_are_well_formed():
    from bastet_agent_os.workflow_presets import EVIDENCE_TYPES, GATES, PRESETS, ROLES

    role_ids = {r["id"] for r in ROLES}
    gate_ids = {g["id"] for g in GATES}
    evidence_ids = {item["id"] for item in EVIDENCE_TYPES}
    assert len(PRESETS) >= 6
    for preset in PRESETS:
        stages = parse_stages(preset["stages"])   # engine must accept every preset
        assert stages, preset["id"]
        assert not is_linear_stage_graph(stages), f"{preset['id']}: still serial"
        covered = {kind for stage in stages for kind in stage.evidence}
        assert set(preset["required_evidence"]) <= covered, preset["id"]
        assert covered <= evidence_ids, preset["id"]
        for raw, stage in zip(preset["stages"], stages, strict=True):
            assert stage.gate in gate_ids
            if raw.get("role"):
                assert raw["role"] in role_ids, f"{preset['id']}: unknown role"
            if stage.gate == "tests-pass":
                assert stage.gate_config.get("command")
            assert not (stage.gate == "auto" and stage.evidence), (
                f"{preset['id']}:{stage.name} claims evidence behind an auto gate")
        # side-effecting last stage should ask a human
        assert stages[-1].gate in ("human-approve", "agent-review"), preset["id"]
        if preset["family"] == "development":
            assert set(stages[-1].delivery_modes) == {"integration", "production"}

    development_roles = {"system-analyst", "ux-researcher", "ui-designer",
                         "visual-artist",
                         "security-reviewer", "integrator", "release-manager"}
    fullstack = next(item for item in PRESETS if item["id"] == "fullstack-dev")
    assert development_roles <= {stage.get("role") for stage in fullstack["stages"]}


def test_delivery_policy_is_valid_and_only_allowed_on_graph_sink():
    stages = parse_stages([
        {"name": "build", "needs": []},
        {"name": "release", "needs": ["build"],
         "delivery_modes": ["integration", "production"]},
    ])
    assert stages[-1].delivery_modes == ["integration", "production"]
    with pytest.raises(ValueError, match="only be declared on a sink"):
        parse_stages([
            {"name": "build", "needs": [], "delivery_modes": ["integration"]},
            {"name": "release", "needs": ["build"]},
        ])
    with pytest.raises(ValueError, match="delivery_modes"):
        parse_stages([{"name": "release", "needs": [],
                       "delivery_modes": ["none"]}])


def test_parallel_read_only_reviewers_can_share_a_workspace():
    stages = parse_stages([
        {"name": "build", "needs": [], "workspace": "isolated"},
        {"name": "quality", "needs": ["build"], "read_only": True,
         "gate": "agent-review", "evidence": ["architecture"]},
        {"name": "security", "needs": ["build"], "read_only": True,
         "gate": "agent-review", "evidence": ["security"]},
        {"name": "release", "needs": ["quality", "security"],
         "gate": "human-approve"},
    ])
    assert stages[1].evidence == ["architecture"]
    assert stages[2].workspace == "shared"


# ---- role prompts & project secrets in runs -----------------------------------------

async def test_role_prompt_and_project_secret_reach_the_run(orch, seeded, monkeypatch,
                                                            tmp_path):
    from bastet_agent_os.db import now as _now

    seeded.write("INSERT INTO role_prompts(role, label, prompt, builtin, updated_at) "
                 "VALUES('reviewer','審查者','你是審查者：只找真正的問題。',1,?)", (_now(),))
    seeded.write("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
                 "VALUES('proj1','fakebot','reviewer',0)")
    secret_file = tmp_path / "deploy-token"
    secret_file.write_text("s3cr3t-value")
    seeded.write("INSERT INTO resources(id, kind, name, secret_ref, config_json, "
                 "created_at, updated_at) VALUES('sec1','secret','deploy',?,?,?,?)",
                 (f"file:{secret_file}", '{"env_name": "DEPLOY_TOKEN"}', _now(), _now()))
    seeded.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, created_at) "
                 "VALUES('grt-sec','sec1','project','proj1',?)", (_now(),))
    add_template(seeded, "reviewed", [{"name": "review", "role": "reviewer",
                                       "gate": "auto"}])

    captured = {}

    def capture(task):
        captured["prompt"] = task.prompt
        captured["env"] = dict(task.extra_env)
        return RunResult(status="succeeded")

    SCRIPT.append(capture)
    job_id = orch.dispatch(req(template_id="reviewed"))
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    assert "你是審查者" in captured["prompt"]          # role definition briefs the agent
    assert captured["env"]["DEPLOY_TOKEN"] == "s3cr3t-value"  # scoped secret injected
    resolved = seeded.query("SELECT * FROM audit_log WHERE action='secret.resolve'")
    assert resolved, "secret resolution must be audited"


async def test_pool_resources_reach_the_run(orch, seeded, tmp_path):
    """A granted resource must arrive as env vars + an MCP config + a prompt
    note — otherwise the agent has no way to call it."""
    from bastet_agent_os.db import now as _now

    key = tmp_path / "api-key"
    key.write_text("sk-pool")
    seeded.write("INSERT INTO resources(id, kind, name, endpoint, secret_ref, "
                 "config_json, created_at, updated_at) "
                 "VALUES('res-api','api','Weather','https://wx.example',?,'{}',?,?)",
                 (f"file:{key}", _now(), _now()))
    seeded.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, created_at) "
                 "VALUES('g-api','res-api','project','proj1',?)", (_now(),))
    seeded.write("INSERT INTO resources(id, kind, name, config_json, created_at, "
                 "updated_at) VALUES('res-mcp','mcp','Docs',?,?,?)",
                 ('{"mcp_command": "npx -y @scope/docs"}', _now(), _now()))
    seeded.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, created_at) "
                 "VALUES('g-mcp','res-mcp','team','team1',?)", (_now(),))

    captured = {}

    def capture(task):
        captured["env"] = dict(task.extra_env)
        captured["prompt"] = task.prompt
        captured["mcp"] = task.mcp_config
        captured["mcp_body"] = open(task.mcp_config).read() if task.mcp_config else ""
        return RunResult(status="succeeded")

    SCRIPT.append(capture)
    orch.dispatch(req())
    await orch.wait_idle()
    assert captured["env"]["BASTET_RES_WEATHER_URL"] == "https://wx.example"
    assert captured["env"]["BASTET_RES_WEATHER_KEY"] == "sk-pool"
    assert "Weather" in captured["prompt"] and "sk-pool" not in captured["prompt"]
    assert "@scope/docs" in captured["mcp_body"]
    import os
    assert not os.path.exists(captured["mcp"])  # cleaned up: the file held a secret


async def test_pool_resources_reach_the_tests_pass_gate(orch, seeded):
    """The gate is part of the stage, so it gets the run's resource grants."""
    add_template(seeded, "resource-gate", [{
        "name": "e2e", "gate": "tests-pass",
        "gate_config": {"command":
            "test \"$BASTET_RES_ANTHROPIC_MAIN_URL\" = https://upstream.example"},
    }])
    SCRIPT.append(RunResult(status="succeeded"))
    job_id = orch.dispatch(req(template_id="resource-gate"))
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"


async def test_executor_account_does_not_wipe_injected_credentials(orch, seeded,
                                                                   tmp_path):
    """Regression: binding an agent to an executor account used to *replace*
    the run env, silently dropping every project secret and resource."""
    from bastet_agent_os.db import now as _now

    secret = tmp_path / "tok"
    secret.write_text("keep-me")
    seeded.write("INSERT INTO resources(id, kind, name, secret_ref, config_json, "
                 "created_at, updated_at) VALUES('sec-k','secret','k',?,?,?,?)",
                 (f"file:{secret}", '{"env_name": "KEEP"}', _now(), _now()))
    seeded.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, created_at) "
                 "VALUES('g-k','sec-k','project','proj1',?)", (_now(),))
    seeded.write("INSERT INTO executor_accounts(id, executor_type, name, home_dir, "
                 "created_at) VALUES('acc1','fake','A',?,?)",
                 (str(tmp_path / "profile"), _now()))
    seeded.write("UPDATE agents SET account_id='acc1' WHERE id='fakebot'")

    captured = {}
    SCRIPT.append(lambda task: (captured.update(env=dict(task.extra_env))
                                or RunResult(status="succeeded")))
    orch.dispatch(req())
    await orch.wait_idle()
    assert captured["env"]["KEEP"] == "keep-me"


# ---- a gate that cannot run is not a failing test -----------------------------------

def test_tests_pass_separates_a_missing_command_from_a_failing_test(tmp_path):
    """Live case: the web-dev preset runs `npm run test:e2e`, the project had no
    package.json, and the pipeline reported it exactly like a failed test — so the
    obvious next move was to re-run the agent, which could never fix it."""
    stage = parse_stages([{"name": "t", "gate": "tests-pass",
                           "gate_config": {"command": "exit 1"}}])[0]
    real = evaluate_gate(stage, str(tmp_path), None)
    assert real.verdict == "failed" and real.config_error is False

    stage.gate_config["command"] = "definitely-not-a-real-command --flag"
    missing = evaluate_gate(stage, str(tmp_path), None)
    assert missing.verdict == "failed" and missing.config_error is True
    assert "工作流設定問題" in missing.detail
    assert "definitely-not-a-real-command" in missing.detail   # names the command


def test_tests_pass_receives_resource_environment(tmp_path):
    stage = parse_stages([{"name": "t", "gate": "tests-pass",
                           "gate_config": {"command":
                               "test \"$BASTET_RES_GITLAB_URL\" = ssh://git/repo"}}])[0]
    outcome = evaluate_gate(stage, str(tmp_path), None,
                            env={"BASTET_RES_GITLAB_URL": "ssh://git/repo"})
    assert outcome.verdict == "passed"


def test_tests_pass_keeps_failure_line_when_long_tail_hides_it(tmp_path):
    stage = parse_stages([{"name": "t", "gate": "tests-pass",
                           "gate_config": {"command":
                               "printf 'not ok 7 - missing resource\\n'; "
                               "python -c 'print(\"ok\\n\" * 5000)'; exit 1"}}])[0]
    outcome = evaluate_gate(stage, str(tmp_path), None)
    assert outcome.verdict == "failed"
    assert "not ok 7 - missing resource" in outcome.detail


def test_npm_missing_script_is_recognised_as_a_config_error(tmp_path):
    """The exact output from the host: npm exits 1, so only the message tells us
    the script does not exist."""
    from bastet_agent_os.workflow import _command_unavailable

    npm_output = ('npm ERR! Missing script: "test:e2e"\n'
                  'npm ERR! To see a list of scripts, run:\n  npm run\n')
    assert _command_unavailable(1, npm_output) is True
    assert _command_unavailable(127, "sh: 1: pytest: not found") is True
    assert _command_unavailable(126, "permission denied") is True
    # a genuine test failure must not be mistaken for one
    assert _command_unavailable(1, "2 failed, 8 passed in 3.1s") is False
    assert _command_unavailable(1, "AssertionError: expected 3 got 4") is False


def test_a_config_error_blocks_with_a_reason_that_points_at_the_template(tmp_path):
    stage = parse_stages([{"name": "t", "gate": "tests-pass",
                           "gate_config": {"command": "npm run test:e2e"}}])[0]
    out = evaluate_gate(stage, str(tmp_path), None)
    assert out.config_error is True
    assert "模板" in out.detail          # where the fix lives


async def test_a_config_error_is_persisted_on_the_gate_row(orch, seeded):
    """The UI must read a flag, not pattern-match translated prose."""
    add_template(seeded, "npm", [{"name": "e2e", "gate": "tests-pass",
                                  "gate_config": {"command": "npm run test:e2e"}}])
    SCRIPT.append(RunResult(status="succeeded"))
    job_id = orch.dispatch(req(template_id="npm"))
    await orch.wait_idle()

    gate = seeded.one("SELECT g.* FROM gate_results g JOIN runs r ON r.id=g.run_id "
                      "WHERE r.job_id=? ORDER BY g.at DESC LIMIT 1", (job_id,))
    assert gate["verdict"] == "failed" and gate["config_error"] == 1
    audited = seeded.one("SELECT detail_json FROM audit_log WHERE action='gate.failed' "
                         "ORDER BY at DESC LIMIT 1")
    assert '"config_error": true' in audited["detail_json"]


async def test_gate_revalidation_reuses_successful_run_and_finishes(orch, seeded):
    add_template(seeded, "revalidate", [{
        "name": "e2e", "gate": "tests-pass", "on_fail": "block",
        "gate_config": {"command": "exit 1"},
    }])
    SCRIPT.append(RunResult(status="succeeded", summary="existing output is complete"))
    job_id = orch.dispatch(req(template_id="revalidate"))
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "blocked"
    run = seeded.one("SELECT id FROM runs WHERE job_id=?", (job_id,))["id"]

    stages = [{"name": "e2e", "gate": "tests-pass", "on_fail": "block",
               "gate_config": {"command": "exit 0"}}]
    seeded.write("UPDATE jobs SET stages_snapshot_json=? WHERE id=?",
                 (json.dumps(stages), job_id))
    result = orch.revalidate_gate(job_id, user="operator")

    assert result == {"job_id": job_id, "status": "done", "stage": None,
                      "verdict": "passed", "reused_run_id": run}
    assert seeded.one("SELECT COUNT(*) n FROM runs WHERE job_id=?", (job_id,))["n"] == 1
    assert seeded.one("SELECT COUNT(*) n FROM gate_results WHERE run_id=?", (run,))["n"] == 2
    assert seeded.one("SELECT COUNT(*) n FROM stage_handoffs WHERE job_id=?",
                      (job_id,))["n"] == 1
    audit = seeded.one("SELECT detail_json FROM audit_log WHERE action='gate.revalidated'")
    assert '"verdict": "passed"' in audit["detail_json"]


async def test_gate_revalidation_refuses_non_deterministic_gate(orch, seeded):
    add_template(seeded, "review", [{"name": "review", "gate": "agent-review",
                                      "on_fail": "block"}])
    SCRIPT.append(RunResult(status="succeeded", structured_verdict={
        "verdict": "reject", "reasons": ["no"]}))
    job_id = orch.dispatch(req(template_id="review"))
    await orch.wait_idle()
    with pytest.raises(ValueError, match="only tests-pass"):
        orch.revalidate_gate(job_id)


def test_gate_tools_names_what_each_preset_needs():
    """The shipped presets run pytest/npm/make; nothing used to check they exist,
    so a project could burn an agent run and then fail on a missing runner."""
    from bastet_agent_os.config import gate_tools

    tools = {t["program"]: t for t in gate_tools()}
    assert {"pytest", "npm", "make"} <= set(tools)
    assert any("前後端" in source for source in tools["pytest"]["used_by"])
    # a compound command contributes every program it runs
    assert any("&&" not in source for source in tools["npm"]["used_by"])
    for tool in tools.values():
        assert tool["path"] is None or tool["path"].startswith("/")


def test_gate_tools_includes_user_templates(db):
    from bastet_agent_os.config import gate_tools

    db.write("INSERT INTO workflow_templates(id, name, version, stages_json) "
             "VALUES('mine','mine',1,?)",
             (json.dumps([{"name": "t", "gate": "tests-pass",
                           "gate_config": {"command": "poetry run pytest -q"}}]),))
    tools = {t["program"]: t for t in gate_tools(db)}
    assert "poetry" in tools and any("mine" in s for s in tools["poetry"]["used_by"])
    # an absolute path needs no PATH lookup, so it is not reported as a dependency
    db.write("INSERT INTO workflow_templates(id, name, version, stages_json) "
             "VALUES('abs','abs',1,?)",
             (json.dumps([{"name": "t", "gate": "tests-pass",
                           "gate_config": {"command": "/usr/bin/env pytest"}}]),))
    assert "/usr/bin/env" not in {t["program"] for t in gate_tools(db)}


def test_the_venv_bin_is_on_path_for_gate_commands():
    """systemd hands the service a minimal PATH; a runner installed next to
    bastet must still be findable, but last so a project's own wins."""
    import os
    import sys

    from bastet_agent_os.config import augment_path

    own = str(pathlib.Path(sys.executable).parent)
    # CI runners put the python bin dir FIRST on PATH, which made this test's
    # precondition false: augment_path correctly does nothing when the dir is
    # already present. Strip it so the append-last behaviour is what's tested.
    os.environ["PATH"] = os.pathsep.join(
        entry for entry in os.environ["PATH"].split(os.pathsep) if entry != own)
    augment_path()
    entries = os.environ["PATH"].split(os.pathsep)
    # last, and ONLY last: in our Docker image the interpreter sits in
    # /usr/local/bin, which is also a TOOL_DIR, and prepending it there put
    # Bastet's own pytest ahead of the project's (found by running the suite
    # inside the shipped image — GitHub's runners hide it in hostedtoolcache)
    assert entries.count(own) == 1
    assert entries[-1] == own


def test_doctor_sees_the_same_path_a_gate_will_see():
    """Live finding: doctor reported pytest missing while it sat in Bastet's own
    venv, because the CLI never augmented PATH. A report that disagrees with the
    gate is worse than none."""
    import subprocess
    import sys

    proc = subprocess.run([sys.executable, "-c",
                           "from bastet_agent_os import cli; "
                           "cli._prepare(); "
                           "import os, sys, pathlib; "
                           "print(str(pathlib.Path(sys.executable).parent) in "
                           "os.environ['PATH'].split(os.pathsep))"],
                          capture_output=True, text=True,
                          env={**__import__("os").environ, "PYTHONPATH": "src"})
    assert proc.stdout.strip() == "True", proc.stderr


def test_a_stage_can_declare_its_own_time_budget():
    """Live case: a heavy optimisation stage ran 50-70 minutes and kept being
    killed by the fixed dispatch default — four timeouts, an hour of work lost
    each time. A template that knows its stage is heavy can now say so."""
    stages = parse_stages([
        {"name": "heavy", "gate": "auto", "timeout_s": 7200},
        {"name": "light", "gate": "auto"},
    ])
    assert stages[0].timeout_s == 7200
    assert stages[1].timeout_s == 0                # inherits the dispatch default
    assert stages[0].to_dict()["timeout_s"] == 7200
    assert parse_stages([{"name": "x", "gate": "auto", "timeout_s": -5}])[0] \
        .timeout_s == 0                            # nonsense clamps to inherit


def test_test_gate_replaces_non_utf8_output_instead_of_crashing(tmp_path):
    """Binary-ish test output is evidence, not a reason to crash the driver."""
    import sys

    command = (f'"{sys.executable}" -c "import sys; '
               'sys.stdout.buffer.write(bytes([255, 254, 10]))"')
    stage = parse_stages([{"name": "test", "gate": "tests-pass",
                           "gate_config": {"command": command}}])[0]

    outcome = evaluate_gate(stage, str(tmp_path), None)

    assert outcome.verdict == "passed"
    assert "\ufffd" in outcome.detail
