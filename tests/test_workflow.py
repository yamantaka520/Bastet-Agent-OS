"""Workflow engine: stage pipelines, gate protocol, retries, human approval."""

import json
import pathlib

import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os.executors.base import RunResult
from bastet_agent_os.workflow import evaluate_gate, parse_stages

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
    from bastet_agent_os.workflow_presets import GATES, PRESETS, ROLES

    role_ids = {r["id"] for r in ROLES}
    gate_ids = {g["id"] for g in GATES}
    assert len(PRESETS) >= 6
    for preset in PRESETS:
        stages = parse_stages(preset["stages"])   # engine must accept every preset
        assert stages, preset["id"]
        for raw, stage in zip(preset["stages"], stages, strict=True):
            assert stage.gate in gate_ids
            if raw.get("role"):
                assert raw["role"] in role_ids, f"{preset['id']}: unknown role"
            if stage.gate == "tests-pass":
                assert stage.gate_config.get("command")
        # side-effecting last stage should ask a human
        assert stages[-1].gate in ("human-approve", "agent-review"), preset["id"]


# ---- role prompts & project secrets in runs -----------------------------------------

async def test_role_prompt_and_project_secret_reach_the_run(orch, seeded, monkeypatch,
                                                            tmp_path):
    from bastet_agent_os.db import now as _now

    seeded.write("INSERT INTO role_prompts(role, label, prompt, builtin, updated_at) "
                 "VALUES('reviewer','審查者','你是審查者：只找真正的問題。',1,?)", (_now(),))
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
