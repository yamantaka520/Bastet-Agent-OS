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
    # work dispatched straight from chat or the board, without the runner
    "activate": ((PLANNING, READY, PAUSED, MAINTENANCE), RUNNING),
    "start": ((READY, PAUSED, MAINTENANCE), RUNNING),
    "pause": ((RUNNING,), PAUSED),
    "resume": ((PAUSED,), RUNNING),
    "stop": ((RUNNING, PAUSED), READY),
    "complete": ((RUNNING,), MAINTENANCE),      # all tasks done, awaiting acceptance
    "close": ((MAINTENANCE, READY, PAUSED), CLOSED),
    "reopen": ((CLOSED,), PLANNING),
    "replan": ((READY, MAINTENANCE, PAUSED), PLANNING),
}


# transitions the system performs for itself; never offered as a UI control,
# because `activate` beside "confirm plan" would let a click skip the human gate
INTERNAL_TRANSITIONS = {"activate", "complete"}


class LifecycleError(Exception):
    pass


def status_of(db, project_id: str) -> str:
    row = db.one("SELECT status FROM projects WHERE id=?", (project_id,))
    if row is None:
        raise LifecycleError("project not found")
    return row["status"] or PLANNING


def allowed_transitions(status: str, include_internal: bool = False) -> list[str]:
    return [name for name, (froms, _) in TRANSITIONS.items()
            if status in froms
            and (include_internal or name not in INTERNAL_TRANSITIONS)]


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
            "by": plan.get("by", ""), "confirmed": bool(plan.get("confirmed")),
            "source": plan.get("source") or {}}


def save_task_plan(db, project_id: str, tasks: list[dict[str, Any]], by: str,
                   confirmed: bool = False,
                   source: dict[str, Any] | None = None) -> None:
    """Store the plan. `source` records which conversation it came from, so a
    proposal can be told apart from the discussion that has since moved on."""
    row = db.one("SELECT config_json FROM projects WHERE id=?", (project_id,))
    config = json.loads(row["config_json"] or "{}")
    previous = config.get("task_plan") or {}
    config["task_plan"] = {"tasks": tasks, "at": now(), "by": by,
                           "confirmed": confirmed,
                           "source": source if source is not None
                                     else previous.get("source") or {}}
    db.write("UPDATE projects SET config_json=?, updated_at=? WHERE id=?",
             (json.dumps(config), now(), project_id))


def chat_state(db, project_id: str) -> dict[str, Any]:
    """When this project's planning conversation was last touched."""
    row = db.one("SELECT COUNT(*) AS messages, MAX(m.at) AS last_at "
                 "FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id "
                 "WHERE s.scope_type='project' AND s.scope_id=? AND m.role != 'system'",
                 (project_id,))
    return {"messages": row["messages"] if row else 0,
            "last_at": row["last_at"] if row else None}


def clear_undispatched(db, project_id: str, actor: str = "") -> int:
    """Throw away the proposal but keep every task that already has a job.
    A stale breakdown should be removable without losing running work."""
    plan = task_plan(db, project_id)
    kept = [t for t in plan["tasks"] if t.get("job_id")]
    dropped = len(plan["tasks"]) - len(kept)
    if dropped:
        save_task_plan(db, project_id, kept, by=plan["by"],
                       confirmed=plan["confirmed"], source={})
        db.audit(actor or "system", "project.tasks.clear", "project", project_id,
                 {"dropped": dropped, "kept": len(kept)})
    return dropped


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


def link_job(db, project_id: str, job_id: str, title: str, spec: str,
             origin: str = "chat") -> int:
    """Put a dispatched job on the project's plan.

    Matching by title first matters: dispatching a task the PM already proposed
    should light up *that* row, not append a near-duplicate. Otherwise the
    project tab and the board drift into two different accounts of the work."""
    plan = task_plan(db, project_id)
    tasks = [dict(t) for t in plan["tasks"]]
    if any(t.get("job_id") == job_id for t in tasks):
        return len(tasks)
    wanted = (title or "").strip()
    for task in tasks:
        if not task.get("job_id") and wanted and task.get("title", "").strip() == wanted:
            task["job_id"] = job_id
            task["origin"] = task.get("origin") or origin
            save_task_plan(db, project_id, tasks, by=plan["by"],
                           confirmed=plan["confirmed"])
            return len(tasks)
    tasks.append({"title": wanted or job_id, "spec": (spec or "")[:4000],
                  "role": "", "job_id": job_id, "origin": origin})
    save_task_plan(db, project_id, tasks, by=plan["by"], confirmed=plan["confirmed"])
    return len(tasks)


def unlink_job(db, project_id: str, job_id: str) -> str:
    """Take a deleted job off the plan.

    A row the dispatch itself created disappears with the job; a task the PM
    proposed keeps its text and simply goes back to "not dispatched", because the
    task still needs doing even though this attempt is gone."""
    plan = task_plan(db, project_id)
    kept: list[dict[str, Any]] = []
    outcome = "absent"
    for task in plan["tasks"]:
        if task.get("job_id") != job_id:
            kept.append(task)
            continue
        if task.get("origin") in ("chat", "dispatch", "runner"):
            outcome = "row_removed"
            continue                      # the row existed only for that job
        outcome = "unlinked"
        kept.append({k: v for k, v in task.items() if k not in ("job_id", "origin")})
    if outcome != "absent":
        save_task_plan(db, project_id, kept, by=plan["by"],
                       confirmed=plan["confirmed"])
    return outcome


