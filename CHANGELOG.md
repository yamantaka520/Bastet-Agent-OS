# Changelog

All notable changes to Bastet Agent OS. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

## [Unreleased]

### Added — M1 core skeleton
- SPEC v1.1: design finalized after a three-perspective review (architecture,
  data model, security); all high findings folded in (design log D12).
- SQLite data layer: full SPEC §3.1 schema, WAL + single-writer discipline,
  append-only audit log with hash chain, CAS optimistic locking.
- LLM gateway: authenticated pass-through proxy (OpenAI + Anthropic flavors),
  short-lived run tokens (hash-only at rest, revoked on terminal run state),
  per-request usage ledger with cache tokens priced separately, two-phase
  quota enforcement.
- Governance: resources & grants with agent > project > team resolution,
  budget periods, per-grant concurrency FIFO.
- `claude-code` executor (headless stream-json), with honest failure mapping
  (`is_error` overrides the misleading `success` subtype).
- Orchestrator: built-in single-stage template, job-owned git worktrees,
  diff artifacts, orphaned-run recovery at startup.
- Control plane API: 127.0.0.1-only, Host/Origin validation (DNS-rebinding
  defence), Bearer-token auth, AMOS org binding (1:1 project mapping).
- `bastet` CLI: init/serve/project/agent/resource/grant/dispatch/run/runs/
  usage/audit/pricing-update/doctor.
- CI: ubuntu/macos/windows × Python 3.11/3.12.

### Added — M2 workflow engine
- Multi-stage workflow templates (YAML/JSON) with per-stage roles, isolation,
  retries.
- Gate protocol: `auto`, `tests-pass` (deterministic command), `agent-review`
  (structured verdict channel — missing verdict rejects), `human-approve`.
- Role-based agent routing (`project_agent_roles`), human approval flow
  (`bastet approve` / `POST /api/jobs/{id}/approve`).

### Added — M4 (in progress)
- Telegram channel (SPEC §5.7): long polling only (no public webhook),
  numeric-user-id allowlist bound to Bastet users via one-time pairing codes
  (`bastet channel pair` → `/pair <code>`), group messages ignored, `/status`
  `/jobs` `/approve`, inline Approve/Reject buttons referencing the concrete
  job id, gate.pending / job.done / budget notifications pushed from the
  event bus; approvals attributed to the bound user. Channels API/CLI
  (admin-only), started via app lifespan.

### Added — M3
- Multi-user auth (D13): per-user tokens (hash-only at rest, shown once),
  roles viewer < operator < admin, bootstrap file token stays the implicit
  admin; audit and approvals attribute the acting user; `bastet user
  add/list/disable/enable`, `bastet whoami`, `/api/me`.
- `bastet-lite` built-in executor: gateway-only tool loop (anthropic + openai
  flavors) with workdir-jailed file tools, allow-listed shell, AMOS memory
  tools, and a native `submit_verdict` tool — the structured gate verdict
  without the file side-channel. In-loop transcript budget elides old tool
  output instead of overflowing.
- Dynamic context engine (SPEC §5.6 outer allocator): budgeted buckets for
  job spec, pipeline history, dependency conclusions, and the AMOS context
  pack; every include/exclude decision is audited (`context.assembled`).
- Queue policy: `on_exceed: queue` now waits for budget/concurrency instead
  of failing; `block` keeps failing fast.
- Container isolation plumbing (SPEC §5.4.3): docker-run wrapper with
  read-only main-.git mount, non-root user, no-new-privileges, host-gateway
  alias; missing Docker fails loudly, never a silent downgrade. `bastet
  doctor` reports daemon availability.

### Added — M2 UI & events
- Typed event bus (SPEC §5.10) + `/api/ws` WebSocket stream (token in the
  first message — never in the URL; Host/Origin validated like HTTP).
- Kanban web UI (Vite + React, served at `/ui`, built assets ship in the
  package): stage columns, live board refresh over WS, job drawer with runs,
  usage/cost per run, gate history, and in-browser approve/reject for
  human-approve gates.
