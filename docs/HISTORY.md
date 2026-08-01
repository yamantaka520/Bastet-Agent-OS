# Bastet Agent OS — Project History and Design Log

Last updated: 2026-08-01

## Purpose

This document records the journey: what was built in what order, what broke, and
why each decision went the way it did. [CHANGELOG.md](../CHANGELOG.md) says what
changed in each version; this says *why*, and keeps the mistakes visible.

Core engineering principle:

> The control plane must never claim more than it verified.

Every feature here is shaped by that. A version it cannot determine is
`unknown`, not "current". An installer that changed nothing says `unchanged`, not
"updated". A gate command that could not run is a configuration problem, not a
failing test. Accounting is never quietly reduced. The reason this matters more
than usual: the thing being reported on is a fleet of autonomous agents, and a
reassuring lie about them is worse than no report at all.

## Canonical repository

- Working tree: `~/Documents/GitHub/bastet-agent-os`
- GitHub remote: `git@github.com:yamantaka520/Bastet-Agent-OS.git`
- Validation host: a second machine running the systemd user service, where
  every feature is confirmed against real agents (see [PROGRESS.md](../PROGRESS.md))

## Timeline

### 2026-07-28 — M0 to M3: the skeleton that holds

The first day laid down the parts that everything later depends on:

- **M0/M1**: SPEC v1.1, the SQLite data model, the control plane, the
  OpenAI/Anthropic-compatible gateway, the `claude-code` executor, CLI dispatch.
- **M2**: the workflow engine — multi-stage templates with four gate types
  (`auto`, `tests-pass`, `agent-review`, `human-approve`) — plus the event bus,
  WebSocket stream and Kanban UI.
