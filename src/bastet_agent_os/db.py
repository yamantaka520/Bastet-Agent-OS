"""SQLite data layer for Bastet (SPEC §3).

Single-writer discipline: the control plane and gateway share one process; all
writes go through Db.write() which serializes on a lock and keeps transactions
short. Reads use the same connection (WAL makes readers cheap).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# All AMOS-referencing ids are TEXT (AMOS id type). See SPEC §3.1.
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,             -- = AMOS project id (D7, 1:1)
  team_id TEXT NOT NULL,           -- = AMOS team id
  repo_path TEXT,
  default_template_id TEXT,
  config_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  amos_agent_id TEXT NOT NULL,
  name TEXT NOT NULL,
  executor_type TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  config_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_agent_roles (
  project_id TEXT NOT NULL REFERENCES projects(id),
  agent_id TEXT NOT NULL REFERENCES agents(id),
  role TEXT NOT NULL,
  preference INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (project_id, agent_id, role)
);

CREATE TABLE IF NOT EXISTS resources (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,              -- llm|mcp|image|video|music|tts|stt|skill|secret
  name TEXT NOT NULL UNIQUE,
  endpoint TEXT,
  api_flavor TEXT,                 -- openai|anthropic
  secret_ref TEXT,
  routing_json TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1,
  config_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grants (
  id TEXT PRIMARY KEY,
  resource_id TEXT NOT NULL REFERENCES resources(id),
  scope_type TEXT NOT NULL,        -- team|project|agent
  scope_id TEXT NOT NULL,
  budget_tokens INTEGER,
  budget_usd REAL,
  period TEXT NOT NULL DEFAULT 'lifetime',  -- lifetime|daily|monthly
  max_concurrency INTEGER,
  priority INTEGER NOT NULL DEFAULT 0,
  on_exceed TEXT NOT NULL DEFAULT 'block',  -- block|queue|degrade
  enabled INTEGER NOT NULL DEFAULT 1,
  expires_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_templates (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  stages_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  template_id TEXT,
  stages_snapshot_json TEXT NOT NULL,
  title TEXT NOT NULL,
  spec_md TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',   -- open|in_progress|blocked|done|cancelled
  priority INTEGER NOT NULL DEFAULT 0,
  parent_job_id TEXT,
  worktree_path TEXT,
  version INTEGER NOT NULL DEFAULT 0,    -- optimistic lock (CAS updates)
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_deps (
  job_id TEXT NOT NULL REFERENCES jobs(id),
  depends_on_job_id TEXT NOT NULL REFERENCES jobs(id),
  effect TEXT NOT NULL DEFAULT 'block',  -- block|context
  PRIMARY KEY (job_id, depends_on_job_id)
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id),
  stage TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  agent_id TEXT NOT NULL REFERENCES agents(id),
  executor_type TEXT NOT NULL,     -- deliberate denormalization: snapshot at run time
  resource_id TEXT REFERENCES resources(id),  -- LLM resource assigned at dispatch
  workdir TEXT,
  isolation TEXT NOT NULL DEFAULT 'worktree',
  status TEXT NOT NULL DEFAULT 'queued',
  -- queued|running|waiting_input|succeeded|failed|cancelled|timeout|orphaned
  error TEXT,
  executor_handle_json TEXT,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0,
  cache_write INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  accounting_precision TEXT,       -- gateway|reported|estimated
  version INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  finished_at TEXT,
  artifacts_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS run_tokens (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  token_hash TEXT NOT NULL UNIQUE, -- sha256; plaintext exists only at issue time
  expires_at TEXT NOT NULL,
  revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS usage_ledger (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  resource_id TEXT NOT NULL REFERENCES resources(id),
  model TEXT,
  provider_request_id TEXT,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0,
  cache_write INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_results (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  gate_type TEXT NOT NULL,
  verdict TEXT NOT NULL,
  reviewer_kind TEXT NOT NULL,     -- agent|user
  reviewer_id TEXT NOT NULL,
  detail_md TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',  -- MUST NOT contain secret values / auth headers
  prev_hash TEXT NOT NULL,
  row_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  config_json TEXT NOT NULL DEFAULT '{}',
  secret_ref TEXT,
  enabled INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id);
CREATE INDEX IF NOT EXISTS idx_ledger_run ON usage_ledger(run_id);
CREATE INDEX IF NOT EXISTS idx_ledger_at ON usage_ledger(at);
CREATE INDEX IF NOT EXISTS idx_grants_resource ON grants(resource_id);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Db:
    """Thread-safe wrapper enforcing single-writer short transactions."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        cur = self._conn
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def write_many(self, statements: list[tuple[str, tuple]]) -> None:
        """Execute several statements in ONE short transaction."""
        with self._lock, self._conn:
            for sql, params in statements:
                self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- optimistic locking (SPEC §3.2) ------------------------------------

    def cas_update(self, table: str, row_id: str, version: int, fields: dict[str, Any]) -> bool:
        """Compare-and-swap update; returns False if someone else won the race."""
        if table == "jobs":
            fields = {**fields, "updated_at": now()}
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE {table} SET {sets}, version=version+1 WHERE id=? AND version=?",
                (*fields.values(), row_id, version),
            )
            return cur.rowcount == 1

    # -- audit hash chain (SPEC §3.1 / §5.9) --------------------------------

    def audit(self, actor: str, action: str, target_type: str, target_id: str,
              detail: dict | None = None) -> None:
        detail_json = json.dumps(detail or {}, ensure_ascii=False)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = row["row_hash"] if row else "genesis"
            at = now()
            payload = f"{prev_hash}|{at}|{actor}|{action}|{target_type}|{target_id}|{detail_json}"
            row_hash = hashlib.sha256(payload.encode()).hexdigest()
            self._conn.execute(
                "INSERT INTO audit_log(at, actor, action, target_type, target_id, "
                "detail_json, prev_hash, row_hash) VALUES(?,?,?,?,?,?,?,?)",
                (at, actor, action, target_type, target_id, detail_json, prev_hash, row_hash),
            )

    def verify_audit_chain(self) -> bool:
        prev = "genesis"
        for r in self.query("SELECT * FROM audit_log ORDER BY id"):
            payload = (f"{prev}|{r['at']}|{r['actor']}|{r['action']}|{r['target_type']}|"
                       f"{r['target_id']}|{r['detail_json']}")
            if hashlib.sha256(payload.encode()).hexdigest() != r["row_hash"]:
                return False
            prev = r["row_hash"]
        return True

    def close(self) -> None:
        self._conn.close()
