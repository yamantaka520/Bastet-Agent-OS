"""Execution-host inventory and fail-closed dispatch placement.

AMOS federation proves that projects and people share an organisation.  It does
not prove that a peer has the repository, grants, Skills, executor credentials,
or authority to accept a job.  This module keeps that separate boundary
explicit.  Local placement is fully operational; peer inventory is observable
but cannot become dispatchable until the authenticated transport layer can
produce a destination admission receipt.
"""

from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .db import new_id, now

HOST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")


class PlacementError(ValueError):
    """A requested host cannot safely accept this job."""

    def __init__(self, receipt: dict[str, Any]):
        self.receipt = receipt
        super().__init__(receipt["reason"])


@dataclass(frozen=True)
class PlacementDecision:
    receipt_id: str
    requested_host_id: str
    selected_host_id: str
    requirements: dict[str, Any]
    candidates: list[dict[str, Any]]


def _json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return sorted({str(item) for item in parsed if str(item).strip()}) \
        if isinstance(parsed, list) else []


def local_host_id(db) -> str:
    row = db.one("SELECT value FROM meta WHERE key='local_execution_host_id'")
    if row:
        return row["value"]
    candidate = "host-" + new_id("node").split("_", 1)[1]
    # Two server processes may start on the same DB during a supervised
    # handover.  The meta PK elects one identity instead of making startup race.
    db.write("INSERT OR IGNORE INTO meta(key,value) "
             "VALUES('local_execution_host_id',?)", (candidate,))
    return db.one("SELECT value FROM meta WHERE key='local_execution_host_id'")["value"]


def ensure_local_host(db, *, max_concurrency: int | None = None) -> str:
    """Create the durable local identity once and refresh its honest inventory."""
    from .execution_capabilities import CATALOG

    host_id = local_host_id(db)
    ts = now()
    executor_types = [row["executor_type"] for row in db.query(
        "SELECT DISTINCT executor_type FROM agents WHERE enabled=1 ORDER BY executor_type")]
    if max_concurrency is None:
        capacity = 0
        for row in db.query("SELECT config_json FROM agents WHERE enabled=1"):
            try:
                configured = int(json.loads(row["config_json"] or "{}")
                                 .get("max_concurrency", 1))
            except (ValueError, TypeError, json.JSONDecodeError):
                configured = 1
            capacity += max(1, min(16, configured))
        capacity = max(1, capacity)
    else:
        capacity = max(1, int(max_concurrency))
    db.write(
        "INSERT OR IGNORE INTO execution_hosts(id,name,kind,enabled,max_concurrency,"
        "capabilities_json,executor_types_json,status,last_heartbeat_at,created_at,"
        "updated_at) VALUES(?,?, 'local',1,?,?,?,?,?,?,?)",
        (host_id, socket.gethostname(), capacity,
         json.dumps(sorted(CATALOG)), json.dumps(executor_types), "local", ts, ts, ts))
    db.write(
        "UPDATE execution_hosts SET name=?,enabled=1,max_concurrency=?,"
        "capabilities_json=?,executor_types_json=?,status='local',"
        "last_heartbeat_at=?,updated_at=? WHERE id=? AND kind='local'",
        (socket.gethostname(), capacity, json.dumps(sorted(CATALOG)),
         json.dumps(executor_types), ts, ts, host_id))
    return host_id


def validate_peer(host_id: str, name: str, endpoint: str, max_concurrency: int) -> None:
    if not HOST_ID_RE.fullmatch(host_id):
        raise ValueError("host id must be 2-64 letters, digits, '.', '_' or '-'")
    if not name.strip():
        raise ValueError("host name is required")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username \
            or parsed.password or parsed.path not in ("", "/") \
            or parsed.query or parsed.fragment:
        raise ValueError("peer endpoint must be a credential-free HTTPS origin")
    if not 1 <= int(max_concurrency) <= 1024:
        raise ValueError("max_concurrency must be between 1 and 1024")


def register_peer(db, *, host_id: str, name: str, endpoint: str,
                  max_concurrency: int, actor: str) -> dict[str, Any]:
    validate_peer(host_id, name, endpoint, max_concurrency)
    if host_id == local_host_id(db):
        raise ValueError("the local execution host cannot be replaced by a peer")
    if db.one("SELECT 1 FROM execution_hosts WHERE id=?", (host_id,)):
        raise ValueError(f"execution host {host_id!r} already exists")
    ts = now()
    db.write(
        "INSERT INTO execution_hosts(id,name,kind,endpoint,enabled,max_concurrency,status,"
        "created_at,updated_at) VALUES(?,?, 'peer',?,1,?,'registered',?,?)",
        (host_id, name.strip(), endpoint.rstrip("/"), int(max_concurrency), ts, ts))
    db.audit(actor, "execution_host.register", "execution_host", host_id,
             {"name": name.strip(), "endpoint": endpoint.rstrip("/"),
              "max_concurrency": int(max_concurrency), "dispatchable": False})
    return host(db, host_id)


