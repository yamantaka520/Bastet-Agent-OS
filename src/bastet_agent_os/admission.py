"""Whole-graph admission before a plan or job is allowed to run.

Runtime routing remains defensive, but it is too late to discover at stage six
that no assigned role can execute it or that its Skill was never supplied.  This
module evaluates the frozen workflow against the project's real agents, route
contracts, grants and managed Skills.  The same structured report drives the UI,
plan confirmation, project start/restart and direct dispatch.
"""

from __future__ import annotations

import json
from typing import Any

from .executors.base import get_executor, route_incompatibility
from .governance import resolve_grant
from .workflow import StageDef, parse_stages


class AdmissionError(ValueError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        detail = "; ".join(item["detail"] for item in report["errors"][:8])
        super().__init__(f"admission blocked: {detail}")


def _agents(db, project_id: str, role: str = "") -> list[Any]:
    role_sql = "AND par.role=?" if role else ""
    params = (project_id, role) if role else (project_id,)
    return list(db.query(
        "SELECT DISTINCT a.* FROM project_agent_roles par JOIN agents a "
        "ON a.id=par.agent_id WHERE par.project_id=? " + role_sql +
        " AND a.enabled=1 AND a.depleted_at IS NULL "
        "ORDER BY par.preference DESC", params))


def _route_problem(db, project_id: str, resource, agent, stage: StageDef) -> str | None:
    if resource is not None and resolve_grant(
            db, resource["id"], project_id, agent["id"]) is None:
        return f"agent has no grant for LLM resource {resource['id']!r}"
    try:
        executor = get_executor(agent["executor_type"])
    except KeyError:
        return f"executor {agent['executor_type']!r} is not installed"
    config = json.loads(agent["config_json"] or "{}")
    model = config.get("model")
    flavor = None
    if resource is not None:
        routing = json.loads(resource["routing_json"] or "{}")
        model = model or routing.get("default_model")
        flavor = resource["api_flavor"]
    return route_incompatibility(
        executor, has_gateway=resource is not None, api_flavor=flavor,
        model=model, read_only=stage.read_only)


def _candidate_rows(db, project_id: str, stage: StageDef, default_agent_id: str,
                    strict_roles: bool) -> tuple[list[Any], bool]:
    assigned = _agents(db, project_id, stage.role or "") if stage.role else []
    if stage.role and strict_roles:
        return assigned, bool(assigned)
    rows = list(assigned)
    if default_agent_id:
        default = db.one("SELECT * FROM agents WHERE id=? AND enabled=1 "
                         "AND depleted_at IS NULL", (default_agent_id,))
        if default is not None:
            rows.append(default)
    rows.extend(_agents(db, project_id))
    seen = set()
    unique = []
    for row in rows:
        if row["id"] not in seen:
            seen.add(row["id"])
            unique.append(row)
    return unique, bool(assigned) if stage.role else True


def workflow_report(db, project_id: str, stages: list[StageDef],
                    default_agent_id: str = "", resource_id: str | None = None,
                    strict_roles: bool = False) -> dict[str, Any]:
    project = db.one("SELECT team_id FROM projects WHERE id=?", (project_id,))
    if project is None:
        raise AdmissionError({"ok": False, "errors": [{
            "code": "project-missing", "detail": f"project {project_id!r} not found"}],
            "warnings": [], "stages": []})
    resource = None
    errors: list[dict[str, Any]] = []
    if resource_id:
        resource = db.one("SELECT * FROM resources WHERE id=? AND kind='llm' "
                          "AND enabled=1", (resource_id,))
        if resource is None:
            errors.append({"code": "llm-resource-unavailable",
                           "detail": f"LLM resource {resource_id!r} is missing or disabled"})

    from .execution_capabilities import CATALOG, resolve_skill_required

    stage_rows = []
    for stage in stages:
        candidates, role_assigned = _candidate_rows(
            db, project_id, stage, default_agent_id, strict_roles)
        stage_errors: list[dict[str, Any]] = []
        if stage.role and strict_roles and not role_assigned:
            stage_errors.append({
                "code": "stage-role-unassigned", "stage": stage.name,
                "role": stage.role,
                "detail": f"stage {stage.name!r} requires assigned role {stage.role!r}"})

        host_requirements = [item for item in stage.requires
                             if not item.startswith("skill:")]
        for requirement in host_requirements:
            if requirement not in CATALOG:
                stage_errors.append({
                    "code": "capability-unknown", "stage": stage.name,
                    "requirement": requirement,
                    "detail": f"stage {stage.name!r} requires unknown capability "
                              f"{requirement!r}"})
            elif requirement == "browser.playwright" and not (
                    stage.gate == "tests-pass"
                    or stage.gate_config.get("precheck_command")):
                stage_errors.append({
                    "code": "capability-undeliverable", "stage": stage.name,
                    "requirement": requirement,
                    "detail": f"stage {stage.name!r} declares browser.playwright but "
                              "has no trusted tests-pass/precheck path"})

        viable, rejected = [], []
        for agent in candidates:
            problem = _route_problem(db, project_id, resource, agent, stage)
            if problem is None:
                skills = resolve_skill_required(
                    db, project_id, project["team_id"], agent["executor_type"],
                    stage.requires)
                missing = [status for status in skills if not status.available]
                if missing:
                    problem = "; ".join(
                        f"{status.capability}: {status.detail}" for status in missing)
            if problem:
                rejected.append({"agent_id": agent["id"],
                                 "executor_type": agent["executor_type"],
                                 "detail": problem})
            else:
                viable.append({"agent_id": agent["id"],
                               "executor_type": agent["executor_type"]})
        if not viable and not stage_errors:
            detail = ("; ".join(f"{item['agent_id']}: {item['detail']}"
                                for item in rejected)
                      or "no enabled, funded candidate agent")
            stage_errors.append({
                "code": "stage-no-viable-agent", "stage": stage.name,
                "role": stage.role or "",
                "detail": f"stage {stage.name!r} has no viable agent: {detail}"})
        errors.extend(stage_errors)
        stage_rows.append({"stage": stage.name, "role": stage.role or "",
                           "requires": stage.requires, "viable": viable,
                           "rejected": rejected, "errors": stage_errors})
    return {"ok": not errors, "errors": errors, "warnings": [],
            "stages": stage_rows}


def project_plan_report(db, project_id: str, tasks: list[dict[str, Any]],
                        fallback_agent_id: str = "",
                        require_default: bool = False) -> dict[str, Any]:
    """Check every task's actual default route plus the project workflow."""
    project = db.one("SELECT default_template_id FROM projects WHERE id=?",
                     (project_id,))
    if project is None:
        raise ValueError(f"unknown project {project_id!r}")
    if project["default_template_id"]:
        template = db.one("SELECT stages_json FROM workflow_templates WHERE id=?",
                          (project["default_template_id"],))
        if template is None:
            stages = []
            errors = [{"code": "template-missing",
                       "detail": f"workflow template {project['default_template_id']!r} "
                                 "does not exist"}]
        else:
            stages = parse_stages(json.loads(template["stages_json"]))
            errors = []
    else:
        stages = parse_stages([{"name": "work", "gate": "auto"}])
        errors = []

    warnings: list[dict[str, Any]] = []
    reports = []
    for task in tasks:
        role = str(task.get("role") or "").strip()
        assigned = _agents(db, project_id, role) if role else []
        if role and not assigned:
            errors.append({
                "code": "task-role-unassigned", "task": task.get("id"), "role": role,
                "detail": f"task {task.get('id')!r} requires assigned role {role!r}"})
            continue
        default = (assigned[0]["id"] if assigned else fallback_agent_id)
        if not default:
            all_agents = _agents(db, project_id)
            default = all_agents[0]["id"] if all_agents else ""
        if not default and not any(stage.role for stage in stages):
            item = {"code": "task-default-agent-missing", "task": task.get("id"),
                    "detail": f"task {task.get('id')!r} needs a fallback agent at start"}
            (errors if require_default else warnings).append(item)
            continue
        report = workflow_report(
            db, project_id, stages, default_agent_id=default, strict_roles=True)
        for item in report["errors"]:
            errors.append({**item, "task": task.get("id")})
        reports.append({"task": task.get("id"), "default_agent_id": default,
                        "workflow": report})

    # Repeated stage errors across tasks are one actionable problem in the UI.
    unique: list[dict[str, Any]] = []
    seen = set()
    for item in errors:
        key = (item.get("code"), item.get("stage"), item.get("role"),
               item.get("requirement"), item.get("detail"))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return {"ok": not unique, "errors": unique, "warnings": warnings,
            "tasks": reports}


def project_workflow_report(db, project_id: str) -> dict[str, Any]:
    """Static readiness used before decomposition and on project overview."""
    project = db.one("SELECT default_template_id FROM projects WHERE id=?",
                     (project_id,))
    if project is None:
        raise ValueError(f"unknown project {project_id!r}")
    if not project["default_template_id"]:
        stages = parse_stages([{"name": "work", "gate": "auto"}])
    else:
        template = db.one("SELECT stages_json FROM workflow_templates WHERE id=?",
                          (project["default_template_id"],))
        if template is None:
            return {"ok": False, "errors": [{
                "code": "template-missing",
                "detail": f"workflow template {project['default_template_id']!r} "
                          "does not exist"}], "warnings": [], "stages": []}
        stages = parse_stages(json.loads(template["stages_json"]))
    return workflow_report(db, project_id, stages, strict_roles=True)


def require(report: dict[str, Any]) -> None:
    if not report["ok"]:
        raise AdmissionError(report)
