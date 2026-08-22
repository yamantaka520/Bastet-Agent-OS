"""Durable maintenance/drain fence shared by every dispatch path."""

from __future__ import annotations

from .db import Db, now


class MaintenanceModeError(ValueError):
    pass


def state(db: Db) -> dict:
    lock = db.one("SELECT * FROM maintenance_lock WHERE id=1")
    jobs = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status='in_progress'")["n"]
    runs = db.one("SELECT COUNT(*) AS n FROM runs WHERE status IN "
                  "('queued','running','waiting_input')")["n"]
    enabled = bool(lock["enabled"])
    return {**dict(lock), "enabled": enabled, "active_jobs": jobs,
            "active_runs": runs, "drained": enabled and jobs == 0 and runs == 0}


def enabled(db: Db) -> bool:
    row = db.one("SELECT enabled FROM maintenance_lock WHERE id=1")
    return bool(row and row["enabled"])


def enter(db: Db, actor: str, reason: str = "") -> dict:
    current = state(db)
    if not current["enabled"]:
        stamp = now()
        db.write("UPDATE maintenance_lock SET enabled=1,generation=generation+1,"
                 "owner=?,reason=?,entered_at=?,released_at=NULL WHERE id=1",
                 (actor, reason.strip(), stamp))
        db.audit(actor, "maintenance.enter", "system", "dispatch-fence",
                 {"reason": reason.strip()})
    return state(db)


def leave(db: Db, actor: str) -> dict:
    current = state(db)
    if current["enabled"]:
        db.write("UPDATE maintenance_lock SET enabled=0,released_at=? WHERE id=1", (now(),))
        db.audit(actor, "maintenance.leave", "system", "dispatch-fence",
                 {"generation": current["generation"]})
    return state(db)


def require_dispatch_allowed(db: Db) -> None:
    if enabled(db):
        raise MaintenanceModeError(
            "maintenance drain lock is active; new dispatch and retry are paused")


def require_drained(db: Db) -> dict:
    current = state(db)
    if not current["enabled"]:
        raise MaintenanceModeError("enter maintenance mode before updating components")
    if not current["drained"]:
        raise MaintenanceModeError(
            f"maintenance drain is not complete: {current['active_jobs']} active jobs, "
            f"{current['active_runs']} active runs")
    return current
