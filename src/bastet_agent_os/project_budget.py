"""Project-wide daily spend ceilings for unattended execution.

Grant budgets answer whether one resource may be used.  This ceiling answers a
different operational question: whether the project may start more work today.
Already-running jobs are allowed to reach a safe stage/job boundary; the project
runner then waits durably until the configured local day rolls over.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Db, now
from .governance import QuotaError


class ProjectBudgetError(ValueError):
    pass


class ProjectBudgetExceeded(QuotaError):
    pass


def validate(limit: float | None, timezone: str) -> tuple[float | None, str]:
    if limit is not None:
        try:
            limit = float(limit)
        except (TypeError, ValueError) as exc:
            raise ProjectBudgetError("daily project cost limit must be a number") from exc
        if not math.isfinite(limit) or limit <= 0:
            raise ProjectBudgetError("daily project cost limit must be greater than zero")
    timezone = str(timezone or "UTC").strip() or "UTC"
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ProjectBudgetError(f"unknown IANA timezone: {timezone}") from exc
    return limit, timezone


def _window(at: datetime, timezone: str) -> tuple[datetime, datetime]:
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    zone = ZoneInfo(timezone)
    local = at.astimezone(zone)
    start_local = datetime.combine(local.date(), time.min, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def status(db: Db, project_id: str, *, at: datetime | None = None) -> dict:
    project = db.one("SELECT config_json FROM projects WHERE id=?", (project_id,))
    if project is None:
        raise ProjectBudgetError("project not found")
    try:
        config = json.loads(project["config_json"] or "{}")
    except json.JSONDecodeError as exc:
        raise ProjectBudgetError("project config is not valid JSON") from exc
    raw_limit = config.get("daily_cost_limit_usd")
    limit, timezone = validate(raw_limit, config.get("daily_cost_timezone") or "UTC")
    start, reset = _window(at or datetime.now(UTC), timezone)
    start_iso = start.isoformat(timespec="seconds")
    ledger = db.one(
        "SELECT COALESCE(SUM(l.cost_usd),0) AS cost FROM usage_ledger l "
        "JOIN runs r ON r.id=l.run_id JOIN jobs j ON j.id=r.job_id "
        "WHERE j.project_id=? AND julianday(l.at)>=julianday(?)",
        (project_id, start_iso))
    reported = db.one(
        "SELECT COALESCE(SUM(r.cost_usd),0) AS cost FROM runs r "
        "JOIN jobs j ON j.id=r.job_id WHERE j.project_id=? "
        "AND COALESCE(r.accounting_precision,'')!='gateway' "
        "AND julianday(COALESCE(r.finished_at,r.started_at))>=julianday(?)",
        (project_id, start_iso))
    spent = float(ledger["cost"] or 0) + float(reported["cost"] or 0)
    exceeded = limit is not None and spent >= limit
    return {
        "enabled": limit is not None,
        "limit_usd": limit,
        "spent_usd": round(spent, 6),
        "remaining_usd": None if limit is None else round(max(0.0, limit - spent), 6),
        "exceeded": exceeded,
        "timezone": timezone,
        "period_start": start_iso,
        "resets_at": reset.isoformat(timespec="seconds"),
    }


def sync_pause(db: Db, project_id: str, *, actor: str = "runner",
               at: datetime | None = None) -> tuple[dict, str]:
    """Persist one pause/resume receipt per budget day; returns event name."""
    value = status(db, project_id, at=at)
    event = ""
    if value["exceeded"]:
        changed = db.write(
            "INSERT OR IGNORE INTO project_cost_pauses(project_id,period_start,"
            "limit_usd,spent_usd,paused_at) VALUES(?,?,?,?,?)",
            (project_id, value["period_start"], value["limit_usd"],
             value["spent_usd"], now())).rowcount
        if changed:
            event = "budget.exceeded"
            db.audit(actor, "project.budget_paused", "project", project_id, value)
    else:
        changed = db.write(
            "UPDATE project_cost_pauses SET resumed_at=? WHERE project_id=? "
            "AND resumed_at IS NULL", (now(), project_id)).rowcount
        if changed:
            event = "budget.resumed"
            db.audit(actor, "project.budget_resumed", "project", project_id, value)
    value["paused"] = value["exceeded"]
    return value, event


def require_available(db: Db, project_id: str) -> None:
    value, _ = sync_pause(db, project_id, actor="orchestrator")
    if value["exceeded"]:
        raise ProjectBudgetExceeded(
            f"project {project_id} daily cost ceiling reached "
            f"({value['spent_usd']:.4f}/{value['limit_usd']:.4f} USD); "
            f"new work resumes after {value['resets_at']}", policy="queue")