def host(db, host_id: str) -> dict[str, Any]:
    ensure_local_host(db)
    row = db.one("SELECT * FROM execution_hosts WHERE id=?", (host_id,))
    if row is None:
        raise ValueError(f"unknown execution host {host_id!r}")
    return _view(db, row)


def _view(db, row) -> dict[str, Any]:
    local_id = local_host_id(db)
    active = db.one(
        "SELECT COUNT(*) n FROM runs r JOIN jobs j ON j.id=r.job_id "
        "WHERE r.status IN ('queued','running','waiting_input','waiting_external') "
        "AND (j.execution_host_id=? OR (j.execution_host_id IS NULL AND ?=?))",
        (row["id"], row["id"], local_id))["n"]
    capacity = max(1, int(row["max_concurrency"]))
    kind = row["kind"]
    dispatchable = bool(row["enabled"]) and kind == "local"
    reason = "" if dispatchable else (
        "disabled" if not row["enabled"] else
        "authenticated peer transport and destination admission are not established")
    return {
        **dict(row),
        "capabilities": _json_list(row["capabilities_json"]),
        "executor_types": _json_list(row["executor_types_json"]),
        "active": active,
        "available": max(0, capacity - active),
        "dispatchable": dispatchable,
        "placement_blocker": reason,
    }


def list_hosts(db) -> list[dict[str, Any]]:
    ensure_local_host(db)
    return [_view(db, row) for row in db.query(
        "SELECT * FROM execution_hosts ORDER BY kind, name, id")]


def requirements(stages) -> dict[str, Any]:
    return {
        "capabilities": sorted({item for stage in stages for item in stage.requires
                                if not item.startswith("skill:")}),
        "skills": sorted({item.removeprefix("skill:") for stage in stages
                          for item in stage.requires if item.startswith("skill:")}),
        "roles": sorted({stage.role for stage in stages if stage.role}),
        "stages": [stage.name for stage in stages],
    }


def preview(db, project_id: str, stages, requested_host_id: str = "local") -> dict[str, Any]:
    local_id = ensure_local_host(db)
    requested = (requested_host_id or "local").strip()
    target = local_id if requested in ("local", "auto") else requested
    reqs = requirements(stages)
    candidates = list_hosts(db)
    selected = next((item for item in candidates if item["id"] == target), None)
    if selected is None:
        return {"ok": False, "requested_host_id": requested,
                "selected_host_id": None, "requirements": reqs,
                "candidates": candidates,
                "reason": f"unknown execution host {target!r}"}
    if not selected["dispatchable"]:
        return {"ok": False, "requested_host_id": requested,
                "selected_host_id": None, "requirements": reqs,
                "candidates": candidates, "reason": selected["placement_blocker"]}
    missing = sorted(set(reqs["capabilities"]) - set(selected["capabilities"]))
    if missing:
        return {"ok": False, "requested_host_id": requested,
                "selected_host_id": None, "requirements": reqs,
                "candidates": candidates,
                "reason": "selected host does not advertise capabilities: " + ", ".join(missing)}
    return {"ok": True, "requested_host_id": requested,
            "selected_host_id": selected["id"], "requirements": reqs,
            "candidates": candidates, "reason": ""}


def decide(db, project_id: str, stages, requested_host_id: str,
           *, actor: str) -> PlacementDecision:
    result = preview(db, project_id, stages, requested_host_id)
    receipt_id = new_id("place")
    if not result["ok"]:
        db.write(
            "INSERT INTO placement_receipts(id,project_id,requested_host_id,status,"
            "requirements_json,candidates_json,reason,created_at) VALUES(?,?,?,'blocked',?,?,?,?)",
            (receipt_id, project_id, result["requested_host_id"],
             json.dumps(result["requirements"]), json.dumps(result["candidates"]),
             result["reason"], now()))
        db.audit(actor, "job.placement.blocked", "placement_receipt", receipt_id,
                 {"project": project_id, "requested_host": result["requested_host_id"],
                  "reason": result["reason"]})
        raise PlacementError({**result, "receipt_id": receipt_id, "status": "blocked"})
    return PlacementDecision(
        receipt_id, result["requested_host_id"], result["selected_host_id"],
        result["requirements"], result["candidates"])


def receipt_statement(decision: PlacementDecision, project_id: str,
                      job_id: str) -> tuple[str, tuple]:
    return (
        "INSERT INTO placement_receipts(id,project_id,job_id,requested_host_id,"
        "selected_host_id,status,requirements_json,candidates_json,created_at) "
        "VALUES(?,?,?,?,?,'selected',?,?,?)",
        (decision.receipt_id, project_id, job_id, decision.requested_host_id,
         decision.selected_host_id, json.dumps(decision.requirements),
         json.dumps(decision.candidates), now()),
    )
