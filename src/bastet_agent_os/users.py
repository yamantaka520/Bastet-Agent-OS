"""Multi-user auth (SPEC D9, M3): per-user tokens with three roles.

Roles form a strict hierarchy:
  viewer   read-only (GET endpoints, WS subscribe)
  operator run the work: projects/agents/templates/roles/dispatch/approve
  admin    structure & money: resources, grants, users

The bootstrap token in ~/.bastet/api_token stays valid and maps to the
implicit admin user "root", so single-user setups keep working unchanged.
Tokens are CSPRNG, hash-only at rest, disabled users are rejected.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from .db import Db, new_id, now

ROLES = {"viewer": 0, "operator": 1, "admin": 2}


@dataclass
class Auth:
    user_id: str
    name: str
    role: str

    def at_least(self, role: str) -> bool:
        return ROLES[self.role] >= ROLES[role]

    @property
    def actor(self) -> str:
        return f"user:{self.user_id}"


ROOT = Auth(user_id="root", name="root", role="admin")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_user(db: Db, name: str, role: str) -> tuple[str, str]:
    """Create a user; returns (user_id, plaintext_token) — token shown once."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {sorted(ROLES)}")
    token = "but_" + secrets.token_urlsafe(32)  # bastet user token
    user_id = new_id("usr")
    db.write(
        "INSERT INTO users(id, name, role, token_hash, created_at) VALUES(?,?,?,?,?)",
        (user_id, name, role, _hash(token), now()),
    )
    return user_id, token


def verify(db: Db, token: str, bootstrap_token: str) -> Auth | None:
    """Resolve a bearer token to an Auth, or None."""
    if not token:
        return None
    if secrets.compare_digest(token, bootstrap_token):
        return ROOT
    row = db.one("SELECT * FROM users WHERE token_hash=? AND enabled=1", (_hash(token),))
    if row is None:
        return None
    db.write("UPDATE users SET last_used_at=? WHERE id=?", (now(), row["id"]))
    return Auth(user_id=row["id"], name=row["name"], role=row["role"])


def set_role(db: Db, user_id: str, role: str) -> bool:
    """Change what a user may do. Their token keeps working with new powers."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {sorted(ROLES)}")
    cur = db.write("UPDATE users SET role=? WHERE id=?", (role, user_id))
    return cur.rowcount == 1


def rotate_token(db: Db, user_id: str) -> str:
    """Issue a fresh token and invalidate the old one immediately (the hash is
    overwritten, so a leaked token dies the moment this returns)."""
    row = db.one("SELECT id FROM users WHERE id=?", (user_id,))
    if row is None:
        raise ValueError("user not found")
    token = "but_" + secrets.token_urlsafe(32)
    db.write("UPDATE users SET token_hash=? WHERE id=?", (_hash(token), user_id))
    return token


def delete_user(db: Db, user_id: str) -> bool:
    cur = db.write("DELETE FROM users WHERE id=?", (user_id,))
    return cur.rowcount == 1


def capabilities() -> list[dict]:
    """What each role can actually do — kept next to the code that enforces it
    and pinned by tests/test_users_roles.py so the UI never oversells."""
    return [
        {"id": "viewer", "rank": ROLES["viewer"],
         "can": ["read.board", "read.projects", "read.resources", "read.audit",
                 "read.memory", "read.chat"],
         "cannot": ["dispatch", "approve", "chat.send", "edit.org", "admin"]},
        {"id": "operator", "rank": ROLES["operator"],
         "can": ["read.*", "dispatch", "approve", "chat.send", "project.lifecycle",
                 "edit.org", "assign.roles", "attach.resources"],
         "cannot": ["manage.resources", "manage.secrets", "manage.users",
                    "manage.channels", "install.mcp"]},
        {"id": "admin", "rank": ROLES["admin"],
         "can": ["everything operator can", "manage.resources", "manage.secrets",
                 "manage.users", "manage.channels", "install.mcp", "test.resources"],
         "cannot": []},
    ]


def set_enabled(db: Db, user_id: str, enabled: bool) -> bool:
    cur = db.write("UPDATE users SET enabled=? WHERE id=?", (1 if enabled else 0, user_id))
    return cur.rowcount == 1
