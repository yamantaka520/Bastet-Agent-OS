"""Project lifecycle: the state a project is actually in, and who may move it.

A project is not a folder of jobs — it goes somewhere:

    planning ──confirm plan──▶ ready ──start──▶ running ⇄ paused
                                 ▲                │
                                 └────stop────────┘
                                                  │ all tasks done
                                                  ▼
                                            maintenance ──close──▶ closed
                                                                     │ reopen
                                                                     ▼
                                                                  planning

Only declared transitions are allowed, each one is audited, and the light shown
in the UI is this status — never a guess derived from job rows.
"""

from __future__ import annotations

import json
from typing import Any

from .db import now

PLANNING = "planning"
READY = "ready"
RUNNING = "running"
PAUSED = "paused"
MAINTENANCE = "maintenance"
CLOSED = "closed"

STATUSES = [PLANNING, READY, RUNNING, PAUSED, MAINTENANCE, CLOSED]

# status -> light shown in the UI (the UI localises the label, not the colour)
LIGHTS = {PLANNING: "🔵", READY: "🟡", RUNNING: "🟢", PAUSED: "⏸",
          MAINTENANCE: "🟠", CLOSED: "⚪"}

# transition name -> (allowed from, to)
TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "confirm_plan": ((PLANNING,), READY),
    "start": ((READY, PAUSED, MAINTENANCE), RUNNING),
    "pause": ((RUNNING,), PAUSED),
    "resume": ((PAUSED,), RUNNING),
    "stop": ((RUNNING, PAUSED), READY),
    "complete": ((RUNNING,), MAINTENANCE),      # all tasks done, awaiting acceptance
    "close": ((MAINTENANCE, READY, PAUSED), CLOSED),
    "reopen": ((CLOSED,), PLANNING),
    "replan": ((READY, MAINTENANCE, PAUSED), PLANNING),
}


class LifecycleError(Exception):
    pass


def status_of(db, project_id: str) -> str:
    row = db.one("SELECT status FROM projects WHERE id=?", (project_id,))
    if row is None:
        raise LifecycleError("project not found")
    return row["status"] or PLANNING


def allowed_transitions(status: str) -> list[str]:
    return [name for name, (froms, _) in TRANSITIONS.items() if status in froms]


def apply(db, project_id: str, transition: str, actor: str = "",
          detail: dict[str, Any] | None = None) -> str:
    """Move the project. Raises LifecycleError when the move is not declared —
    an illegal transition is a bug, not something to paper over."""
    if transition not in TRANSITIONS:
        raise LifecycleError(f"unknown transition {transition!r}")
    froms, target = TRANSITIONS[transition]
    current = status_of(db, project_id)
    if current not in froms:
        raise LifecycleError(
            f"{transition} needs status in {list(froms)}, project is {current!r}")
    db.write("UPDATE projects SET status=?, updated_at=? WHERE id=?",
             (target, now(), project_id))
    db.audit(actor or "system", f"project.{transition}", "project", project_id,
             {"from": current, "to": target, **(detail or {})})
    return target


def task_plan(db, project_id: str) -> dict[str, Any]:
    """The PM agent's decomposition proposal, as stored on the project."""
    row = db.one("SELECT config_json FROM projects WHERE id=?", (project_id,))
    if row is None:
        raise LifecycleError("project not found")
    config = json.loads(row["config_json"] or "{}")
    plan = config.get("task_plan") or {}
    return {"tasks": plan.get("tasks", []), "at": plan.get("at"),
            "by": plan.get("by", ""), "confirmed": bool(plan.get("confirmed"))}


def save_task_plan(db, project_id: str, tasks: list[dict[str, Any]], by: str,
                   confirmed: bool = False) -> None:
    row = db.one("SELECT config_json FROM projects WHERE id=?", (project_id,))
    config = json.loads(row["config_json"] or "{}")
    config["task_plan"] = {"tasks": tasks, "at": now(), "by": by,
                           "confirmed": confirmed}
    db.write("UPDATE projects SET config_json=?, updated_at=? WHERE id=?",
             (json.dumps(config), now(), project_id))


def job_progress(db, project_id: str) -> dict[str, int]:
    counts = {"total": 0, "done": 0, "active": 0, "blocked": 0, "open": 0,
              "cancelled": 0}
    for row in db.query("SELECT status, COUNT(*) AS n FROM jobs WHERE project_id=? "
                        "GROUP BY status", (project_id,)):
        counts["total"] += row["n"]
        if row["status"] == "done":
            counts["done"] += row["n"]
        elif row["status"] == "in_progress":
            counts["active"] += row["n"]
        elif row["status"] == "blocked":
            counts["blocked"] += row["n"]
        elif row["status"] == "cancelled":
            counts["cancelled"] += row["n"]
        else:
            counts["open"] += row["n"]
    return counts


def maybe_complete(db, project_id: str, actor: str = "runner") -> str | None:
    """Running project with every task finished → maintenance (awaiting
    acceptance). Returns the new status when it moved."""
    if status_of(db, project_id) != RUNNING:
        return None
    progress = job_progress(db, project_id)
    if not progress["total"]:
        return None
    settled = progress["done"] + progress["cancelled"]
    if settled == progress["total"]:
        return apply(db, project_id, "complete", actor, {"jobs": progress})
    return None


def overview(db, project_id: str) -> dict[str, Any]:
    status = status_of(db, project_id)
    return {"status": status, "light": LIGHTS.get(status, "⚪"),
            "transitions": allowed_transitions(status),
            "progress": job_progress(db, project_id),
            "task_plan": task_plan(db, project_id)}
