"""Plans and jobs are admitted as complete graphs, before partial execution."""

import json

from fake_executor import add_template, req

from bastet_agent_os import admission
from bastet_agent_os.db import now
from bastet_agent_os.workflow import parse_stages


def test_strict_workflow_admission_requires_the_declared_stage_role(seeded):
    stages = parse_stages([{"name": "design", "role": "ui-designer",
                            "gate": "auto"}])
    report = admission.workflow_report(seeded, "proj1", stages, strict_roles=True)
    assert not report["ok"]
    assert report["errors"][0]["code"] == "stage-role-unassigned"

    seeded.write("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
                 "VALUES('proj1','ag1','ui-designer',10)")
    report = admission.workflow_report(seeded, "proj1", stages, strict_roles=True)
    assert report["ok"]
    assert report["stages"][0]["viable"][0]["agent_id"] == "ag1"


def test_task_role_must_be_real_and_assigned(seeded):
    report = admission.project_plan_report(seeded, "proj1", [{
        "id": "ux", "title": "UX", "spec": "flow", "needs": [],
        "role": "ux-designer",
    }])
    assert not report["ok"]
    assert report["errors"][0]["code"] == "task-role-unassigned"


def test_skill_supply_is_resolved_for_the_exact_executor(seeded):
    seeded.write("INSERT INTO project_agent_roles(project_id,agent_id,role,preference) "
                 "VALUES('proj1','ag1','visual-artist',10)")
    stages = parse_stages([{"name": "art", "role": "visual-artist", "gate": "auto",
                            "requires": ["skill:sprites"]}])
    assert not admission.workflow_report(
        seeded, "proj1", stages, strict_roles=True)["ok"]

    digest = "sha256:" + "a" * 64
    config = {"skill_id": "sprites", "skill_version": "1",
              "skill_source": "/opt/sprites-src", "skill_target": "/opt/sprites",
              "skill_digest": digest, "compatible_executors": "claude-code",
              "install_command": "install-sprites",
              "install": {"status": "installed", "digest": digest},
              "test": {"status": "ok", "digest": digest}}
    ts = now()
    seeded.write("INSERT INTO resources(id,kind,name,config_json,created_at,updated_at) "
                 "VALUES('skill-sprites','skill','Sprites',?,?,?)",
                 (json.dumps(config), ts, ts))
    seeded.write("INSERT INTO grants(id,resource_id,scope_type,scope_id,created_at) "
                 "VALUES('grant-sprites','skill-sprites','project','proj1',?)", (ts,))

    report = admission.workflow_report(seeded, "proj1", stages, strict_roles=True)
    assert report["ok"]
    assert report["stages"][0]["viable"] == [
        {"agent_id": "ag1", "executor_type": "claude-code"}]


def test_dispatch_checks_later_stages_before_creating_a_job(orch, seeded):
    add_template(seeded, "future-skill", [
        {"name": "plan", "gate": "auto", "needs": []},
        {"name": "render", "gate": "auto", "needs": ["plan"],
         "requires": ["skill:renderer"]},
    ])
    before = seeded.one("SELECT COUNT(*) n FROM jobs")["n"]
    try:
        orch.dispatch(req(template_id="future-skill"))
    except admission.AdmissionError as exc:
        assert "skill:renderer" in str(exc)
    else:
        raise AssertionError("whole-graph admission should reject the future gap")
    assert seeded.one("SELECT COUNT(*) n FROM jobs")["n"] == before


def test_unknown_or_undeliverable_host_capability_is_a_plan_error(seeded):
    unknown = admission.workflow_report(
        seeded, "proj1", parse_stages([{
            "name": "x", "gate": "auto", "requires": ["quantum.browser"]}]),
        default_agent_id="fakebot")
    assert {item["code"] for item in unknown["errors"]} == {"capability-unknown"}

    unmanaged = admission.workflow_report(
        seeded, "proj1", parse_stages([{
            "name": "x", "gate": "auto", "requires": ["browser.playwright"]}]),
        default_agent_id="fakebot")
    assert {item["code"] for item in unmanaged["errors"]} == {
        "capability-undeliverable"}
