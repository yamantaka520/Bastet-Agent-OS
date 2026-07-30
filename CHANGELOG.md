# Changelog

All notable changes to Bastet Agent OS. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

Every user-visible change bumps `__version__` in
`src/bastet_agent_os/__init__.py` and adds a section here; `web/package.json`
follows the same number and the WebUI prints it beside the title.
`tests/test_version.py` fails the build if the three drift apart.

## [0.6.1] - 2026-07-31

### Fixed
- Saved credentials are editable: `PUT /api/secrets/{id}` changes the name,
  injected env var, note and visibility scope, and rotates the value itself
  (blank keeps the stored one; the old file is left in place rather than
  deleting a key that might still be needed). The Admin card grows an inline
  editor — before this, a typo meant delete and retype.
- Resources whose credential is a `secret:<id>` pointer pick the rotation up
  on the next run, with no per-resource edits.
- The project resource picker no longer says "no other agent available" when
  every pool resource is already attached.

## [0.6.0] - 2026-07-31

### Added — classified resource pool, MCP installs, agent-callable resources
- Resource kinds catalog (`resource_kinds.py`): `llm / mcp / api / skill / git`
  plus media, grouped model / tool / asset / media. Each kind declares which
  fields the UI shows and whether a credential is required (skills need none),
  and the same table validates the API — a new kind needs no new endpoint.
- Visibility scope sits with the resource: create it global / team / project,
  add or drop scopes later (`POST|DELETE /api/resources/{id}/scopes`).
- The API-key field is a picker over saved credentials: a resource stores
  `secret:<id>` pointing into the pool, so rotating a credential updates every
  resource that uses it. Raw pastes still get filed at 0600 as before.
- MCP install flow: keep the vendor's install command with the resource and run
  it from the WebUI (`POST /api/resources/{id}/install`) — admin only, audited,
  never implicit. Full stdout/stderr and exit code come back and stay on the
  row, so a failed install can be debugged, the command edited, and retried.
- Resources are now callable by the agents running a project: at run start
  every grant covering the project turns into env vars
  (`BASTET_RES_<NAME>_URL/_KEY/_TOKEN/_MODEL/_SOURCE`), an `mcpServers` config
  file (`BASTET_MCP_CONFIG`, plus `--mcp-config` for claude-code), and a
  manifest listed in the task prompt. The MCP file holds resolved credentials,
  so it lives outside the worktree at 0600 and is deleted when the run ends.
  Resources with no usable channel are not advertised at all.
- Project tab: add/remove resources directly (project-scoped grants);
  team/global access is shown as inherited and must be changed where granted.

### Fixed
- An agent bound to an executor account no longer loses the project's injected
  credentials: account env is merged into the run env instead of replacing it.

## [0.5.0] - 2026-07-30

### Added — multi-language UI (zh-Hant / zh-Hans / en / ja / ko)
- `web/src/i18n/`: every visible string goes through `t()`. `zh-Hant.ts` is the
  canonical dictionary and the other four locales are typed against it, so a
  missing translation is a compile error rather than a Chinese string leaking
  into an English page. New UI work must add keys there.
- Locale picked from `localStorage` → `navigator.languages` (zh-TW/HK/MO →
  traditional, other zh → simplified), switchable in the header and remembered
  per browser; `<html lang>` follows it.
- Workflow vocabulary (14 roles, 4 gate types) is localised by its stable id,
  so a stage that stores `role: "reviewer"` renders as 審查者 / 审查者 /
  Reviewer / レビュアー / 검토자. Built-in preset stage text stays as authored:
  the preset name becomes the template id on copy, so translating the display
  would desync it from what is saved.
- Version badge next to the title, from the new unauthenticated
  `GET /api/version`; `pyproject.toml` now takes its version from
  `__init__.py` (single source of truth).

## [0.4.0] - 2026-07-29

### Added — one-click install, executor accounts, memory view
- `install.sh`: one-click installer (macOS/Linux) — Bastet + latest Agent
  Memory OS + claude-agent-sdk into ~/.bastet/venv, plus the executor CLIs
  via their OFFICIAL installers (Claude Code, Codex, Grok Build, Antigravity,
  Hermes); idempotent, `--minimal` / `--executors` / `--upgrade` flags,
  ends with `bastet doctor` and per-tool login guidance.
