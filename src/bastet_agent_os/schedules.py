"""Durable, timezone-aware recurring workflow dispatch.

Schedules create ordinary workflow jobs; they never bypass workflow gates. Each
occurrence is claimed in SQLite before dispatch and uses the orchestrator's
existing deterministic project-task receipt, so restart and competing servers
converge on one job.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import new_id, now
from .orchestrator import DispatchRequest

log = logging.getLogger("bastet.schedules")
POLL_S = 30
MAX_SEARCH_MINUTES = 366 * 24 * 60 * 5


class ScheduleError(ValueError):
    pass


def _values(raw: str, minimum: int, maximum: int, *, sunday: bool = False) -> set[int]:
    values: set[int] = set()
    if not raw:
        raise ScheduleError("cron field cannot be empty")
    for part in raw.split(","):
        base, slash, step_text = part.partition("/")
        try:
            step = int(step_text) if slash else 1
        except ValueError as exc:
            raise ScheduleError(f"invalid cron step {part!r}") from exc
        if step < 1:
            raise ScheduleError("cron step must be positive")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ScheduleError(f"invalid cron range {base!r}") from exc
        else:
            try:
                start = int(base)
                end = maximum if slash else start
            except ValueError as exc:
                raise ScheduleError(f"invalid cron value {base!r}") from exc
        if start < minimum or end > maximum or start > end:
            raise ScheduleError(
                f"cron value {base!r} must be between {minimum} and {maximum}")
        values.update(range(start, end + 1, step))
    if sunday and 7 in values:
        values.remove(7)
        values.add(0)
    return values


def parse_cron(expression: str) -> tuple[set[int], ...]:
    fields = expression.strip().split()
    if len(fields) != 5:
        raise ScheduleError("cron must contain minute hour day-of-month month day-of-week")
    return (
        _values(fields[0], 0, 59),
        _values(fields[1], 0, 23),
        _values(fields[2], 1, 31),
        _values(fields[3], 1, 12),
        _values(fields[4], 0, 7, sunday=True),
    )


def timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"unknown timezone {value!r}") from exc


def next_run(expression: str, zone_name: str, after: datetime | str) -> str:
    minute, hour, dom, month, dow = parse_cron(expression)
    zone = timezone(zone_name)
    if isinstance(after, str):
        try:
            after_dt = datetime.fromisoformat(after.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ScheduleError("invalid schedule timestamp") from exc
    else:
        after_dt = after
    if after_dt.tzinfo is None:
        after_dt = after_dt.replace(tzinfo=UTC)
    candidate = after_dt.astimezone(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
    dom_any = expression.split()[2] == "*"
    dow_any = expression.split()[4] == "*"
    for _ in range(MAX_SEARCH_MINUTES):
        local = candidate.astimezone(zone)
        cron_dow = (local.weekday() + 1) % 7
        day_matches = ((local.day in dom and cron_dow in dow) if dom_any or dow_any
                       else (local.day in dom or cron_dow in dow))
        if local.minute in minute and local.hour in hour and local.month in month \
                and day_matches:
            return candidate.isoformat(timespec="seconds")
        candidate += timedelta(minutes=1)
    raise ScheduleError("cron has no occurrence within five years")


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    expression = str(payload.get("cron") or "").strip()
    zone_name = str(payload.get("timezone") or "UTC").strip()
    prompt = str(payload.get("prompt") or "").strip()
    name = str(payload.get("name") or "").strip()
    parse_cron(expression)
    timezone(zone_name)
    if not name:
        raise ScheduleError("schedule name is required")
    if not prompt:
        raise ScheduleError("schedule prompt is required")
    try:
        timeout_s = int(payload.get("timeout_s") or 3600)
    except (TypeError, ValueError) as exc:
        raise ScheduleError("schedule timeout_s must be an integer") from exc
    if timeout_s < 60 or timeout_s > 86400:
        raise ScheduleError("schedule timeout_s must be between 60 and 86400")
    delivery = payload.get("delivery") or {"mode": "none"}
    if not isinstance(delivery, dict):
        raise ScheduleError("schedule delivery must be an object")
    return {
        "name": name,
        "cron": expression,
        "timezone": zone_name,
        "prompt": prompt,
        "role": str(payload.get("role") or "").strip(),
        "agent_id": str(payload.get("agent_id") or "").strip() or None,
        "template_id": str(payload.get("template_id") or "").strip() or None,
        "delivery": delivery,
        "timeout_s": timeout_s,
        "enabled": bool(payload.get("enabled", True)),
    }


def _validate_targets(db, value: dict[str, Any]) -> None:
    if value["agent_id"] and db.one(
            "SELECT id FROM agents WHERE id=?", (value["agent_id"],)) is None:
        raise ScheduleError(f"agent {value['agent_id']!r} not found")
    if value["template_id"] and db.one(
            "SELECT id FROM workflow_templates WHERE id=?",
            (value["template_id"],)) is None:
        raise ScheduleError(f"workflow template {value['template_id']!r} not found")


def create(db, project_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    if db.one("SELECT id FROM projects WHERE id=?", (project_id,)) is None:
        raise ScheduleError("project not found")
    value = validate(payload)
    _validate_targets(db, value)
    schedule_id = new_id("schedule")
    stamp = now()
    due = next_run(value["cron"], value["timezone"], stamp)
    db.write(
        "INSERT INTO workflow_schedules(id,project_id,name,cron,timezone,prompt,role,"
        "agent_id,template_id,delivery_json,timeout_s,enabled,next_run_at,created_at,"
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (schedule_id, project_id, value["name"], value["cron"], value["timezone"],
         value["prompt"], value["role"], value["agent_id"], value["template_id"],
         json.dumps(value["delivery"]), value["timeout_s"], int(value["enabled"]), due,
         stamp, stamp))
    db.audit(actor, "schedule.create", "schedule", schedule_id,
             {"project": project_id, "cron": value["cron"],
              "timezone": value["timezone"], "next_run_at": due})
    return get(db, schedule_id)


def get(db, schedule_id: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM workflow_schedules WHERE id=?", (schedule_id,))
    if row is None:
        raise ScheduleError("schedule not found")
    item = dict(row)
    item["delivery"] = json.loads(item.pop("delivery_json") or "{}")
    item["enabled"] = bool(item["enabled"])
    latest = db.one(
        "SELECT scheduled_for,status,job_id,claimed_at,finished_at,error "
        "FROM workflow_schedule_runs WHERE schedule_id=? "
        "ORDER BY scheduled_for DESC LIMIT 1", (schedule_id,))
    item["last_occurrence"] = dict(latest) if latest else None
    return item


def list_for_project(db, project_id: str) -> list[dict[str, Any]]:
    return [get(db, row["id"]) for row in db.query(
        "SELECT id FROM workflow_schedules WHERE project_id=? ORDER BY created_at",
        (project_id,))]


def update(db, schedule_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    current = get(db, schedule_id)
    merged = {
        key: payload.get(key, current[key])
        for key in ("name", "cron", "timezone", "prompt", "role", "agent_id",
                    "template_id", "delivery", "timeout_s", "enabled")
    }
    value = validate(merged)
    _validate_targets(db, value)
    stamp = now()
    timing_changed = (value["cron"] != current["cron"]
                      or value["timezone"] != current["timezone"]
                      or (value["enabled"] and not current["enabled"]))
    due = next_run(value["cron"], value["timezone"], stamp) \
        if timing_changed else current["next_run_at"]
    db.write(
        "UPDATE workflow_schedules SET name=?,cron=?,timezone=?,prompt=?,role=?,"
        "agent_id=?,template_id=?,delivery_json=?,timeout_s=?,enabled=?,next_run_at=?,"
        "updated_at=? WHERE id=?",
        (value["name"], value["cron"], value["timezone"], value["prompt"],
         value["role"], value["agent_id"], value["template_id"],
         json.dumps(value["delivery"]), value["timeout_s"], int(value["enabled"]),
         due, stamp, schedule_id))
    db.audit(actor, "schedule.update", "schedule", schedule_id,
             {"cron": value["cron"], "timezone": value["timezone"],
              "enabled": value["enabled"], "next_run_at": due})
    return get(db, schedule_id)


def delete(db, schedule_id: str, actor: str) -> None:
    get(db, schedule_id)
    db.write("DELETE FROM workflow_schedules WHERE id=?", (schedule_id,))
    db.audit(actor, "schedule.delete", "schedule", schedule_id, {})


class WorkflowScheduler:
    def __init__(self, db, orchestrator, bus=None):
        self.db = db
        self.orchestrator = orchestrator
        self.bus = bus

    async def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:
                log.warning("schedule tick failed: %r", exc)
            await asyncio.sleep(POLL_S)

    def tick(self, at: str | None = None) -> list[str]:
        stamp = at or now()
        claimed = []
        for row in self.db.query(
                "SELECT * FROM workflow_schedules WHERE enabled=1 AND next_run_at<=? "
                "ORDER BY next_run_at,id", (stamp,)):
            scheduled_for = row["next_run_at"]
            inserted = self.db.write(
                "INSERT OR IGNORE INTO workflow_schedule_runs(schedule_id,scheduled_for,"
                "status,claimed_at) VALUES(?,?,'claimed',?)",
                (row["id"], scheduled_for, stamp))
            following = next_run(row["cron"], row["timezone"], stamp)
            self.db.write(
                "UPDATE workflow_schedules SET next_run_at=?,updated_at=? "
                "WHERE id=? AND next_run_at=?",
                (following, stamp, row["id"], scheduled_for))
            if inserted.rowcount == 1:
                claimed.append(f"{row['id']}\x1f{scheduled_for}")
        # Any claim left by a dead process is safe to replay: orchestrator
        # dispatch uses the same deterministic plan_key/task_id receipt.
        for row in self.db.query(
                "SELECT schedule_id,scheduled_for FROM workflow_schedule_runs "
                "WHERE status='claimed' ORDER BY claimed_at"):
            key = f"{row['schedule_id']}\x1f{row['scheduled_for']}"
            if key not in claimed:
                claimed.append(key)
        jobs = []
        for key in claimed:
            schedule_id, scheduled_for = key.split("\x1f", 1)
            job_id = self._dispatch(schedule_id, scheduled_for)
            if job_id:
                jobs.append(job_id)
        return jobs

    def run_now(self, schedule_id: str) -> str:
        get(self.db, schedule_id)
        scheduled_for = now()
        inserted = self.db.write(
            "INSERT OR IGNORE INTO workflow_schedule_runs(schedule_id,scheduled_for,"
            "status,claimed_at) VALUES(?,?,'claimed',?)",
            (schedule_id, scheduled_for, scheduled_for))
        if inserted.rowcount != 1:
            raise ScheduleError("schedule occurrence already exists")
        return self._dispatch(schedule_id, scheduled_for)

    def _dispatch(self, schedule_id: str, scheduled_for: str) -> str:
        row = self.db.one("SELECT * FROM workflow_schedules WHERE id=? AND enabled=1",
                          (schedule_id,))
        if row is None:
            self._finish(schedule_id, scheduled_for, "skipped", "schedule disabled")
            return ""
        project = self.db.one("SELECT status FROM projects WHERE id=?", (row["project_id"],))
        if project is None or project["status"] == "closed":
            self._finish(schedule_id, scheduled_for, "skipped", "project is closed")
            return ""
        if row["last_job_id"]:
            previous = self.db.one("SELECT status FROM jobs WHERE id=?", (row["last_job_id"],))
            if previous is not None and previous["status"] not in ("done", "cancelled"):
                self._finish(schedule_id, scheduled_for, "skipped",
                             f"previous job {row['last_job_id']} is still active")
                return ""
        agent_id = self._agent(row)
        if not agent_id:
            self._finish(schedule_id, scheduled_for, "failed", "no eligible agent")
            return ""
        try:
            job_id = self.orchestrator.dispatch(
                DispatchRequest(
                    project_id=row["project_id"], prompt=row["prompt"],
                    title=f"{row['name']} · {scheduled_for}", agent_id=agent_id,
                    template_id=row["template_id"], timeout_s=int(row["timeout_s"]),
                    origin="schedule", delivery=json.loads(row["delivery_json"] or "{}"),
                    plan_key=f"schedule:{schedule_id}", task_id=scheduled_for,
                ), actor="scheduler")
        except Exception as exc:
            from .maintenance_mode import MaintenanceModeError
            if isinstance(exc, MaintenanceModeError):
                return ""  # keep claim for automatic retry after the fence opens
            self._finish(schedule_id, scheduled_for, "failed",
                         f"{type(exc).__name__}: {exc}"[:500])
            return ""
        stamp = now()
        self.db.write(
            "UPDATE workflow_schedule_runs SET status='dispatched',job_id=?,finished_at=? "
            "WHERE schedule_id=? AND scheduled_for=?",
            (job_id, stamp, schedule_id, scheduled_for))
        self.db.write(
            "UPDATE workflow_schedules SET last_run_at=?,last_job_id=?,updated_at=? WHERE id=?",
            (scheduled_for, job_id, stamp, schedule_id))
        self.db.audit("scheduler", "schedule.dispatched", "schedule", schedule_id,
                      {"scheduled_for": scheduled_for, "job_id": job_id})
        if self.bus is not None:
            self.bus.emit("schedule.dispatched", row["project_id"],
                          schedule_id=schedule_id, job_id=job_id)
        return job_id

    def _agent(self, row) -> str:
        if row["agent_id"]:
            match = self.db.one(
                "SELECT id FROM agents WHERE id=? AND enabled=1 AND depleted_at IS NULL",
                (row["agent_id"],))
            return match["id"] if match else ""
        if row["role"]:
            match = self.db.one(
                "SELECT a.id FROM project_agent_roles par JOIN agents a "
                "ON a.id=par.agent_id WHERE par.project_id=? AND par.role=? "
                "AND a.enabled=1 AND a.depleted_at IS NULL "
                "ORDER BY par.preference DESC LIMIT 1", (row["project_id"], row["role"]))
            return match["id"] if match else ""
        match = self.db.one(
            "SELECT a.id FROM project_agent_roles par JOIN agents a ON a.id=par.agent_id "
            "WHERE par.project_id=? AND a.enabled=1 AND a.depleted_at IS NULL "
            "ORDER BY par.preference DESC LIMIT 1", (row["project_id"],))
        return match["id"] if match else ""

    def _finish(self, schedule_id: str, scheduled_for: str,
                status: str, error: str) -> None:
        stamp = now()
        self.db.write(
            "UPDATE workflow_schedule_runs SET status=?,error=?,finished_at=? "
            "WHERE schedule_id=? AND scheduled_for=?",
            (status, error, stamp, schedule_id, scheduled_for))
        row = self.db.one("SELECT project_id FROM workflow_schedules WHERE id=?",
                          (schedule_id,))
        self.db.audit("scheduler", f"schedule.{status}", "schedule", schedule_id,
                      {"scheduled_for": scheduled_for, "error": error})
        if self.bus is not None:
            self.bus.emit(f"schedule.{status}", row["project_id"] if row else None,
                          schedule_id=schedule_id, error=error)
