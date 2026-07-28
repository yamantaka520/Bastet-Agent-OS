"""Grant resolution and two-phase quota enforcement (SPEC §5.2.4, §5.3).

Phase 1 (dispatch-time, all accounting precisions): concurrency + budget
estimate. Phase 2 (in-stream, gateway precision only): check-and-reserve per
request; a bounded overshoot on the final request is accepted by design —
claiming zero overshoot would be false precision.

Reservations are in-memory: gateway and control plane share one process (SPEC
§3.2), so no cross-process state is needed in M1.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from .db import Db

# Reserve per admitted in-flight request until its real cost lands in the ledger.
DEFAULT_RESERVE_USD = 0.25


class QuotaError(Exception):
    def __init__(self, message: str, policy: str = "block"):
        super().__init__(message)
        self.policy = policy


@dataclass
class GrantView:
    id: str
    resource_id: str
    scope_type: str
    scope_id: str
    budget_usd: float | None
    budget_tokens: int | None
    period: str
    max_concurrency: int | None
    on_exceed: str


def _period_start(period: str) -> str | None:
    nowdt = datetime.now(UTC)
    if period == "daily":
        return nowdt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(
            timespec="seconds"
        )
    if period == "monthly":
        return nowdt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(
            timespec="seconds"
        )
    return None  # lifetime


def resolve_grant(db: Db, resource_id: str, project_id: str, agent_id: str) -> GrantView | None:
    """Most specific enabled, unexpired grant wins: agent > project > team."""
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    scopes = [("agent", agent_id), ("project", project_id)]
    if project is not None:
        scopes.append(("team", project["team_id"]))
    for scope_type, scope_id in scopes:
        row = db.one(
            "SELECT * FROM grants WHERE resource_id=? AND scope_type=? AND scope_id=? "
            "AND enabled=1 AND (expires_at IS NULL OR expires_at > datetime('now')) "
            "ORDER BY priority DESC LIMIT 1",
            (resource_id, scope_type, scope_id),
        )
        if row is not None:
            return GrantView(
                id=row["id"],
                resource_id=row["resource_id"],
                scope_type=row["scope_type"],
                scope_id=row["scope_id"],
                budget_usd=row["budget_usd"],
                budget_tokens=row["budget_tokens"],
                period=row["period"],
                max_concurrency=row["max_concurrency"],
                on_exceed=row["on_exceed"],
            )
    return None


def _runs_for_scope_filter(grant: GrantView) -> tuple[str, tuple]:
    """SQL fragment selecting runs governed by this grant's scope."""
    if grant.scope_type == "agent":
        return "r.agent_id = ?", (grant.scope_id,)
    if grant.scope_type == "project":
        return "j.project_id = ?", (grant.scope_id,)
    return "j.project_id IN (SELECT id FROM projects WHERE team_id = ?)", (grant.scope_id,)


def spent_usd(db: Db, grant: GrantView) -> float:
    where, params = _runs_for_scope_filter(grant)
    period_start = _period_start(grant.period)
    period_sql = " AND l.at >= ?" if period_start else ""
    if period_start:
        params = (*params, period_start)
    row = db.one(
        "SELECT COALESCE(SUM(l.cost_usd), 0) AS c FROM usage_ledger l "
        "JOIN runs r ON r.id = l.run_id JOIN jobs j ON j.id = r.job_id "
        f"WHERE l.resource_id = ? AND {where}{period_sql}",
        (grant.resource_id, *params),
    )
    reported = db.one(
        # runs without ledger rows (reported/estimated precision) still count
        "SELECT COALESCE(SUM(r.cost_usd), 0) AS c FROM runs r JOIN jobs j ON j.id = r.job_id "
        f"WHERE r.resource_id = ? AND r.accounting_precision != 'gateway' AND {where}",
        (grant.resource_id, *_runs_for_scope_filter(grant)[1]),
    )
    return float(row["c"]) + float(reported["c"])


def active_runs(db: Db, grant: GrantView) -> int:
    where, params = _runs_for_scope_filter(grant)
    row = db.one(
        "SELECT COUNT(*) AS n FROM runs r JOIN jobs j ON j.id = r.job_id "
        f"WHERE r.resource_id = ? AND r.status IN ('queued','running','waiting_input') "
        f"AND {where}",
        (grant.resource_id, *params),
    )
    return int(row["n"])


class Reservations:
    """In-flight request reservations for phase-2 admission (single process)."""

    def __init__(self, reserve_usd: float = DEFAULT_RESERVE_USD):
        self.reserve_usd = reserve_usd
        self._lock = threading.Lock()
        self._by_grant: dict[str, int] = {}

    def admit(self, db: Db, grant: GrantView) -> None:
        """check-and-reserve; raises QuotaError when the budget is exhausted."""
        if grant.budget_usd is None:
            return
        with self._lock:
            reserved = self._by_grant.get(grant.id, 0) * self.reserve_usd
            if spent_usd(db, grant) + reserved + self.reserve_usd > grant.budget_usd:
                raise QuotaError(
                    f"grant {grant.id} budget exhausted (budget {grant.budget_usd} USD)",
                    policy=grant.on_exceed,
                )
            self._by_grant[grant.id] = self._by_grant.get(grant.id, 0) + 1

    def settle(self, grant: GrantView) -> None:
        """Release one reservation after the request's real cost hit the ledger."""
        if grant.budget_usd is None:
            return
        with self._lock:
            n = self._by_grant.get(grant.id, 0)
            if n > 0:
                self._by_grant[grant.id] = n - 1


def dispatch_check(db: Db, grant: GrantView) -> None:
    """Phase-1 check at dispatch time (applies to every accounting precision)."""
    if grant.max_concurrency is not None and active_runs(db, grant) >= grant.max_concurrency:
        raise QuotaError(
            f"grant {grant.id} concurrency limit {grant.max_concurrency} reached",
            policy=grant.on_exceed,
        )
    if grant.budget_usd is not None and spent_usd(db, grant) >= grant.budget_usd:
        raise QuotaError(
            f"grant {grant.id} budget exhausted (budget {grant.budget_usd} USD)",
            policy=grant.on_exceed,
        )
