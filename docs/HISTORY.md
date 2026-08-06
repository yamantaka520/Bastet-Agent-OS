# Bastet Agent OS — Project History and Design Log

Last updated: 2026-08-07

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

### 2026-08-02 — the media loop, closed

One day, one theme: things agents generate must land somewhere real.

- **0.19.0** — the operator's missing handles, all found by running a real
  project: a display timezone (storage stays UTC), a heartbeat on in-progress
  cards (`updated_at` cannot tell working from stuck), 任務補給 (handing a
  running job the Firebase project id its spec never contained), previews for
  human approval (an approval without evidence is a blind signature), and the
  first PyPI + Docker Hub releases.
- **0.20.x** — models stay current (grok's whole lineup had turned over; the CLI
  is now asked live, every model field is free-entry), Bastet configures Bastet
  from the conversation (the `bastet-config` skill and the propose-then-apply
  protocol — the model proposes, the human's click is the authority), and
  finished work pushes itself to the project's remote. A same-day review pass
  found four defects in the new code, including a credential that could travel
  to the wrong host and `file:`/`env:` refs in chat proposals that could
  exfiltrate host files. Both closed the same day, stricter than the admin UI.
- **0.21.x** — generated media return to the chat (`$BASTET_CHAT_OUTBOX`,
  rendered inline) and media stages must persist real files (vendor URLs
  expire). Then three fixes from one live art card: a human retry refills the
  rework budget (three manual retries had produced three instant re-blocks),
  never background the generation and end the turn (a headless run's children
  are reaped; the "completion notification" can never arrive), and a job
  approved into done pushes like any other (the approve path skipped delivery
  entirely — 52 fresh PNGs stayed local with no audit row).

**The live proof of the whole loop**: a chat agent read the Novita docs it had
just been permitted to fetch, proposed a conforming image resource, the human
applied it, the agent called `seedream-5.0-lite` through the granted env,
self-corrected on the real pixel-minimum constraint, downloaded the expiring S3
result, and the orange cat rendered inline in the conversation. Credential never
printed.

### 2026-08-04 → 08-06 — the T3D saga: three cards, five lessons

CatsWalker's Three.js tasks were heavy enough to find every remaining weak
seam, one per day:

- **Quota failures wait themselves out (0.22.0).** `You've hit your session
  limit · resets 1:30am (Asia/Taipei)` blocked a card for hours — the deadline
  was in the message, and only a human could act on it. The orchestrator now
  parses the vendor's own reset time (timezone and all) and retries itself;
  Telegram says 「會自己續跑 —— 不需要你做什麼」.
- **Playwright became standard tooling (0.22.1)** — E2E gates want a real
  browser and approval previews want real screenshots; "provide a screenshot"
  had been an instruction without a means. The next card's approval carried six
  viewport screenshots.
- **Vendors tighten validation without notice (0.22.2).** OpenAI's strict
  structured-output rules started rejecting the codex verdict schema outright
  (`required` must list every key), killing every codex review before the model
  saw a token. And the operator's retry-with-another-agent silently lost to the
  role mapping — an explicit choice on retry is now a one-shot override.
- **A stage can declare its own time budget (0.22.3).** A 50–70 minute
  optimisation stage was killed four times at the fixed 3600s mark — an hour of
  work lost each time. Templates carry `timeout_s` per stage now.
- **Acceptance criteria must be verifiable where the work runs.** A reviewer
  kept demanding real-device fps evidence no headless agent can produce; the
  agent honestly logged "no real-device reports", the reviewer honestly
  rejected, and the loop correctly refused to converge. The resolution was
  human: a supply ruling split the criterion — machine-verifiable throttled
  emulation for the review gate, the real device reserved for the human at
  上線核准. Nobody faked anything, which is the point of the whole design.

### 2026-08-07 — the release day, and what the image was hiding

The doc set, screenshots and badges went out (0.22.4 → 0.23.1), and then two
days' worth of "CI is red" turned out to be three unrelated things wearing the
same colour:

- **A real bug, invisible to CI.** Running the suite inside our own published
  image failed one test. `augment_path()` prepends the well-known `TOOL_DIRS`
  and appends Bastet's own `bin` last, so a project shipping its own `pytest`
  wins the gate. In the image the interpreter lives in `/usr/local/bin` — itself
  a `TOOL_DIR` — so a minimal-PATH start prepended it and Bastet's runner won
  instead: the documented rule, inverted, in the artefact we ship. GitHub's
  runners keep Python in `hostedtoolcache`, so no CI leg could ever have seen
  it. Fixed in 0.23.2; the test now pins "exactly once, last".
- **A workflow red for a credential nobody configured.** Every `v*` tag failed
  at `gh-action-pypi-publish` because Trusted Publishing was never set up, and
  each attempt left a failed `pypi` deployment in the Deployments tab. The tag
  check and the wheel build are worth running on every tag; the publish is not
  worth failing over. Release split into `build` + a gated `pypi` job, so the
  environment — and its deployment record — only exists once the switch is on.
- **A platform outage.** GitHub Actions was in a major incident with webhooks
  throttled to ~15%: jobs cancelled at 15 minutes with "not acquired by Runner
  of type hosted", and pushes that produced no run at all. Nothing to fix in the
  repository — but it did expose that CI could only be *pushed*, never *asked
  for*, so `workflow_dispatch` was added.

The Windows leg stayed red by declaration, as it has been: ~35 tests assume
POSIX fake-executor scripts, forward-slash paths and 0600 bits. It reports and
does not block, and real Windows support is still its own pass.

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
| The model proposes, the human applies (bastet-config) | configuration by conversation without giving the model write access to configuration |
| Quota failures park with the vendor's own reset time | the deadline is in the message; waiting for a human to read it is waste |
| A human retry refills the rework budget | pressing retry means "I fixed the world"; a spent budget made the button a no-op |
| Retry's agent choice is a one-shot override | the role mapping is right for dispatch and wrong for explicit human intervention |
| Per-stage `timeout_s` | one global timeout cannot serve both a lint pass and an hour-long optimisation |
| Machine-verifiable criteria for machine gates, human criteria for human gates | a reviewer demanding evidence agents cannot produce loops honestly and forever |
| A publish step is gated, not attempted-and-failed | a job red for a missing credential says nothing about the release |
| CI is dispatchable, not only push-triggered | during an Actions incident a push produces no run at all |

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
| The approve path skipped push, memory, and the done event | when two paths reach the same state, a test must walk both |
| Agents backgrounded generation and waited for a notification that cannot arrive | one-shot runs must be told they are one-shot, with the incident in the brief |
| A credential fallback could send a GitLab token to github.com | credentials travel only to the exact host they were configured for |
| The event registry drifted eight types behind the code | a registry that nothing enforces is documentation, and stale documentation at that |
| Our own image inverted the PATH rule, and no CI leg could see it | the suite runs inside the artefact we ship, not only on runners that look nothing like it |

## Collaboration model

Built by one person (Manfred / yamantaka520) with Claude as the implementing
agent, on a two-machine loop: development on macOS, validation on a Linux host
running the service against real vendor CLIs. Nearly every bug in this document
was found by *running the product on a real project*, not by reading the code —
which is why the validation host exists and why features are not called done
until they have been exercised there.