def sync_from_jobs(db, project_id: str, actor: str = "system") -> str | None:
    """Make the status match what is actually happening.

    A light that says 規劃中 while a job is executing is worse than no light at
    all, and a project whose tasks have all finished should be waiting for
    acceptance rather than pretending to still run. Returns the new status when
    it moved."""
    try:
        status = status_of(db, project_id)
    except LifecycleError:
        return None
    if status == CLOSED:
        return None                       # a closed project is not resurrected
    progress = job_progress(db, project_id)
    in_flight = progress["active"] + progress["blocked"] + progress["open"]
    if in_flight and status in (PLANNING, READY, PAUSED, MAINTENANCE):
        return apply(db, project_id, "activate", actor,
                     {"reason": "work in flight", "jobs": progress})
    if not in_flight and status == RUNNING and progress["total"]:
        # a runner mid-list has settled tasks but undispatched ones remain; calling
        # that "finished" would stop the run after its first task
        pending = [t for t in task_plan(db, project_id)["tasks"]
                   if not t.get("job_id")]
        if pending:
            return None
        return apply(db, project_id, "complete", actor, {"jobs": progress})
    return None


def reconcile(db, project_id: str, actor: str = "system") -> dict[str, Any]:
    """Heal a project whose plan or light drifted from its jobs.

    Event-driven sync only fixes work dispatched *after* the code that does it —
    a job created earlier, or a control plane restarted mid-run, would stay wrong
    forever. So this also runs at startup and whenever the project is read: both
    steps are idempotent and only write when something actually changed."""
    linked = 0
    for job in db.query("SELECT id, title, spec_md FROM jobs WHERE project_id=? "
                        "ORDER BY created_at", (project_id,)):
        before = task_plan(db, project_id)["tasks"]
        if any(t.get("job_id") == job["id"] for t in before):
            continue
        link_job(db, project_id, job["id"], job["title"], job["spec_md"] or "",
                 origin="dispatch")
        linked += 1
    moved = sync_from_jobs(db, project_id, actor)
    return {"linked": linked, "status": moved}


def reconcile_all(db, actor: str = "server") -> list[dict[str, Any]]:
    out = []
    for row in db.query("SELECT id FROM projects"):
        result = reconcile(db, row["id"], actor)
        if result["linked"] or result["status"]:
            out.append({"project": row["id"], **result})
    return out


def plan_with_jobs(db, project_id: str) -> dict[str, Any]:
    """The plan, each task carrying its job's live state — this is what makes
    the project tab and the Kanban board the same picture."""
    plan = task_plan(db, project_id)
    # A breakdown taken before the conversation moved on is a stale snapshot; say
    # so rather than letting it pass for "the plan". Compare the message count
    # recorded when it was taken, not timestamps: `now()` has second resolution,
    # so a decomposition that reads the chat and saves in the same second would
    # otherwise flag itself stale immediately.
    chat = chat_state(db, project_id)
    source = plan.get("source") or {}
    seen = source.get("messages")
    proposal = [t for t in plan["tasks"] if not t.get("job_id")]
    # `source.at` is stamped only by a decomposition; plan["at"] moves whenever
    # the plan is touched (linking a job), so it would mask staleness
    taken_at = source.get("at")
    if isinstance(seen, int):
        provenance = "recorded"
        stale = bool(proposal and chat["messages"] > seen)
    elif taken_at:
        provenance = "recorded"
        stale = bool(proposal and chat["last_at"] and chat["last_at"] > taken_at)
    else:
        # a plan from before provenance existed: we cannot know when it was taken,
        # and an unverifiable breakdown is precisely what passes for "the plan"
        # while describing something the conversation abandoned
        provenance = "unknown"
        stale = False
    tasks = []
    for task in plan["tasks"]:
        item = dict(task)
        if item.get("job_id"):
            job = db.one("SELECT status, stage, title FROM jobs WHERE id=?",
                         (item["job_id"],))
            if job is None:
                item["job_status"] = "missing"
            else:
                item["job_status"] = job["status"]
                item["job_stage"] = job["stage"]
        tasks.append(item)
    return {**plan, "tasks": tasks, "stale": stale, "chat": chat,
            "provenance": provenance,
            "unverified": bool(proposal and provenance == "unknown"),
            "dispatched": sum(1 for t in tasks if t.get("job_id"))}


def overview(db, project_id: str) -> dict[str, Any]:
    status = status_of(db, project_id)
    return {"status": status, "light": LIGHTS.get(status, "⚪"),
            "transitions": allowed_transitions(status),
            "progress": job_progress(db, project_id),
            "task_plan": plan_with_jobs(db, project_id)}