- **M3**: `bastet-lite` (Bastet's own minimal executor), the dynamic context
  engine, queueing, container isolation, and multi-user auth with three roles.

**Decision — a control plane, not a framework.** Execution comes from
orchestrating agents that already exist. Writing another agent loop would have
been competing on the one axis where the vendors are strongest and the moat is
weakest.

**Decision — the gate verdict travels in a file, not in prose.** An
`agent-review` stage must write `{"verdict": "approve"}` to
`._bastet/verdict.json`. Review prose never decides, a missing verdict is a
rejection, and the engine deletes the file before the run so a stale verdict
cannot leak in. Letting an LLM's free text decide a gate means the gate is
whatever the model felt like saying.

### 2026-07-29 — M4, M5, and the login wizard grind

- **M4**: the Telegram channel (pairing, inline gate approval, notifications),
  media governance, in-run interactions, and the `claude-sdk` / `codex` /
  `hermes` executors.
- **M5**: federation — a shared org view over AMOS sync.
- Then `grok` and `agy` executors, the one-click installer, executor accounts,
  the AMOS memory tab, LAN mode, and the service installer for all three OSes.

**Fifteen commits went into one feature: the login wizard.** Interactive OAuth
logins for five vendor CLIs cannot be done for the user (a password or an OAuth
browser flow is theirs), so the wizard gives them a real terminal in the
browser. Getting there took: PTY window sizing, `TERM=dumb` for `agy`,
xterm.js, a zero-height container that collapsed `fit()`, swallowed handler
errors that hid the real failure, alt-screen switches, web-hostile escape
sequences, and finally explicit key buttons because some CLIs need arrow keys
that the browser eats. The lesson kept: **when a vendor tool is interactive,
give the human a real terminal rather than trying to script the interaction.**

**Bug worth remembering — WebSocket routes went blank.** `from fastapi import
WebSocket` inside a function body silently broke *every* WS route. Module-level
imports for anything FastAPI introspects.

**Bug worth remembering — services with a minimal PATH.** Under systemd the
executors were invisible and unspawnable: a login shell's PATH is not the
service's PATH. Bastet now reconstructs what it needs and appends its own venv
**last**, so a project that provides its own runner wins.

### 2026-07-30 — roles, prompts, and the Project tab

Role assignment per project, role definition prompts (what a role *means* when an
agent plays it), and the Project tab. Role assignment was then made to grant real
**AMOS project membership** — assigning a role without it left the agent outside
the project's memory scope, so it "had" a role and could not see the project's
memory.

Then the five-language WebUI and the version badge, with the canonical dictionary
typed so a missing translation fails `npm run build` and a test that fails on
hard-coded CJK that skipped `t()`. That guard has since caught three real
regressions, twice in my own comments.

### 2026-07-31 — the long day: pool, chat, lifecycle, and the loop

This is where the product became usable, driven by a real project (CatsWalker)
finding every gap:

**The classified resource pool** (kinds, per-resource visibility, credential
picker, MCP install-and-debug, run-time access) and a **test button per
resource**. Then the bugs that only a real user finds:

- The gateway ignored `secret:<id>` pointers, so every resource created through
  the new picker would have 502'd.
- `RunResult` has no `.error`, so the failure-*reporting* path crashed — the code
  that was supposed to explain a failure became the failure.
- Executors truncated `summary` at 2000 chars, which silently broke the first
  real PM decomposition, because for chat and decomposition **the summary is the
  payload**. Now 200 000.
- `~` was never expanded in `repo_path`, and an unexpanded path had created a
  literal `/home/yujin/~/...` directory.
- grok pretty-prints its JSON, so line-by-line parsing found nothing; then the
  *inner* verdict parse needed the same tolerance because it emitted two objects
  back to back.
- A single-line credential input destroyed PEM keys (`error in libcrypto`, 398
  bytes, zero newlines). Fixed with textareas plus a repair that re-wraps a
  one-line paste using the BEGIN/END markers — a repair, not a guess.

**Project lifecycle** as a real state machine with a light, PM decomposition
gated behind human confirmation, and run/pause/stop on the card. Two corrections
came from the user here, both about honesty:

1. A stale decomposition was being shown as "the plan". Fixed with provenance
   (which conversation it came from), staleness detection, and preservation of
   rows already dispatched.
2. The project card said 規劃中 while a task was running. The light now derives
   from a reconciliation that runs at startup and on read.

**The runner died on restart** — a job was left mid-plan with the project parked.
Fixed with `ensure_running` (idempotent resume), `watch(bus)` and a `reconcile`
at startup. **The Telegram notify loop died silently** on one send error while
the channel still reported `polling`, so an approval request reached nobody and
the workflow waited for a human who was never told.

Then audit search, memory browse, and the maintenance card — and, writing tests
for the last one, two silent failures in the memory path:

- `chat.remember()` passed `project_id=` to `MemoryClient.add`, which does not
  accept it. The `TypeError` was caught and logged at info, so **every planning
  conversation reported itself remembered and none of it was.**
- Both memory endpoints read AMOS records with `.get`, but `search` returns
  `SearchResult` and `list_recent` returns `MemoryRecord`. That only raises where
  AMOS is actually installed — so it passed tests and 500'd in production.

### 2026-07-31 (later) — the loop that made it a product

The user's framing was the turning point:

> 任務卡這個開發過程不是應該要自己去解決所有的情況，不應該是碰到錯誤不處理丟個錯誤
> 訊息然後任務就停下來了。畢竟程式也是 AI Agent 在寫，應該是有能力自行處理的。

Correct, and it reframed the engine. A failing gate now hands the card **back**
to a stage that can fix it, with the gate's real output attached, and the
pipeline continues. Read-only reviewers are skipped over, because a reviewer
cannot fix what it just rejected.

**Decision — the rework brief must name the shortcuts.** The cheapest way to make
a gate pass is to weaken the gate: delete the test, assert `True`, add
`skip`, edit the test command. An agent told only "make it green" will do exactly
that, so the brief forbids each one explicitly and the loop is capped at three
cycles.

**Decision — an unrunnable command is handed back too.** `npm ERR! Missing
script: "test:e2e"` used to stop the card dead as a "configuration problem". It
*is* one, but the agent that writes the project can close it — with instructions
to add the real script or dependency, and to report a genuinely wrong command
rather than fake a green exit.

**Then validation found the thing that made all of it pointless.** The loop ran
on the host, a real Claude Code agent changed `a - b` to `a + b`, the gate went
green, the job finished — and cleanup deleted the fix. `git worktree remove
--force` discards uncommitted changes, no stage ever commits, and the
`bastet/<job>` branch still pointed at the job's starting commit. The work
survived only as a diff file. Cleanup now commits to that branch first, which is
what its own docstring had been claiming.

**AMOS was wired to exactly one executor.** Only `bastet-lite` wrote memories, so
a project driven by Claude Code or Codex contributed nothing and every context
pack read from an empty store — the real reason the memory tab was blank. Writing
moved into the orchestrator, so every executor contributes, and context packs are
read *as the running agent*, which turns on the ACL that had been keeping nothing
apart.

## Design decisions, in one place

| Decision | Why |
|---|---|
| Control plane, not another agent framework | execution is where vendors are strongest; governance and memory are where the value is |
| Executor as a plugin interface | new vendor CLIs arrive constantly; adding one must not touch the engine |
| Gate verdicts in a file, never in prose | otherwise the gate is whatever the model felt like saying |
| A failed gate reworks instead of blocking | a failing test is an ordinary event in a development loop |
| The rework brief forbids weakening the gate | the cheapest way to pass a gate is to break it |
| Rework is capped (3) | "self-healing" must not mean "burns tokens forever" |
| Side effects stay `human-approve` | deploy/release/merge are not decisions to delegate to a loop |
| Work is committed to `bastet/<job>` | otherwise the loop's output is discarded on cleanup |
| Never write to the project's own branch | merging is a deliberate act |
| Credentials referenced (`secret:<id>`), never copied | rotating a key must update every user of it |
| Secret values are write-only | the UI can rename and rescope a credential but never show it |
| The gate command runs on the Bastet host, venv last on PATH | a project that provides its own runner should win |
| Usage rows cannot be silently deleted | the accounting is the product |
| Memory writes can never break a run | enforced at the module boundary, not per call site |
| Context packs are read as the running agent | an unscoped pack recalls every project into every run |
| One canonical i18n dictionary, others typed against it | a missing translation should fail the build, not ship blank |
| A single `__version__`, four places checked by a test | the UI must show the version actually running |

## Mistakes kept on the record

Listed because each one changed how the code is written now.

| What happened | What it changed |
|---|---|
| Memory writes failed silently for a month of conversations | failures log at warning; the guarantee moved to the module boundary |
| `.get` on AMOS dataclasses passed tests, 500'd in production | tests for AMOS integration use real AMOS, never a stub |
| Cleanup deleted the loop's output | integration verification on a real host, not just unit tests |
| The reporting path crashed while reporting a failure | error paths get tests too |
| A stale plan was presented as the current plan | provenance and staleness on anything derived from a conversation |
| The notify loop died while status said `polling` | a background loop that can die must report that it died |
| A 2000-char truncation broke decomposition | when the summary *is* the payload, size limits are a design decision |
| Two UI regressions from one CSS refactor (white-on-white cards) | block-shaped buttons opt out of the control system explicitly |
| A restart left a live card `in_progress` forever, untouchable by any button | anything that can die must have something that notices and resumes it |
| asyncio's 64 KiB line limit killed runs after minutes of work | a limit you did not choose is still a limit you own |
| Fixing that, a trailing comment swallowed `cwd=`/`env=` in three executors | guards that matter parse the code; grep cannot see a kwarg that moved into a comment |

## Collaboration model

Built by one person (Manfred / yamantaka520) with Claude as the implementing
agent, on a two-machine loop: development on macOS, validation on a Linux host
running the service against real vendor CLIs. Nearly every bug in this document
was found by *running the product on a real project*, not by reading the code —
which is why the validation host exists and why features are not called done
until they have been exercised there.
