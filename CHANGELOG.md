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

### Added — M2 workflow engine (in progress)
- Multi-stage workflow templates (YAML/JSON) with per-stage roles, isolation,
  retries.
- Gate protocol: `auto`, `tests-pass` (deterministic command), `agent-review`
  (structured verdict channel — missing verdict rejects), `human-approve`.
