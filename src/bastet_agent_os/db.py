"""SQLite data layer for Bastet (SPEC §3).

Single-connection discipline: the control plane and gateway share one process;
every operation on the process-wide SQLite connection is serialized.  WAL
still protects interoperability with backup/diagnostic connections, but a
single sqlite3.Connection must not be used concurrently by worker threads.
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
  -- lifecycle (SPEC §5.12): planning -> ready -> running <-> paused
  --                         -> maintenance -> closed (reopenable)
  status TEXT NOT NULL DEFAULT 'planning',
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
  archived INTEGER NOT NULL DEFAULT 0,   -- hidden from the board, history kept
  spec_md TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',   -- open|in_progress|blocked|done|cancelled
  priority INTEGER NOT NULL DEFAULT 0,
  parent_job_id TEXT,
  default_agent_id TEXT,           -- fallback executor agent for stages without a role match
  resource_id TEXT,                -- LLM resource used by this job's runs (NULL = direct path)
  worktree_path TEXT,
  version INTEGER NOT NULL DEFAULT 0,    -- optimistic lock (CAS updates)
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- data handed to a job while it runs: deploy targets, endpoints, decisions the
-- agent could not know. Injected into every later run's brief and dropped into
-- the live worktree's inbox. Secrets do NOT belong here (they would travel in a
-- prompt); the credentials card + resource grants exist for those.
CREATE TABLE IF NOT EXISTS job_supplies (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id),
  name TEXT NOT NULL,
  content TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
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
  config_error INTEGER NOT NULL DEFAULT 0,  -- the gate could not run at all
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

CREATE TABLE IF NOT EXISTS role_prompts (
  role TEXT PRIMARY KEY,           -- matches workflow stage roles
  label TEXT NOT NULL,
  prompt TEXT NOT NULL,            -- prepended to a stage run's task context
  builtin INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS executor_accounts (
  id TEXT PRIMARY KEY,
  executor_type TEXT NOT NULL,
  name TEXT NOT NULL,
  home_dir TEXT NOT NULL,          -- exported as the CLI's home/config env var per run
  config_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_interactions (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  request_id TEXT NOT NULL,
  kind TEXT,                       -- permission_request|plan_approval|question
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|answered|expired
  reply_json TEXT,
  created_at TEXT NOT NULL,
  answered_at TEXT
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL DEFAULT 'operator',  -- viewer|operator|admin (D9, M3)
  token_hash TEXT NOT NULL UNIQUE,        -- sha256; plaintext shown once at creation
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS channels (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT,
  config_json TEXT NOT NULL DEFAULT '{}',
  secret_ref TEXT,
  enabled INTEGER NOT NULL DEFAULT 1
);

-- Chat: the human input channel per project (SPEC §5.11). Sessions belong to
-- a real project (or a team/global scope) so the discussion stays consistent
-- with the org the runs execute against.
CREATE TABLE IF NOT EXISTS chat_sessions (
  id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL,        -- global|team|project
  scope_id TEXT NOT NULL,          -- '*' for global
  title TEXT NOT NULL DEFAULT '',
  responder_kind TEXT NOT NULL,    -- agent|resource
  responder_id TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'web',   -- web|telegram
  config_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES chat_sessions(id),
  role TEXT NOT NULL,              -- user|assistant|system
  author TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  attachments_json TEXT NOT NULL DEFAULT '[]',
  meta_json TEXT NOT NULL DEFAULT '{}',   -- model, usage, cost, job_id …
  at TEXT NOT NULL
);

-- One durable internal room per project.  This is agent-to-agent operational
-- communication, distinct from chat_sessions (the human input channel).
CREATE TABLE IF NOT EXISTS project_rooms (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL UNIQUE REFERENCES projects(id),
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS room_messages (
  id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES project_rooms(id),
  author_type TEXT NOT NULL,       -- agent|pm|user|system
  author_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'message', -- message|assignment|handoff|test_evidence
  content TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}',
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_room_messages ON room_messages(room_id, at);

-- The contract between consecutive stages.  A summary alone is insufficient:
-- changed paths and verification are what let the next agent select context
-- and what let the test engine invalidate stale evidence safely.
CREATE TABLE IF NOT EXISTS stage_handoffs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  job_id TEXT NOT NULL REFERENCES jobs(id),
  run_id TEXT NOT NULL REFERENCES runs(id),
  from_stage TEXT NOT NULL,
  to_stage TEXT,
  agent_id TEXT NOT NULL REFERENCES agents(id),
  summary TEXT NOT NULL DEFAULT '',
  changed_paths_json TEXT NOT NULL DEFAULT '[]',
  verification_json TEXT NOT NULL DEFAULT '[]',
  risks_json TEXT NOT NULL DEFAULT '[]',
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_handoffs_job ON stage_handoffs(job_id, at);

-- A durable dispatch fence for safe upgrades.  The row exists even while the
-- fence is open so status checks never depend on process-local state.
CREATE TABLE IF NOT EXISTS maintenance_lock (
  id INTEGER PRIMARY KEY CHECK(id=1),
  enabled INTEGER NOT NULL DEFAULT 0,
  generation INTEGER NOT NULL DEFAULT 0,
  owner TEXT,
  reason TEXT,
  entered_at TEXT,
  released_at TEXT
);
INSERT OR IGNORE INTO maintenance_lock(id, enabled, generation)
VALUES(1, 0, 0);

-- One row per independently declared test case.  Evidence is reusable only
-- while its covered paths and command inputs remain unchanged.
CREATE TABLE IF NOT EXISTS test_evidence (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  job_id TEXT NOT NULL REFERENCES jobs(id),
  run_id TEXT NOT NULL REFERENCES runs(id),
  stage TEXT NOT NULL,
  case_id TEXT NOT NULL,
  command TEXT NOT NULL,
  verdict TEXT NOT NULL,
  base_commit TEXT,
  covered_paths_json TEXT NOT NULL DEFAULT '[]',
  output_tail TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_test_evidence_job_case
  ON test_evidence(job_id, stage, case_id, at);

CREATE TABLE IF NOT EXISTS context_evaluations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  job_id TEXT NOT NULL REFERENCES jobs(id),
  stage TEXT NOT NULL,
  role TEXT,
  expected_buckets_json TEXT NOT NULL DEFAULT '[]',
  expected_terms_json TEXT NOT NULL DEFAULT '[]',
  forbidden_terms_json TEXT NOT NULL DEFAULT '[]',
  report_json TEXT NOT NULL,
  bucket_recall REAL NOT NULL,
  term_recall REAL NOT NULL,
  noise_count INTEGER NOT NULL,
  passed INTEGER NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_evaluations_job
  ON context_evaluations(job_id, at);
CREATE INDEX IF NOT EXISTS idx_chat_msg_session ON chat_messages(session_id, at);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_scope
  ON chat_sessions(scope_type, scope_id);

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
        # FULL costs an extra fsync but guarantees that a committed state
        # transition survives power loss.  WAL+NORMAL preserves consistency,
        # yet SQLite explicitly allows the newest commits to disappear after a
        # power failure — unacceptable for job/run/handoff state.
        cur.execute("PRAGMA synchronous=FULL")
        cur.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            # additive migrations for pre-release DBs (CREATE IF NOT EXISTS
            # doesn't add columns to existing tables)
            existing = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)")}
            for col, decl in [("default_agent_id", "TEXT"), ("resource_id", "TEXT")]:
                if col not in existing:
                    self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
            agent_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(agents)")}
            if "account_id" not in agent_cols:
                self._conn.execute("ALTER TABLE agents ADD COLUMN account_id TEXT")
            if "depleted_at" not in agent_cols:
                # when this agent's paid balance ran out. Set from a vendor's
                # own 402, cleared only by a human (topping up is not something
                # the engine can do). Dispatch skips depleted agents — routing
                # work to an agent that cannot run it produced an infinite
                # rework loop, one instant 402 per cycle.
                self._conn.execute("ALTER TABLE agents ADD COLUMN depleted_at TEXT")
                self._conn.execute("ALTER TABLE agents ADD COLUMN depleted_reason TEXT")
            gate_cols = {r[1] for r in
                         self._conn.execute("PRAGMA table_info(gate_results)")}
            if "config_error" not in gate_cols:
                self._conn.execute("ALTER TABLE gate_results ADD COLUMN config_error "
                                   "INTEGER NOT NULL DEFAULT 0")
            # the rework loop's state: how many times this card has been sent
            # back, and the failure the receiving agent must fix
            if "rework_count" not in existing:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN rework_count "
                                   "INTEGER NOT NULL DEFAULT 0")
            if "rework_note" not in existing:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN rework_note TEXT")
            # when a quota-blocked job should retry itself (UTC ISO)
            if "resume_at" not in existing:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN resume_at TEXT")
            # one-shot agent override set by retry; cleared on stage transition
            if "agent_override" not in existing:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN agent_override TEXT")
            if "archived" not in existing:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN archived INTEGER "
                                   "NOT NULL DEFAULT 0")
            project_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(projects)")}
            if "status" not in project_cols:
                self._conn.execute("ALTER TABLE projects ADD COLUMN status TEXT "
                                   "NOT NULL DEFAULT 'planning'")
            run_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(runs)")}
            # liveness: what the run last said, and when — the difference between
            # "working" and "stuck" on the board
            if "heartbeat_at" not in run_cols:
                self._conn.execute("ALTER TABLE runs ADD COLUMN heartbeat_at TEXT")
            if "progress_text" not in run_cols:
                self._conn.execute("ALTER TABLE runs ADD COLUMN progress_text TEXT")
            if "progress_at" not in run_cols:
                # when the run last SAID something, as opposed to heartbeat_at =
                # when it was last confirmed alive. Conflating the two hid a real
                # incident: a stage sat alive-but-silent for 52 minutes (blocked
                # on an interactive prompt inside a child process) and the board
                # had no way to show the difference.
                self._conn.execute("ALTER TABLE runs ADD COLUMN progress_at TEXT")
            channel_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(channels)")}
            if "name" not in channel_cols:
                self._conn.execute("ALTER TABLE channels ADD COLUMN name TEXT")
            handoff_cols = {r[1] for r in
                            self._conn.execute("PRAGMA table_info(stage_handoffs)")}
            for col, decl in [
                ("delivered_at", "TEXT"),
                ("delivered_to_agent_id", "TEXT"),
                ("acknowledged_at", "TEXT"),
                ("acknowledged_by", "TEXT"),
                ("acknowledgement", "TEXT"),
                ("questions_json", "TEXT NOT NULL DEFAULT '[]'"),
            ]:
                if col not in handoff_cols:
                    self._conn.execute(
                        f"ALTER TABLE stage_handoffs ADD COLUMN {col} {decl}")
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
        # FastAPI sync handlers and the orchestrator run in different worker
        # threads.  check_same_thread=False permits that ownership model; it
        # does *not* make overlapping calls on one sqlite3.Connection safe.
        # Without this lock, UI polling could corrupt cursor/row state and
        # intermittently raise InterfaceError, return no project, or even
        # expose a row with the wrong shape while a job transition was being
        # committed.
        with self._lock:
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