- Executor accounts: multiple logins per executor via isolated profile
  dirs (~/.bastet/executor-profiles/<id>) exported as CLAUDE_CONFIG_DIR /
  CODEX_HOME / GROK_HOME per run; `/api/executors` catalog (installed /
  supports_accounts), `/api/executor-accounts` CRUD returning the exact
  interactive login command; agents bind an account_id. agy is global-login
  only (upstream limitation), bastet-lite needs none.
- Org page: executor dropdown (with 未安裝 markers), account picker, inline
  account creation with login instructions and profile status.
- 記憶 tab: AMOS search view backed by `/api/memory/search` (ACL-filtered).
- Fixes: uvicorn[standard] (WebSocket 403 root cause), httpx logger forced
  to WARNING (bot tokens live in Telegram URLs), WS rejection reasons logged.

### Added — grok & agy executors
- `grok` executor (xAI Grok Build CLI): headless `-p` with streaming-json
  events; review runs get a REAL read-only toolset (`--tools
  read_file,grep,list_dir`) plus a schema-enforced verdict (`--json-schema`);
  gateway path via `GROK_MODELS_BASE_URL` + run token in `XAI_API_KEY`
  (the gateway now serves `/v1/models` for its startup probe). Headless
  output has no usage — gateway metering or honest `estimated`.
- `agy` executor (Google Antigravity CLI): `-p --output-format json`
  envelope carries full usage (input/output/thinking/cache tokens →
  `reported` precision); review runs rely on headless soft-denial (de-facto
  read-only) plus `--json-schema` verdicts; no custom endpoint exists
  upstream, so the gateway path is refused honestly.

### Added — M5
- Federation org view (`GET /api/org`): the AMOS-converged teams / projects /
  members tree merged with local binding state; `POST /api/org/bind` attaches
  a project synced from another node to a local repo. Org page grows a
  Federation section (🔗 bound / ◌ unbound, inline bind). Local-only
  projects (AMOS record gone) stay listed with their history. See
  docs/FEDERATION.md for what converges (org + memory, via AMOS) and what
  stays per-node (resources, grants, jobs).

### Added — WebUI management console
- Tabbed, role-aware SPA: board with dispatch modal + in-run Allow/Deny +
  inline diff; resources/grants with budget burn bars; org & templates
  management; admin page (users with show-once tokens, Telegram pairing);
  audit view; WS auto-reconnect.

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

### Added — M4
- Media resource governance: gateway endpoints for `/v1/images/generations`,
  `/v1/audio/speech`, `/v1/audio/transcriptions` with per-request resource
  selection (`X-Bastet-Resource`), grants enforced, flat per-call cost
  (`config_json.cost_per_call`) in the ledger; bastet-lite gains
  `generate_image` / `text_to_speech` tools (workdir-jailed output).
- In-run interactions: `interaction_request` events park the run in
  `waiting_input` (persisted in `run_interactions`), answered via
  `POST /api/runs/{id}/respond`, the UI-facing events stream, or Telegram
  Allow/Deny buttons; replies attributed to the acting user.
- `claude-sdk` executor: Claude Code via the Agent SDK — `can_use_tool`
  pauses on permission requests and resumes on the human's allow/deny;
  unattended fallback denies after a timeout. (`pip install
  bastet-agent-os[sdk]`; API-key/gateway path only — the SDK does not do
  Max-subscription auth.)
- `codex` executor: `codex exec --json` JSONL driver with cached/reasoning
  token splits; review runs get a schema-enforced JSON verdict via
  `--output-schema` (read-only sandbox can't write the verdict file).
  Direct path only until the gateway learns the Responses API.
- `hermes` executor: oneshot (`hermes -z`) driver with a Bastet-managed
  HERMES_HOME profile routing inference through the gateway (run token via
  env, never argv/disk); read-only review runs refused honestly (oneshot is
  hard-wired YOLO upstream).
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
