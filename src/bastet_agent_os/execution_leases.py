"""Short durable leases for control-plane work outside workflow stage nodes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _expiry(ttl_s: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=max(1, ttl_s))).isoformat(
        timespec="seconds")


def acquire(db, *, kind: str, target_id: str, owner_id: str, ttl_s: int) -> bool:
    """Claim or reclaim an expired lease with one SQLite compare-and-set."""
    stamp = _stamp()
    changed = db.write(
        "INSERT INTO execution_leases(kind,target_id,owner_id,acquired_at,"
        "heartbeat_at,expires_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(kind,target_id) DO UPDATE SET owner_id=excluded.owner_id,"
        "acquired_at=excluded.acquired_at,heartbeat_at=excluded.heartbeat_at,"
        "expires_at=excluded.expires_at WHERE execution_leases.expires_at<=? "
        "OR execution_leases.owner_id=excluded.owner_id",
        (kind, target_id, owner_id, stamp, stamp, _expiry(ttl_s), stamp)).rowcount
    return bool(changed)


def renew(db, *, kind: str, target_id: str, owner_id: str, ttl_s: int) -> bool:
    stamp = _stamp()
    return bool(db.write(
        "UPDATE execution_leases SET heartbeat_at=?,expires_at=? "
        "WHERE kind=? AND target_id=? AND owner_id=?",
        (stamp, _expiry(ttl_s), kind, target_id, owner_id)).rowcount)


def release(db, *, kind: str, target_id: str, owner_id: str) -> bool:
    return bool(db.write(
        "DELETE FROM execution_leases WHERE kind=? AND target_id=? AND owner_id=?",
        (kind, target_id, owner_id)).rowcount)


def owned(db, *, kind: str, target_id: str, owner_id: str) -> bool:
    """Whether this owner still has an unexpired right to apply side effects."""
    return db.one(
        "SELECT 1 AS x FROM execution_leases WHERE kind=? AND target_id=? "
        "AND owner_id=? AND expires_at>?",
        (kind, target_id, owner_id, _stamp())) is not None
