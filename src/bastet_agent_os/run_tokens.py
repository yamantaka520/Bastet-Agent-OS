"""Run tokens (SPEC §5.2.1): the short-lived credential a run presents to the
gateway. >=128-bit CSPRNG, opaque, hash-only at rest, revoked on any terminal
run state. Blast radius on leak: spend that run's grant budget until hard stop.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from .db import Db, new_id, now


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(db: Db, run_id: str, ttl_seconds: int) -> str:
    """Issue a token for a run. The plaintext exists only in the return value."""
    token = "brt_" + secrets.token_urlsafe(32)  # ~256 bits
    expires = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(
        timespec="seconds"
    )
    db.write(
        "INSERT INTO run_tokens(id, run_id, token_hash, expires_at) VALUES(?,?,?,?)",
        (new_id("rtk"), run_id, _hash(token), expires),
    )
    return token


def verify(db: Db, token: str) -> str | None:
    """Return the run_id for a valid token, else None (expired/revoked/unknown)."""
    row = db.one("SELECT * FROM run_tokens WHERE token_hash=?", (_hash(token),))
    if row is None or row["revoked_at"] is not None:
        return None
    if row["expires_at"] <= now():
        return None
    return row["run_id"]


def revoke_for_run(db: Db, run_id: str) -> None:
    db.write(
        "UPDATE run_tokens SET revoked_at=? WHERE run_id=? AND revoked_at IS NULL",
        (now(), run_id),
    )
