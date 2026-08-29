# Changelog

All notable changes to Bastet Agent OS. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

Every user-visible change bumps `__version__` in
`src/bastet_agent_os/__init__.py` and adds a section here; `web/package.json`
follows the same number and the WebUI prints it beside the title.
`tests/test_version.py` fails the build if the three drift apart.

## [0.34.16] - 2026-08-29

### Fixed

- A direct Pi card whose Agent has no explicit model now reuses the most recent
  `model_change` proven in that isolated account's Login & model settings
  terminal. Bastet validates the exact provider/model against the account's
  trusted extension catalogue and exports only that provider's saved key. It
  no longer falls back to an unrelated unauthenticated Pi default after the
  operator successfully tested an extension model interactively.

## [0.34.15] - 2026-08-29

### Fixed

- A reviewer precheck now reuses a just-passed repair verification when the
  command and Git HEAD are identical and the worktree is clean. The durable
  audit records the evidence commit and source audit row; any command, commit,
  tracked, or untracked change invalidates reuse and runs the test normally.

## [0.34.14] - 2026-08-29

### Fixed

- PM and incident retries that return a rejected card to its writable target
  now preserve the rejection brief and are recognized as repairs, so their
  `auto` stage must pass the original deterministic acceptance command before
  re-review. A failed repair verification remains at the current writable
  stage instead of being routed one stage too far backward.

## [0.34.13] - 2026-08-29

### Fixed

- An exhausted PM/incident circuit no longer strands a card forever when a
  materially different authoritative failure appears later. Reassessment is
  keyed by a stable fingerprint of the gate failure, terminal run, and latest
  handoff scope: unchanged evidence remains latched, while new evidence gets a
  bounded recovery attempt. A lifetime cap still prevents infinite churn.

## [0.34.12] - 2026-08-29

### Fixed

- A writable `auto` stage receiving rejected work must now pass the original
  acceptance stage's deterministic test/precheck command before Bastet can
  send the card to a reviewer again. A successful Agent exit no longer
  self-certifies a repair; failed repair verification keeps the card at the
  writable stage with the exact output and starts PM diagnosis.
- Exhausting the rework budget starts PM diagnosis immediately instead of
  relying only on a later supervisor sweep. Diagnosis start is persisted and
  posted in the project room, and the PM brief includes the latest handoff's
  actual changed paths, verification and risks so it can detect a repair that
  did not touch the rejected scope.

## [0.34.11] - 2026-08-29

### Fixed

- Direct Pi card runs now deterministically bridge the selected account's
  `auth.json` API key into the extension provider environment. This fixes the
  case where the same independent Pi profile passed an interactive inference
  but an unattended card process intermittently reported `No API key found`.
  The selected account credential takes precedence over inherited service or
  project variables, and the key value is never logged.

## [0.34.10] - 2026-08-29

### Fixed

- Pi direct runs now preserve repository isolation while explicitly loading
  only provider packages installed in the selected Agent account profile. This
  fixes profiles such as `pi-ollama-cloud-provider`, which disappeared under
  the blanket `--no-extensions` flag even though login and the model worked in
  Pi's interactive terminal.
- Pi model admission now uses the same isolated, trusted-extension model
  catalogue as the real run and resolves an exact provider/model pair. It no
  longer relies on `pi auth check`, which does not recognize extension-defined
  providers and incorrectly reported valid API-key profiles as `invalid_state`.

## [0.34.9] - 2026-08-29

### Added

- Every Agent row now has a **Login & model settings** action. It opens the
  executor's real server-side profile terminal and saves the exact LLM model on
  the Agent. Hermes runs its model/provider setup, Pi accepts `/login`, and
  OpenClaw runs its guided auth/model onboarding in the selected account home.
- Agent ids are editable. Bastet atomically migrates project roles, jobs, run
  history, resource grants, chats, room messages and handoff receipts while
  preserving the separate AgentMemoryOS identity. An Agent with a live run
  cannot be renamed.

### Fixed

- Direct Pi runs now call `pi auth check` for the selected model before doing
  work. Missing provider credentials are a non-retryable configuration fault:
  Bastet routes once to a configured stand-in when available, otherwise waits
  for login instead of spending PM/rework cycles on identical attempts.
- Long deterministic gates and host prechecks run outside the async control
  loop. Browser E2E can no longer make the board, API and WebSocket control
  plane time out while the test process is healthy.

## [0.34.8] - 2026-08-29

### Fixed

- OpenClaw's official installer may place its executable under
  `~/.npm-global/bin`. Bastet now includes that directory in runtime,
  systemd, and launchd paths, and the one-click installer publishes a safe
  `~/.local/bin/openclaw` link when needed. This prevents a successful
  OpenClaw install from appearing unavailable to task-card routing.

## [0.34.7] - 2026-08-29

### Added

- Pi Coding Agent is a first-class executor and account type. It runs in
  ephemeral JSONL mode, streams text/tool activity, reports provider usage and
  cost, applies a real read-only tool allowlist for review work, and supports
  direct profiles plus temporary OpenAI/Anthropic Bastet Gateway profiles.
- OpenClaw is a first-class direct-path executor and account type through the
  stable `agent exec --json --isolated` contract. Runs have bounded temporary
  state, explicit workdir/model/timeout wiring, provider usage and cost, and
  process-group cancellation. Its honest initial contract is writable
  code/light-task only; Gateway and review routes are rejected before a run.
- The one-click installer, maintenance card, login wizard, compatibility table,
  specification, and operator guides now include Pi and OpenClaw. Cross-executor
  guards cover non-interactive stdin/env, large output lines, workdir/env
  propagation, route admission, isolation, cancellation, and restart-safe
  serializable handles for the expanded CLI set.

### Validated

- v0.34.6 was deployed under maintenance with all 74 production cards intact.
  A separate real Hermes direct-path task card completed in 26 seconds through
  routing, worktree execution, reported usage, auto gate, stage handoff, project
  room message, commit, and cleanup without touching the production database.

## [0.34.6] - 2026-08-29

### Fixed

- Executor routing now validates direct versus Gateway support, API flavor,
  required model, read-only capability, and resource grant before creating a
  run. Automatic role routing skips incompatible candidates instead of
  spending a failed attempt or PM intervention; explicit incompatible choices
  are rejected before a run starts.
- Hermes supports the same two execution paths as the other subscription
  CLIs: jobs without an LLM resource use the existing logged-in Hermes profile,
  while OpenAI-flavor resources use a temporary Bastet Gateway profile. Direct
  runs import token and estimated-cost data from Hermes `--usage-file` output.
- Retry audit entries now name the Agent selected after role routing and retain
  the original explicit request separately. Every created run records a
  `run.routed` audit event with its actual Agent, executor, and direct/Gateway
  path.
- PM diagnosis routing ignores executors that cannot perform its read-only,
  direct-path task, preventing an incompatible PM assignment from consuming a
  diagnosis transport attempt.

## [0.34.5] - 2026-08-29

### Fixed

- Maintenance drain now parks a card durably before every new stage or
  executor retry. A completed attempt can no longer create a ghost run behind
  the fence; releasing maintenance resumes the parked card from its recorded
  boundary.
- Grok thought and tool lifecycle events now count as real activity and update
  `progress_at` independently of the process heartbeat. A live PID whose work
  stream has frozen can therefore be distinguished from an active long run.
- After two PM interventions, the deterministic supervisor performs one
  bounded evidence reassessment per human-renewed episode. It compares the
  latest gate, terminal run and PM handoffs, routes rejected work to a writable
  fixing stage or replaces a failed executor, and records the decision in the
  project room and audit log without renewing the PM lease or looping forever.
- The second PM diagnosis includes earlier intervention decisions so it cannot
  blindly repeat an ineffective handoff.

## [0.34.4] - 2026-08-29

### Fixed

- PM replacement decisions now return rejected work to the writable fixing
  stage and select the alternate for that stage's role. Previously a PM could
  correctly blame the implementer but Bastet replaced the reviewer and spent
  both intervention attempts rechecking the unchanged defect.
- Project-room handoffs are operational contracts rather than prose-only logs.
  Every receiving or replacement Agent gets an independent durable receipt;
  assignments and completion acknowledgements are posted to the room, and PM
  retries, replacements, supplies, and escalations state their reason, target
  stage, and assignee there.
- Automatic gates now record only `execution: succeeded` and explicitly warn
  that Agent-reported checks are not authoritative test evidence. Only the
  configured workflow gate can claim acceptance passed.
- Preserve and commit repository-tracked files under `._bastet/preview` when an
  Agent refreshes acceptance evidence; only untracked engine scratch remains
  excluded from job commits.

## [0.34.3] - 2026-08-27

### Fixed

- Serialize reads as well as writes on Bastet's process-wide SQLite connection.
  Concurrent WebUI lifecycle polling and orchestrator state transitions could
  otherwise overlap calls on the same connection, intermittently raising
  `sqlite3.InterfaceError`, returning malformed rows, and showing a completed
  card or an existing project as blocked/missing until a later reconciliation.
- Add a concurrency regression test proving a read waits for the connection's
  current operation instead of entering SQLite concurrently.

## [0.34.2] - 2026-08-27

### Fixed

- A PM or human ruling now restarts a rejected card at the stage's writable
  `rework_target`. Previously `supply_then_retry` re-ran the same read-only
  reviewer against an unchanged diff, ignored the ruling's requested code
  change, and could exhaust all recovery cycles without giving an implementer
  a chance to act.
- Ordinary environment retries still re-run the current stage. Returning to a
  rework target does not silently renew the bounded rework or PM recovery
  lease, so routing intent and circuit-breaker authority remain separate.

## [0.34.1] - 2026-08-25

### Fixed

- PM "ruling and retry" now adopts the project's current workflow instead of
  replaying a stale card snapshot. A compatible changed workflow cannot be
  bypassed with `refresh_workflow=false`, so newly deployed execution
  capabilities and host prechecks apply to existing cards.
- Retrying and reopening bounded rework/PM recovery budgets are separate
  operator decisions. A plain retry preserves the circuit breaker; the WebUI
  exposes an explicit recovery-lease checkbox when the environment truly was
  repaired.
- Agy read-only and PM diagnosis runs now use plan mode plus the terminal
  sandbox, auto-answer headless permission prompts inside that boundary, and
  add the job worktree as a readable directory. PM diagnosis can inspect the
  project without gaining edit or unrestricted host-shell access.

## [0.34.0] - 2026-08-25

### Added

- Workflow stages can declare execution `requires`, beginning with
  `browser.playwright`. Bastet performs a real Chromium launch-and-render
  preflight through its host runner before starting an Agent and exposes live
  execution-capability health through the API.
- Agent-review stages can declare an operator-controlled
  `gate_config.precheck_command`. Bastet executes it outside the LLM sandbox
  and injects the auditable output into the reviewer's context. The Web
  workflow uses this path for browser E2E evidence.

### Fixed

- Chrome/Playwright sandbox failures (`Crashpad` permission denial, `SIGTRAP`,
  missing browser executables) are execution-capability outages. They stop
  identical retries, preserve rework cycles, and post an actionable escalation
  to the project room.
- A failed PM diagnosis no longer selects the same broken path indefinitely.
  The next diagnosis uses another project executor; two transport failures trip
  a deterministic circuit breaker and publish the escalation in the room.

## [0.33.4] - 2026-08-25

### Fixed

- Active executor output now renews a bounded soft deadline while a hard 2x
  ceiling still terminates noisy or wedged processes. Long benchmarks no
  longer lose completed work at the fixed one-hour boundary.
- PM diagnosis transport or permission failures no longer consume a recovery
  intervention. Repeated executor failures use a narrow deterministic
  alternate-Agent fallback, with cooldown to prevent retry storms.
- Web review and E2E failures return explicitly to the implementation stage,
  whose two-hour budget covers browser and performance validation; they no
  longer drift backward into the design stage on later rework cycles.

## [0.33.3] - 2026-08-24

### Fixed

- Uvicorn now limits graceful connection draining to five seconds. An open
  dashboard WebSocket previously prevented application lifespan shutdown from
  starting at all, so systemd still hit its 15-second kill fence in v0.33.2.

## [0.33.2] - 2026-08-24

### Fixed

- Graceful shutdown now stops channels, awaits every cancelled lifespan task,
  terminates live executor handles with a bounded timeout, and reaps owned job
  drivers. Generated systemd units use a 15-second control-group stop fence.
- SQLite now uses WAL with `synchronous=FULL`, ensuring committed workflow
  state survives sudden power loss. Startup recovery keeps the same job,
  task-plan link and worktree, orphans interrupted runs, revokes their tokens,
  and resumes the recorded stage as a new attempt.

## [0.33.1] - 2026-08-24

### Fixed

- Removing a finished task card is now always recoverable: the compatibility
  DELETE endpoint archives it while retaining runs, gates, handoffs, usage and
  its task-plan link. The board no longer offers permanent deletion and can
  show and restore archived cards. This prevents project history from silently
  disappearing through a board action.

## [0.33.0] - 2026-08-24

### Added

- Gate-only revalidation for blocked `tests-pass` stages through
  `POST /api/jobs/{id}/revalidate` and `bastet job-revalidate`. It reuses the
  latest successful stage run, executes only the deterministic gate, and then
  follows the normal gate audit, project-room handoff, completion and branch
  delivery path. It refuses non-deterministic gates and stages without a
  successful run, preventing an Agent failure from forcing already-complete
  work and tests to run again.

## [0.32.3] - 2026-08-23

### Fixed

- Restored the full cross-platform CI matrix by correcting lint violations in
  the maintenance/handoff and release-workflow regression tests.

## [0.32.2] - 2026-08-23

### Fixed

- The local CLI now translates wildcard server bind addresses (`0.0.0.0` and
  `::`) to loopback destinations. A LAN-exposed installation previously sent
  the wildcard as its HTTP Host and was rejected by Bastet's own host guard,
  breaking maintenance/drain commands with `403 bad host`.

## [0.32.1] - 2026-08-23

### Fixed

- `tests-pass` gates now receive the same project secrets and granted resource
  environment as their stage Agent. Previously a stage could complete using a
  GitLab resource and then fail its deterministic gate solely because the
  engine dropped `BASTET_RES_*` variables at the gate boundary.
- Long TAP output retains its `not ok` failure summary even when thousands of
  later passing lines push the actual failure outside the stored output tail.
  Blocked cards now show the actionable failure instead of an all-green tail.

## [0.32.0] - 2026-08-22

### Added

- Durable maintenance/drain lock across API, CLI, supervisor and Admin UI. It
  fences new dispatch, retries, PM intervention and driver recovery while
  allowing existing runs to finish; component updates require a drained lock.
- Stage handoff delivery and acknowledgement records. Context assembly records
  the actual receiving Agent, and the project room exposes acknowledgement,
  understanding and question fields for auditable stage-to-stage transfer.
- Human-approved stages now publish the completed run's summary, changed paths,
  and approval evidence to the project room before advancing. Previously the
  approval path bypassed handoff creation entirely.
- Persistent context golden-case evaluation API measuring expected-bucket
  recall, expected-term recall and forbidden-term noise.

### Changed

- `bastet maintenance enter --wait` is the supported safe-deployment entry
  point; `status` reports active jobs/runs and `leave` reopens dispatch.

## [0.31.1] - 2026-08-22

### Fixed

- PM supervision now has a six-intervention lifetime ceiling in addition to its
  two-attempt episode budget. Manual retries can still resume a card, but can no
  longer reopen unlimited automatic retries; INT-01 had reached 17 PM
  interventions and 38 runs through that loophole.
- PM and infrastructure retries no longer erase the card's rework count. Only
  an actual human retry grants a fresh rework lease.
- `tests-pass` gates decode subprocess output with UTF-8 replacement. Binary or
  mixed-encoding test output is retained as readable evidence instead of
  crashing the job driver with `UnicodeDecodeError`.

## [0.31.0] - 2026-08-22

### Added

- Project meeting rooms are created with every project. Membership follows
  project role assignments; PM assignments and structured stage handoffs share
  one project-local activity stream in the UI and API.
- Stage completion records source/target stage, summary, changed paths,
  verification and risks for the next agent.
- Named `tests-pass` cases can declare `covered_paths`. Passing evidence is
  reused only while its commit remains an ancestor and changed paths do not
  intersect coverage; monolithic commands retain always-run behavior.
- Context selection now budgets spec, handoffs, history, test evidence, room
  messages, dependencies and semantic AMOS results by current stage role.
- Added opt-in `codex-app-server` using the stable stdio JSONL protocol for
  stateful threads, streamed events and approvals. `codex exec` stays supported.

### Changed

- AMOS retrieval uses semantic search instead of an importance-heavy generic
  context pack, reducing unrelated high-importance memories in runs.

## [0.30.2] - 2026-08-22

### Fixed

- PROGRESS.md stated the previous release — a claim that goes stale the instant
  we ship, and did: 0.30.1 was the release that updated the docs and made them
  wrong at the same time. The stated release is now pinned to `__version__` by
  a test, alongside the existing checks on `web/package.json` and the CHANGELOG
  heading. Historical mentions ("landed in 0.29.1") are untouched — those stay
  true forever.

## [0.30.1] - 2026-08-22

### Documentation

The doc set catches up with the 0.25 → 0.30 arc, which six releases of engine
changes had outrun:

- **PROGRESS.md**: current release, the 08-19 → 08-22 hardening arc with all six
  defects and their fixes, 479 tests, automated releases, `model3d`; open items
  rewritten (the release-secrets item is done; per-role stand-ins and the E2E
  stage's undeclared time budget are the honest new ones).
- **docs/WORKFLOWS.md**: the rework walk-back, PM-level supervision and its
  fence, depleted agents leaving the routing rotation, per-stage commits with
  `._bastet/` excluded and why a sandboxed agent needs its worktree's git
  metadata, liveness as two separate facts, and four new rows in the
  every-stop table.
- **docs/USER_GUIDE.md**: alive-vs-talking on the board, and the PM's question
  appearing on the card with a box for the ruling.
- **SECURITY.md**: the extra writable root granted to sandboxed executors —
  what it is, how narrow, and what it does allow — plus the PM supervisor's
  fence as a trust boundary.
- **docs/HISTORY.md**: the full account of one card finding six defects in
  order, three of them introduced by the previous fix, and the two method
  lessons it settled.
- **README.md**: roadmap rows through 0.30.

## [0.30.0] - 2026-08-22

Two engine defects the same card kept exposing: we were killing our own
long-running tests, and committing our own scratch directory into the work.

### Fixed

- **The supervisor interrupts the dead, not the merely quiet.** It judged
  liveness by `progress_at` (when a run last *spoke*) instead of `heartbeat_at`
  (when it was last confirmed *alive*) — throwing away the distinction this
  engine keeps on purpose. Live cost: an agent reported "FPS bench is still
  running. Waiting for it (20 levels × 60s)", beat every 20 seconds to prove it
  was alive, and was executed at the 15-minute silence mark. Four times,
  growing to 42 minutes, across two different agents — a 20-minute test can
  never finish inside a 15-minute patience, so no retry and no handover could
  ever have helped. A lost heartbeat (3 minutes, nine missed beats) now decides
  interruption; a quiet-but-alive stage is bounded by its own `timeout_s`, which
  is what declaring a time budget is for. The board still shows it amber,
  because "alive but silent" is information, not a death sentence.
- **`._bastet/` is never committed.** Per-stage commits (0.29.0) used
  `git add -A`, which swept the engine↔agent boundary — previews, verdict
  files, the inbox — into the job branch. Every later run then regenerated
  those files and dirtied the tree again, so the reviewer went on refusing
  evidence for "uncommitted modifications": the same wall the card hit before,
  freshly painted by our own fix. Previously-committed scratch is untracked
  once, and staging excludes the path from then on.

## [0.29.1] - 2026-08-22

### Fixed

- **A sandboxed agent can use git in its worktree again.** A linked worktree's
  `.git` is a *file* pointing at `<main repo>/.git/worktrees/<name>`, so every
  git WRITE lands outside the worktree — outside what `--sandbox
  workspace-write` allows. Commit, stash and even index refreshes failed, in
  words that read like broken hardware: `cannot lock ref 'ORIG_HEAD':
  Read-only file system`, `cannot create .git/worktrees/<job>/index.lock`.
  (That message fooled this maintainer once: the directory really is writable —
  it is just not in the sandbox.) codex now receives it as an `--add-dir`
  writable root on write-capable runs, never on read-only reviews. Detected by
  reading the `gitdir:` pointer, so an ordinary checkout grants nothing.

## [0.29.0] - 2026-08-22

A reviewer refused test evidence because the scripts that produced it were
uncommitted changes on top of HEAD. It was right, and the cause was ours.

### Changed

- **Every stage commits its own work.** The worktree was committed once, at
  job completion — so through a whole multi-stage, multi-rework pipeline, every
  stage's output sat uncommitted and each later stage reasoned about a tree that
  matched no commit. Nothing could bind test evidence to the content under
  review, which is exactly what the reviewer said. Each stage boundary now
  commits to the job branch as `bastet(<stage>): <title>`, so the history reads
  as the pipeline actually ran, rework included, and every stage starts from a
  clean tree. (The job row is re-read first: the *first* stage's run is what
  creates the worktree, so the row in hand still says None.)
- **The evidence-freshness rule is now satisfiable.** Committing a test log
  changes the tip, so evidence can never name the commit containing it —
  demanding that is a loop with no exit, and one card was rejected three times
  by it. The review brief now accepts evidence that names the commit it ran
  against when that commit is an ancestor of the reviewed tip and the delta
  touches no product code, while still rejecting non-ancestors (rebase or
  force-push), product changes made after the tests ran, and uncommitted
  modifications to the code or scripts behind the evidence.

## [0.28.0] - 2026-08-22

The PM escalated a question it could have answered, and the card showed the
human a retry button and no question at all.

### Changed

- **Escalation is the PM's last resort, not its default.** It escalated "which
  commit is the acceptance baseline?" — a fact its own read-only diagnosis run
  could have established with `git ls-remote`. The brief now asks one question
  first: is the reviewer demanding *a checkable fact* or *a decision that needs
  authority*? Facts the PM rules on itself (`supply_then_retry`); escalation is
  reserved for money, publishing, changing acceptance criteria, choosing between
  two defensible product directions, and things only a human can do in the
  physical world. An escalation's `reason` must now be phrased as an answerable
  question, because the card presents it as one.

### Added

- **The card shows what the PM decided — and what it is asking.** An escalation
  that lives only in the audit log is an escalation to nobody: the operator saw
  `blocked` plus a retry button, with no sign the PM had a question for them.
  Job detail now carries `pm_decision`, and an escalated card opens with a
  「PM 需要你的裁定」panel: the question verbatim, a box for the answer, and one
  button that files it to the job's inbox and retries — one action, because a
  ruling only helps if the card runs again, and a human retry is what refreshes
  the rework and PM budgets. The Telegram escalation notice points at it instead
  of offering retry alone.

## [0.27.0] - 2026-08-22

### Fixed

- **Rework walks backwards instead of standing still.** The target for a failed
  gate was the nearest writable stage counting *from the failing stage itself* —
  so a stage that can write was always its own target, forever. Live cost: an
  E2E stage failed one test, the tester re-ran that same failing test nine times
  across four hours (three full rework budgets, two PM interventions), and
  nobody ever touched the product code the test was failing on. The hand-back
  now advances: the failing stage first (an implementer whose own tests fail
  should fix them), then the nearest earlier writable stage, skipping read-only
  reviewers, clamping at the earliest writable stage. Counted per stage per
  episode, so a human retry starts the walk over. An explicit `rework_target`
  still wins outright.

## [0.26.1] - 2026-08-21

0.26.0 went live and the first real 402 exposed two holes in the handover
chain it had just built.

### Fixed

- **Only the failing run's own error decides whether a balance is the problem.**
  The classifier pooled the rework note with the live error, so a card that had
  ever hit a 402 read every later failure as "balance exhausted". Live cost: an
  unrelated Agy failure was diagnosed as a balance problem, and the resulting
  handover dispatched the one agent that genuinely had no balance.
- **An exhausted supervisor hands over instead of going silent.** The PM was
  offered only *non-recoverable* stalls, so a card the supervisor had called
  recoverable but could no longer act on (two retries spent) fell through to
  nobody at all — which is exactly what a live card did after its 402. Both
  conditions now reach the PM.

## [0.26.0] - 2026-08-21

A card looped on a Grok `402 Payment Required` while the PM watched its own
decisions get undone. Routing was the bug, not the supervisor.

### Fixed

- **A depleted agent leaves the rotation.** `402 Payment Required: usage
  balance exhausted` matched none of the quota markers, so the card merely
  "failed" — and role mapping dispatched the same dead agent on every rework
  cycle, earning an instant 402 each time. The PM diagnosed it correctly and
  handed the stage over twice; the rework cycle cleared the one-shot override
  both times and routed straight back. Now a vendor's credit exhaustion marks
  the agent depleted (`agents.depleted_at`), every routing path skips depleted
  agents — role mapping, explicit override, alternate selection, the job
  default, and PM selection — and the stall becomes recoverable *by routing*,
  so the infra supervisor swaps in a funded stand-in without spending a PM
  intervention.
- **A one-agent role stands in rather than dead-ends.** `tester` was one agent
  on the live project; when its balance emptied there was no funded tester at
  all, though three capable agents sat under other roles. Dispatch now falls
  back to any funded agent on the project, and says so in the log.
- **Only a human clears it.** Topping up is not something the engine can do:
  the flag clears through `POST /api/agents/{id}/undeplete`, the 「已充值，解除」
  button on the Agents card, or a human retry that explicitly names the agent.
  Automated retries (PM, supervisor, quota resume) cannot clear it.
- **Everyone is told, once.** An `agent.depleted` event, an audit row, a team
  memory, and a Telegram note saying which agent, what the vendor said, that
  work is being routed around it, and how to clear it. The PM's brief now names
  this case so it stops proposing handovers the router already made.

## [0.25.3] - 2026-08-20

A human-approve stage looked stuck with "needs approval" and no approve button
anywhere. Root cause chain, each link now fixed:

### Fixed

- **agy: the envelope outranks the exit code.** agy flushes telemetry to
  Google AFTER printing its result; on flaky egress that flush fails and the
  process exits nonzero holding a complete SUCCESS envelope. 4 of 5
  approval-prep stages in one day were marked "execution failed" over
  finished, correct work — so the human gate never opened, which is why there
  was nothing to approve. A SUCCESS envelope now wins (with a logged warning
  about the exit code); a real failure's record now leads with
  `[agy status=… exit=…]` instead of burying the cause.
- **The PM is told what "execution failed" means.** It diagnosed a dead
  approval-prep run as "needs human approval" — but no gate was pending, so
  the human found nothing to click. The diagnosis brief now distinguishes
  "the stage's run died (retry it — nothing awaits approval yet)" from
  "the gate rejected the work (rule or escalate)".
- **An escalation notification carries the 🔁 retry button.** The human's
  lever on an escalated stall is retry (it also unlatches the PM); a message
  that asks for a human with no control sent someone hunting for an approve
  button that does not exist.

## [0.25.2] - 2026-08-19

### Fixed

- **A human retry refreshes the PM's intervention budget.** The 2-intervention
  cap was counted over the job's lifetime, so after a person fixed the
  environment and retried, the PM would never help that card again — while the
  docs (and the rework budget's own rule, "a human retry is a fresh lease")
  said otherwise. The budget now counts per human-retry episode; automated
  retries (the PM's own, the infra supervisor's, quota auto-resumes) do not
  anchor a new episode, pinned by a test that fails if the PM can refresh its
  own allowance.

## [0.25.1] - 2026-08-19

### Added

- Resources can be **reclassified**: `PUT /api/resources/{id}` accepts `kind`,
  validated against the target kind's requirements and audited. Categories
  arrive after the resources do — the Meshy 3D endpoints were filed under
  "image" until `model3d` existed, with no way to move them.

## [0.25.0] - 2026-08-19

Three asks from live operation: the engine should keep its own project moving,
review evidence must verifiably reach the person, and 3D generation deserves a
truthful category.

### Added

- **PM-level supervision.** The infrastructure supervisor already handled
  fake-alive runs and executor crashes; business stalls (rework budget spent,
  criteria disputes, missing rulings) still just waited for a human — the PM
  that planned the card had no further duty. Now a card blocked for a business
  reason is diagnosed by the project's PM agent, which chooses one bounded
  action: retry, hand the stage to another agent, file a ruling into the job's
  inbox and retry, or escalate with its reasoning. Hard walls: two
  interventions per card (audit-counted, restart-proof), an escalation latches
  until a human retries, human-approve gates and quota waits are never
  touched, the diagnosis run is read-only, and a secret-shaped "ruling" is
  refused the same way the human supply endpoint refuses it. Every
  intervention is an audit row, a team memory, and a Telegram note.
- **Delivery accounting for notifications.** The route to api.telegram.org is
  provably flaky, and a one-shot send with no record left "did the approval
  evidence ever reach Telegram?" unanswerable. Every outbound message and
  attachment is now retried (3 attempts, backoff) and recorded as
  `notify.sent` / `notify.failed` in the audit log.
- **The approval card carries the checklist.** Approval requests (event and
  /approve command alike) now include the stage's own description and the
  spec's acceptance section, plus the preview attachments — approving from a
  phone no longer means approving blind.
- **`model3d` resource kind.** Meshy-style 3D model/animation generation was
  filed under "image" for lack of a truer category; it now has its own media
  kind across validation, the resource browser, grants, media briefs and the
  bastet-config skill.

## [0.24.2] - 2026-08-19

### Fixed

- **The verdict schema no longer hijacks non-review runs.** PM decomposition is
  a read-only run whose answer IS a task list — but codex, agy and grok bound
  their review schema to `read_only`, so a codex PM could answer nothing but
  `{verdict, reasons, comments}` and honestly rejected every decomposition:
  "no usable tasks in the decomposition", for any card format. (Undetected for
  weeks because the PM role happened to be held by agents whose planning path
  didn't enforce the schema; promoting Codex1 to pm exposed it within three
  dispatches.) `TaskSpec` now carries `expect_verdict`, set by the orchestrator
  for agent-review gates only; `read_only` remains purely a tool restriction.
  Each executor's argv is pinned by tests that fail if the two concepts are
  ever re-merged.

## [0.24.1] - 2026-08-19

### Added

- **A bounded project supervisor.** It distinguishes process heartbeat from
  semantic progress, interrupts a live run after fifteen minutes without
  progress, and automatically recovers classified engine/executor failures
  (`max turns`, no output, lost/orphaned driver) at most twice. Recovery prefers
  another enabled agent and writes both the audit trail and project AMOS memory.
- **Complete approval packages.** Bastet generates `_review-manifest.md`; Telegram
  sends images as photos, videos as playable video, and PDF/HTML/Markdown/text as
  documents instead of listing filenames that approvers cannot inspect.

### Fixed

- Successful runs missing a gate because their driver disappeared are resumed,
  while `_driving_jobs` prevents the supervisor from duplicating a legitimate
  long-running `tests-pass` gate.
- New job worktrees use project `base_ref`, then `main`/`master`, rather than the
  host repository's ambient checkout.
- The whole test suite now points `AGENT_MEMORY_HOME` at a per-test temporary
  store. Tests no longer write the operator's real AMOS on an unrestricted host
  or fail read-only inside a sandbox.
- Human approval and acceptance failures remain human decisions; the supervisor
  never approves or weakens a gate.

## [0.24.0] - 2026-08-17

A card sat "in progress" for 52 minutes with nothing happening and no way to
tell. Both halves of that are fixed here.

### Fixed

- **Nothing a run spawns can wait for a human.** A PM stage ran `npm exec
  playwright --version`; npx wanted to install the package first and asked "Ok
  to proceed? (y)". Its stdin was a tty, so it waited — 52 minutes, 2 seconds of
  CPU, the agent blocked on its own child, the card frozen behind a question
  nobody would ever see. Every CLI executor now spawns with `stdin=DEVNULL` and
  a non-interactive environment (`CI`, `npm_config_yes`, `GIT_TERMINAL_PROMPT=0`,
  `DEBIAN_FRONTEND`, `PIP_NO_INPUT`), both inherited by grandchildren — because
  what the agent decides to run is not ours to control. The human-approve brief
  also names the incident and says not to `npx` the Playwright that is already
  installed.
- **`last_json_object` returned the wrong object for line-delimited output.**
  It kept the last object to *start*, which inside `{"result":{…,"usage":{…}}}`
  is the nested usage dict — so a successful run parsed as "no status", i.e.
  failed. The last complete line is now preferred; the character scan remains
  for pretty-printed output. Found by replaying a real agy transcript, not by
  reasoning about it.

### Changed

- **agy streams.** It ran `--output-format json`, which prints nothing until the
  process exits: a 53-minute stage showed no sign of life for its entire run,
  and all 23 of its heartbeats were a literal `…`. Now `--output-format
  stream-json`, with the agent's own words as progress text. The schema still
  binds the final result, and `result()` accepts both envelope shapes — the
  streamed one wraps it, and reading the wrapper would have marked every agy run
  failed.
- **A run reports alive even when it says nothing.** A 20-second beat, running
  alongside the stream, claims only what it can check: the process has not
  exited. One-shot executors (every read-only reviewer) no longer look dead for
  their whole life. A failing beat can never break a run.
- **The board separates "alive" from "talking".** New `runs.progress_at` records
  when a run last *said* something, next to `heartbeat_at` for when it was last
  confirmed alive; the card turns amber after 10 minutes of silence even while
  the process is healthy. That distinction is exactly what the 52-minute hang
  looked like from outside. `/api/runs` now exposes all three.

## [0.23.2] - 2026-08-07

### Fixed

- `augment_path()` no longer prepends the interpreter's own bin directory when
  it happens to be one of the well-known `TOOL_DIRS`. In the shipped Docker
  image the interpreter lives in `/usr/local/bin`, so a service started with a
  minimal PATH put Bastet's own `pytest` **ahead** of the project's — the exact
  opposite of the documented rule. Found by running the suite inside the image;
  GitHub's runners hide the interpreter in `hostedtoolcache`, so no CI leg could
  have caught it. The test now asserts the directory appears exactly once, last.

### Changed

- Release workflow: the tag/version check and the wheel build still run on every
  `v*` tag, but publishing is gated — PyPI on `vars.PUBLISH_TO_PYPI`, Docker Hub
  on its two secrets — and reports a skip notice instead of failing. A job that
  is red for a credential nobody configured says nothing about the release.
- CI/Release actions moved to `checkout@v5` / `setup-python@v6` (Node 20 EOL).
- Release workflow split into `build` + `pypi`, so the `pypi` environment — and
  the GitHub deployment record it creates — only exists once publishing is
  switched on. Five stale failed `pypi` deployments were retired to `inactive`.
- Docker Hub repository overview (`docs/dockerhub-overview.md`) written and
  published: what is and is not in the image, the `/data` volume, uid 1000, and
  the security note. Every claim verified by running the published image.
- New CI leg `image-base`: the suite also runs inside `python:3.12-slim`, the
  base of the published image, where the interpreter sits in `/usr/local/bin`.
  That is the environment the PATH bug above lived in and the six existing legs
  could not see.
- CI gained `workflow_dispatch`. During GitHub's 2026-08-06 Actions incident
  webhooks were throttled to ~15% and commits landed with no run at all; a run
  should be something you can ask for.

## [0.23.1] - 2026-08-07

### Added

- README (both languages): badge row (PyPI version, Python versions, CI,
  Docker pulls, license) and a Screenshots section with three live captures
  from the validation deployment — the expanded project card, a finished
  card's drawer with its rework history, and the maintenance card. Image
  URLs are absolute so the PyPI project page renders them too.

## [0.23.0] - 2026-08-07

### Security — findings from a dedicated review, each with a pinned test
The common shape: a directory the agent writes and the system collects, where a
symlink is an instruction to exfiltrate whatever it points at.

- **Preview collection refuses symlinks and escapes.** `._bastet/preview/` is
  agent-written (and repo content is untrusted): a symlink named `x.png`
  pointing at `~/.bastet/api_token` would have been copied into artifacts and
  sent to Telegram as a "photo".
- **The chat outbox refuses symlinks and escapes** — it has no extension filter
  at all, so a symlink would have attached any host file to the conversation.
- **Preview endpoints use resolved-path containment, not name sanitisation.**
  The first fix used `Path(job_id).name` — which is `".."` for `".."`, a no-op —
  and its own test disproved it. The property actually wanted (resolves inside
  the artifacts dir) is now what is checked.
- **The built-in `bastet-config` skill is not editable from chat** — redirecting
  its `skill_source` would poison the guide every future conversation reads.

### Fixed — the automatic quota-reset retry no longer refills the rework budget
That refill is the human's "I fixed the world". A vendor limit interleaving
with a rework loop would otherwise have disabled the cycles cap entirely.

### Changed — install.sh defaults to PyPI
Versioned, cache-friendly, and what the maintenance card compares against;
`BASTET_REPO` still overrides for source installs.

### Added — documentation
`docs/USER_GUIDE.zh-Hant.md`（繁中操作手冊）: onboarding, daily operation per
tab, a stuck-card quick table, and ops routines. SECURITY.md rewritten around
the actual boundaries: where agent-written content crosses into system
behaviour and the rule at each crossing, including everything this review
added.

## [0.22.4] - 2026-08-07

### Added — the documentation caught up with the product
Fourteen versions had shipped since the docs were written. Everything brought
current (READMEs in both languages, USER_GUIDE, INSTALLATION, HISTORY, PROGRESS,
ROADMAP), plus two new documents:

- **docs/WORKFLOWS.md** — the workflow operations manual: every stage field
  (including `timeout_s`), every gate, the rework loop, quota self-wait, retry
  semantics (budget refill, one-shot agent override, workflow refresh),
  supplies, previews, delivery, and a table of every way a card stops with what
  to do about each.
- **docs/CAUTIONS.md（注意事項）** — every operational pitfall hit in
  production, with the boundary rules: the venv-on-PATH `python` trap, vendor
  quota and validation surprises, unverifiable acceptance criteria, media URL
  expiry, credential handling, restart effects, and the honest list of what is
  still open.

## [0.22.3] - 2026-08-06

### Added — a stage can declare its own time budget
The dispatch default (3600s) was the only timeout, and a heavy stage — a
50-70 minute Three.js optimisation pass, live — kept being killed at the hour
mark, losing the whole run's work each time (four times on one card, including a
run that had worked for 59 minutes). Templates can now set `timeout_s` per
stage; the run token's TTL follows the effective budget. 0 (the default)
inherits the dispatch value, nonsense clamps to inherit.

## [0.22.2] - 2026-08-05

### Fixed — every codex review died on `invalid_json_schema`
OpenAI's strict structured-output validation requires `required` to list every
key in `properties` at every object level; our codex verdict schema listed only
`verdict`, so the vendor rejected the request outright and the review run
failed before the model saw a single token. The schema is strict-compliant now,
and a test walks every object level so adding a field cannot quietly
reintroduce the rejection.

### Fixed — retrying with a different agent silently used the same one
Role assignment outranks the job's default agent — correct for dispatch, wrong
for a human explicitly picking who runs a retry: the live card was retried with
Claude1 and the role mapping handed it straight back to the failing Codex1. An
explicit choice on retry is now a one-shot override that outranks the mapping
for the retried stage and clears on the next stage transition, so the role
mapping resumes where the human's intervention ends.

## [0.22.1] - 2026-08-04

### Added — Playwright as standard tooling
Browser automation joins pytest and Pillow as a tool the shipped workflows can
assume: `install.sh` and the Docker image install the `playwright` package and
the chromium browser (the package without a browser dies with "Executable
doesn't exist" on first use, so both go in — a failed browser download warns
with the exact command instead of aborting the install). The maintenance card
tracks it for check/update.

The human-approve preview brief now names the tool: a web project's approval
evidence can be a real `playwright screenshot` of the running page, not just a
prose summary — "provide a screenshot" was an instruction without a means.

## [0.22.0] - 2026-08-04

### Added — quota failures wait themselves out
The live case: every attempt on a card died in seconds with `You've hit your
session limit · resets 1:30am (Asia/Taipei)` — and the card just blocked. The
vendor's message states the deadline, but a human still had to notice, wait for
someone else's clock, and press retry at the right moment.

The orchestrator now reads the clock. An execution failure that is a quota /
rate limit (session limit, usage limit, 429, overloaded, low credit) parks the
job with a `resume_at`: the stated reset time when the message names one —
am/pm and the vendor's timezone parsed, next-occurrence semantics, a safety
margin, capped at 26h — or a 30-minute backoff when it does not. A background
sweep retries due jobs (audited as `server:quota-reset`); a manual retry beats
the clock and clears the timer. The Telegram notification says it plainly:
⏳ 額度用盡，會自己續跑 —— 不需要你做什麼，預計 HH:MM 自動重試.

Unparseable inputs stay safe: an unknown timezone falls back to UTC, a nonsense
time falls back to the default backoff, and an ordinary failure is never
mistaken for a timer.

## [0.21.4] - 2026-08-02

### Fixed — "No module named PIL", but only inside runs
Media runs failed on Pillow while the system `python3` had it all along. The
bastet venv sits on PATH (last, by design), and on systems without
`/usr/bin/python` that makes a bare `python` resolve to the venv — which did not
carry Pillow. It does now, everywhere: `install.sh`, the Docker image, and the
maintenance card track it as standard media tooling.

## [0.21.3] - 2026-08-02

### Fixed — a job approved into done never pushed
Auto-push (and the done audit row, the finished-memory write, and the job.done
event) hung off the driver loop's completion branch only. A job whose LAST stage
is human-approve completes through `approve()` instead — the live art card did
exactly that, and its 52 freshly generated PNGs stayed local with no audit row
of any kind. Both completion paths now deliver identically.

## [0.21.2] - 2026-08-02

### Fixed — the second way the art card got stuck
After the DNS blip cleared and the retry refilled the budget, the card stalled a
new way: the agent kicked its generation pipeline into the **background** and
ended its run — "the background task will notify me when it finishes". It
cannot: a headless run is one-shot, its children are reaped the moment it ends,
and no notification ever arrives. Three cycles of start-pipeline-and-exit, each
honestly rejected by the reviewer over empty asset directories.

The media brief now states this bluntly, with the incident in it: never
background the generation and end the turn; poll in the foreground (sleep +
check files) until the assets exist, batch when there are many, and if time
truly runs out, finish part of the set and say exactly where you stopped so the
next cycle continues.

## [0.21.1] - 2026-08-02

### Fixed — a human retry refills the rework budget
Live case: a transient DNS failure burned all three rework cycles on an art
task (nothing could be generated, review honestly rejected, loop exhausted —
all correct). Then the operator pressed retry three times and got three instant
re-blocks. The retry re-ran the *reviewer*, which rejected the same diff, and
the spent budget meant the failed gate could never hand the card back to the
writing stage that would actually regenerate the work.

Retry now resets `rework_count` (and the stale rework note): a person pressing
the button after fixing the world is a fresh lease, so the failed review flows
back to the writer and the pipeline finishes on its own — which is the entire
point of the loop.

## [0.21.0] - 2026-08-02

### Added — generated media come back into the conversation
A chat agent that generates an image had nowhere to put it: the reply is text.
Every agent-responder run now gets `$BASTET_CHAT_OUTBOX`; whatever it saves
there (image, audio, video, document) is attached to the assistant message and
rendered inline in the chat — images as images, audio with a player, video with
controls. The prompt tells the agent to download the actual file rather than
paste a vendor URL, because those expire. Caps: 8 files, 50 MB each; the outbox
is removed after collection either way.

### Added — media stages are told to persist their assets
Q: does an async generation's expiring download URL survive? Only as a file. A
stage whose project has media resources granted now carries an explicit rule in
its brief: download generated assets into the worktree (the project's asset
directory) before the stage ends, polling async jobs to completion within the
run — the workflow then commits them to `bastet/<job_id>` and pushes, so the
files (not the URLs) are what survive. A background fetcher for async jobs that
outlive their run is on the roadmap; until then the brief demands honesty:
"say the generation did not finish" beats leaving a dead link.

## [0.20.2] - 2026-08-02

### Fixed — the chat agent had the credential and no way to use it
The Novita case, part two. The resource had `NOVITA_API_KEY` wired, but a chat
responder run never built resource access at all: no env vars, no MCP config,
no manifest — and its tool set had neither Bash nor a web tool, so even a wired
credential would have been uninvokable. The agent truthfully said it had no
credential.

A project-scoped chat now hands the responder exactly what a workflow run gets:
`BASTET_RES_<NAME>_URL/_KEY/…` env vars, the MCP config, and the resource
manifest in its prompt. Its tool set is Read/Grep/Glob/WebFetch/WebSearch/Bash —
no Edit/Write (a conversation should not edit the repo), but Bash so a granted
API is actually callable from the conversation. The secret-bearing access dir is
removed when the reply finishes, same as a run.

### Fixed — `auth_header` as a full header line crashed every probe
The field means "header name" (`X-API-Key`), but an agent reading vendor docs
naturally writes the whole line — `Authorization: Bearer {API_KEY}` — and the
live Novita setup did exactly that, so both resources' test buttons failed with
`Illegal header name`, which reads as "our test connector is broken". Both
shapes are legitimate input now, normalised in one place (`auth_header_pair`)
and used by the test probe and the MCP header path alike: placeholders
(`{API_KEY}`, `{TOKEN}`, …) are substituted with the resolved credential, a bare
`Authorization` still gets its `Bearer` prefix. The config guide documents both
shapes.

## [0.20.1] - 2026-08-02

### Fixed — WebFetch / WebSearch were not permitted
"讀一下這份文件" failed with a permission error: neither the default stage tools
nor the read-only set (chat responders, reviewers) included the web tools. Both
do now — an agent implementing against a vendor API needs the vendor's docs, and
fetching them is read-only by nature.

### Fixed — `no such table: teams` on chat-apply
Teams are AMOS org objects; Bastet has no local `teams` table, and the apply
path's scope check invented one. It now validates projects locally and accepts
team ids the way the rest of the product does.

### Fixed — four defects from a code-review pass over the recent features
- **Every approval arrived on Telegram twice**: the preview work added a second
  `gate.pending` emit. One event now, carrying the previews.
- **A git credential could travel to the wrong host**: auto-push fell back to
  "any granted git resource" when none matched the remote — which would send,
  say, a GitLab token in a header to github.com. Exact host match only; no
  match pushes unauthenticated and lets git say no.
- **Chat proposals are now stricter than the admin UI about credentials**:
  `secret:<id>` pointers only. A model-proposed `file:`/`env:` ref could point a
  "credential" at an arbitrary host file, which a run would then send to
  whatever endpoint the same proposal named. Credential rows are also not
  updatable from chat, and the apply card now shows each action's endpoint and
  ref, so the person clicking sees where data will flow.
- **Preview files are size-capped** (10 MB) — `read_bytes()` on an unbounded
  "preview" is a memory grenade — and a timed-out push is audited as failed
  instead of vanishing.

### Added — the guide teaches the skill-install flow
The novita case: a proposal can create a skill resource carrying its
`install_command`; the human applies, then presses 安裝 on the Resources tab
(admin, audited, full log) — the same flow MCP installs use. Shell on the host
never runs as a side effect of applying a proposal.

## [0.20.0] - 2026-08-02

### Fixed — model pickers that were stale the day a vendor shipped
The executor model lists were curated once and wrong already: grok's entire
lineup had turned over (the CLI itself now offers only `grok-4.5`), and claude
gained the `fable` alias. Three changes so this cannot rot again the same way:
the Claude lists carry the current aliases and full ids (fable/opus/sonnet/haiku,
claude-fable-5 …); `grok models` is asked **live** and the curated entry is only
the fallback; and every model field is now free-entry with suggestions
(datalist) instead of a closed dropdown — any model id a vendor ships tomorrow
is usable the same day.

### Added — Bastet configures Bastet, from the conversation
A built-in `bastet-config` **skill** (globally granted, regenerated each boot)
documents Bastet's own configuration: every resource kind — including the
multimedia ones (image / video / music / tts / stt) — its fields, how
credentials travel, scopes, and an action protocol. A chat responder asked to
"set up an ElevenLabs TTS resource" reads it like any other skill and ends its
reply with a fenced ```bastet-config``` proposal block.

The chat renders that block as a card. **The model proposes; the human
applies**: nothing happens until the 套用 button, the audit rows name the person
who pressed it (`via: "chat"`), and per-action results mean one typo doesn't
void four good actions. The whitelist is deliberately small — resources, grants,
timezone. Users, tokens and channels are *not* in it: anything that changes who
can act is not configuration, and a prompt-injected "add an admin" must find
nothing here to call. Raw keys in a proposal are refused outright (they already
travelled through the model); `secret:<id>` pointers only.

### Added — finished work pushes itself to the project's remote
When a job walks its whole pipeline — tests green, reviews approved, human gates
passed — its `bastet/<job_id>` branch is pushed to the project's remote:
`origin` if the repo has one, else a granted git resource's URL. Credentials
come from the project's git resources (deploy key via `GIT_SSH_COMMAND`, or a
token in an env-provided header — never argv, never the URL). The project's own
branch is **never** pushed: parking work somewhere reviewable is automation,
merging it is a decision. Per-project opt-out (`git_auto_push: false`); an
unchanged branch (read-only stages) is not pushed; a failed push is audited and
non-fatal — the job is done and the work is safe locally.

## [0.19.0] - 2026-08-02

### Added — a display timezone, as a system setting
Every timestamp rendered as UTC. The Admin tab gains a 系統設定 card: pick a
timezone (common list + any IANA name, with one-click "use the host's zone") and
every timestamp in the UI renders in it immediately, for every user. Storage
stays UTC — an audit trail in local time cannot be compared across machines —
and the five hand-rolled time renderings were replaced by one formatter, so the
next fix to time display is one edit.

### Added — the board shows that work is happening
An in-progress card carries a stage progress bar (n of m stages) and a
heartbeat: the run's last output line and how long ago — 🟢 while fresh, 🟠
after three silent minutes with a "possibly stuck" hint. Runs persist a
throttled heartbeat (`heartbeat_at`, `progress_text`) and emit `run.progress`
on the bus. `updated_at` could not tell working from stuck, because a long
stage legitimately goes minutes between DB writes.

### Added — 任務補給: handing data to a running job
The Firebase case: mid-project, the agent needs a deploy target or a project id
that the spec never contained, and there was nowhere to put it. The job drawer
gains a supplies box: what you provide is included in every later run's brief
(marked as overriding the original spec), and a live worktree also receives it
as a `._bastet/inbox/` file. Credential-shaped content is refused with a pointer
to the credentials card — a supply travels inside a prompt, and prompts go to
LLM providers; credentials arrive as env vars and never do. Interaction requests
(`run.waiting_input`) also take an optional free-text message with allow/deny.

### Added — previews for human approval
A human asked to approve 上線 with nothing but a diff is being asked to
rubber-stamp. The brief for a human-approve stage now instructs the agent to
leave evidence in `._bastet/preview/` (screenshots, an HTML snapshot, a Markdown
summary); Bastet copies it out before the worktree is removed, shows images
inline in the approval panel, and sends them as photos with the Telegram
approval card — which keeps its inline Approve/Reject buttons. A gate with no
preview says so instead of presenting a bare diff as if that were normal.

### Added — PyPI package and a Docker image
`pip install bastet-agent-os` — the wheel carries the built WebUI, so no Node on
the host. `yamantaka520/bastet-agent-os` on Docker Hub: control plane, gateway,
WebUI and bastet-lite in a container (non-root, healthcheck, state in a `/data`
volume); the vendor CLIs stay out of the image deliberately, because their
interactive logins and credentials belong to you, not to a public image. A
release workflow publishes both on every version tag (PyPI via Trusted
Publishing).

### Fixed — first-stage gates judged the wrong directory
Found by the preview tests: the gate and preview collection used a job row read
*before* the run created its worktree, so a first-stage `tests-pass` gate
evaluated in the project repo instead of the worktree the agent had just edited.
The row is re-read after the run.

### Fixed — request models must live at module level
`PUT /api/settings` returned 422 with the body treated as a query parameter:
under `from __future__ import annotations`, FastAPI resolves the stringified
annotation against module globals, and a Pydantic model defined inside
`create_app` silently degrades. Both new models moved out, with a comment that
says why.

## [0.18.4] - 2026-08-01

### Fixed — a big tool result killed the run
`asyncio`'s StreamReader defaults to a 64 KiB line limit, and every CLI executor
reads its `stream-json` output one line at a time. One line carrying a large tool
result — a file read, a long diff, a test log — overran it, `readline()` raised
`LimitOverrunError`, and the run died with `ValueError: Separator is found, but
chunk is longer than limit` after minutes of real work. Nothing in the message
suggests the actual cause, and the stage looked like an executor crash.

All five CLI executors now start their subprocess with a 32 MiB reader limit, and
treat an over-limit line as a dropped progress line rather than a fatal error —
asyncio discards the offending data itself and the stream recovers, so losing one
line beats losing the run. Tests pin the limit, that every executor passes it, and
the recovery behaviour the guard depends on.

Found on the validation host: this is what `job_264bc8d2524a` was actually dying
of, on both attempts.

The first attempt at this fix put the comment at the end of the `limit=` line,
which swallowed `cwd=task.workdir` (and `env=`) into it in three executors — every
run would have executed in the *server's* directory with none of its injected
credentials. The agy and grok tests caught it; codex had no test that would, so
there is now one, and it parses the call's keywords instead of grepping for them:
the swallowed text is still in the file, and only the AST knows it is no longer an
argument.

## [0.18.3] - 2026-07-31

### Fixed — the event registry had drifted by eight types
`EVENT_TYPES` is a registry, not a filter — an unlisted type is still delivered,
because dropping it would silently disable whatever depended on it — but it logs
`unknown event type` every time. Eight types the engine emits were missing,
including `job.rework` and `project.status`, so a healthy rework loop wrote a
warning to the log on every hand-back. `tests/test_events.py` now fails if the
code emits a literal type that is not registered.

### Fixed — a job whose driver died on restart was stuck forever
Restarting the service (to deploy, in this case) kills whatever stage is running.
Startup marked those runs `orphaned` but left the **job** at `in_progress` with
nobody driving it: the project runner only resumes projects that still have
undispatched plan tasks, and `retry` refuses anything that is not blocked. The
card sat on the board looking alive, with no process behind it, and no button in
the product would touch it. Found on the validation host — a live CatsWalker card
had been stuck for half an hour after a deploy restart.

Startup now re-drives interrupted jobs from their current stage, audited as
`job.resumed`. Exceptions, both honest rather than optimistic:

- a **paused or closed** project is not restarted (pause means a human asked for
  it to stop), but its card is blocked with the real reason so the board stops
  claiming work is happening;
- a job whose workflow snapshot cannot be parsed is blocked saying exactly that,
  instead of being fed to a driver that crashes and reports `driver_crashed`.

The project states are read once up front: blocking the first job runs a lifecycle
sync that could move its project out of `paused`, and the next job of the same
project would then be judged against a status this very loop had just changed.

## [0.18.2] - 2026-07-31

### Added — the documentation set
Everything was in a single README and a Chinese SPEC. Split into the shape Agent
Memory OS uses, so the same questions have the same answers in both projects:

- `docs/USER_GUIDE.md` — every tab and every CLI command, end to end
- `docs/INSTALLATION.md` — install.sh flags, executor logins, service, upgrading
- `docs/HISTORY.md` — the project journey, the design log, and the mistakes that
  changed how the code is written
- `docs/ROADMAP.md` — what is next, and what is deliberately not
- `README.zh-Hant.md` — the full README in traditional Chinese
- `PROGRESS.md` — current status, what was verified on the validation host, and
  what is still open
- `COMPATIBILITY.md`, `CONTRIBUTING.md`, `SECURITY.md`

`SPEC.md` gains milestone M6 and design decisions D15–D17 (the rework loop, work
preservation, and run memory for every executor).

## [0.18.1] - 2026-07-31

### Added — a project can be deleted
Trial projects accumulate (a workflow test, a lifecycle probe) and there was no
way to remove one, so the only option was editing the database by hand. Admins
get a delete button on the project card: it takes the project's jobs, runs,
gates, worktrees, role assignments, project-scoped grants and chat sessions. The
audit trail stays — that the project existed is part of the record — and its
team memories stay in AMOS, where the memory tab manages them.

Two refusals, both with the reason: work in flight (deleting rows under a running
job leaves the runner driving a ghost) and runs that spent money (removing usage
rows lowers reported spend). `force` overrides either, and the written-off total
goes into the audit row rather than quietly disappearing.

## [0.18.0] - 2026-07-31

### Added — a failed gate is handled by the agents, not by you
This is what the engine was supposed to do all along. When a `tests-pass` or
`agent-review` gate says no, the card no longer stops: it goes **back** to a
stage that can fix it, carrying the gate's actual output, and the pipeline keeps
running. A reviewer that rejected something cannot fix it, so the work travels
past read-only stages to the last one that writes.

The brief handed to the fixing agent names the shortcuts it must not take —
don't edit the test command, don't delete the test, don't make the assertion
trivially true, don't add skip/xfail, don't touch the workflow config. The
cheapest way to make a gate pass is to weaken the gate, and an agent told only
"make it green" will do exactly that.

Bastet still stops for a human in the three cases where it genuinely cannot
proceed: a stage declared `on_fail: block` (a release step should not be looped
by an agent), a pipeline with no writable stage to return to, and a loop that
has spent its cycles — three by default — without converging. Every hand-back is
audited (`job.rework`) and the count shows on the board card.

An unrunnable test command (`npm ERR! Missing script: "test:e2e"`) now goes back
too, with a brief that says to add the missing script or dependency for real and
to report a genuinely wrong command instead of faking a green exit.

### Fixed — the loop no longer throws away its own output
Found while verifying the above on the live host: a job ran its rework cycle, the
agent correctly changed `a - b` to `a + b`, the gate went green — and cleanup
deleted the fix. `git worktree remove --force` discards uncommitted changes, and
no stage ever commits, so the `bastet/<job>` branch still pointed at the commit
the job started from. The work existed only as a diff file in
`~/.bastet/artifacts`, recoverable by hand.

Cleanup now commits whatever the agents produced onto the job's own branch first,
so the branch really does carry the work (as the docstring had been claiming).
The project's own branch is untouched — merging stays a deliberate step, which is
the part that should keep asking a human. A run that changed nothing makes no
commit.

### Added — 運維處理 and 持續維護 workflow presets
Incident handling (locate → stop the bleeding → root cause → fix with a
regression test → review → deploy → postmortem) and periodic maintenance
(health check → dependency and security updates → regression → tech debt →
review → acceptance), with `ops-engineer` and `maintainer` role prompts. Both
use the rework loop; the steps that touch production keep asking a human.

### Fixed — notifications that said nothing
A blocked job sent `🟠 job.blocked: job_abc stage tests-pass`. It now carries the
task title, the project, the gate, how many reworks were already spent, and the
failing output itself — plus a 🔁 retry button, so a stuck card can be restarted
from the notification. Rework messages are separate and read as progress: what
failed, who is fixing it, cycle N of M, "nothing for you to do". Failing-command
output kept for these is 8000 chars rather than 1000, because 1000 cut off the
assertion.

### Fixed — AMOS was only wired to one executor
Only `bastet-lite` wrote memories, so a project driven by Claude Code, Codex,
Grok or agy contributed nothing and every context pack read from an empty store.
Writing now happens in the orchestrator, the same for every executor: what each
stage did (attributed to that agent's AMOS id), what a gate rejected (as a
`warning` — the most useful memory a run produces), and how the job ended.
Context packs are read *as the running agent*, which turns on AMOS's ACL — an
unscoped `context_pack(query)` was recalling every project's memories into every
project's runs. A memory write can never break a run, and that is now enforced
at the boundary rather than per call site.

### Added — turbovec is visible in maintenance
AMOS's semantic recall needs turbovec + numpy. They arrive with
`agent-memory-os[full]`, but when absent AMOS falls back to keyword matching
without a word of complaint. Both are listed in the maintenance card now, and
the memory tab states which mode is actually live.

### Fixed — UI regressions from the shared control system
Board cards and preset tiles are `<button>` elements, so they inherited
`color: #fff` (white on white) and `white-space: nowrap` (long titles running off
the card instead of wrapping). Both now opt out explicitly. The credentials
textarea rests at the shared control height and grows on focus, so a row of
credential fields lines up. Closed projects are no longer listed in the
project ↔ workflow mapping card.

## [0.17.0] - 2026-07-31

### Added — searching the audit trail
The audit tab showed the latest rows and nothing else, which is unusable the
moment there are thousands. `GET /api/audit` now takes `q` (free text over
actor, action, target and detail), `action`, `actor`, `since`/`until` dates and
`limit`, and returns the categories present so the filter offers real values
rather than a guessed list.

### Added — memory browsing, and the way out to AMOS
Team memory could only be searched, so you had to already know what you were
looking for. The memory tab now lists recent memories by scope with counts, and
links to the full Agent Memory OS console when it is running — with the command
to start it when it is not, instead of a dead link.

### Added — maintenance: check and update every moving part
Bastet orchestrates other people's tools, so "am I current?" is a question
about a dozen things installed in different ways. The Admin tab now lists each
component — Bastet itself, agent-memory-os, the Claude Agent SDK, pytest, and
the claude/codex/grok/agy/hermes CLIs — with its installed and available
version, and updates them individually or all at once.

Nothing self-updates: swapping the agents under a running project is not
something anyone could reason about afterwards. A component whose available
version cannot be determined (an official installer with no version endpoint)
reports `unknown` rather than implying it is current, and an update that ran
cleanly but moved nothing reports `unchanged` rather than claiming success.
Updating Bastet itself says so and asks for a service restart.

### Fixed — chat memory was never actually written
`chat.remember()` called AMOS with `project_id=`/`team_id=` keywords that
`MemoryClient.add` does not accept, and the resulting `TypeError` was caught and
logged at info level. Every planning conversation reported that it had been
remembered and none of it had; recall was broken the same way. AMOS takes the
bare scope (`project`) with the id as a visibility grant (`project:<id>`) —
which is also what gates which agents may recall it — so that is what Bastet
writes now, and a failed write is logged as a warning instead of a shrug.

### Fixed — the memory tab read AMOS records as dictionaries
`search` returns `SearchResult(record, score, reason)` and `list_recent` returns
`MemoryRecord`; both were read with `.get`, which raises on a dataclass. The
failure only appeared where AMOS was actually installed, so it passed tests and
broke on real hosts. Both endpoints now normalise through one function, and show
the visibility grants (the real project/team pointer) instead of a `project_id`
field that never existed.

### Changed — one control system instead of per-page sizes
Buttons, inputs and selects drifted in height, padding and font size from page
to page, so a row of them never lined up. They now share `--control-h`,
`--control-pad`, `--control-radius` and `--control-font`; variants (`ghost`,
`danger`) change emphasis only, chips are the one deliberate exception, and
control rows align on a common baseline. The audit date range got its own
labels instead of borrowing the project page's 更新於之後/建立於之前, and at a
narrow window the header wraps as whole groups (title, tab strip, identity)
with the tabs scrolling sideways rather than stacking into a column of words.

### Changed — board cards say what the task is
A card led with its job id, which identifies a row but tells you nothing. The
task title is now the first line and the id is secondary.

## [0.16.0] - 2026-07-31

### Fixed — automatic continuation, the thing the engine exists for
A live project stopped after its first task and the next one had to be dispatched
by hand. The audit log showed why, and there were two independent causes.

**The runner only ever lived in memory.** A control-plane restart ended a
project's run silently: `reconcile()` parked it as paused, `sync_from_jobs()` saw
work in flight and flipped it straight back to 執行中, and nothing was driving it.
- `ProjectRunner.ensure_running()` starts the loop when a project is running, its
  plan is confirmed, and work remains — idempotent, so it is safe everywhere.
- Startup resumes those projects instead of parking them; a project is parked only
  when there is genuinely nothing to continue, and the audit says so.
- `ProjectRunner.watch()` listens for settled jobs and revives a project whose
  loop died for any reason, so progress recovers by itself.

**An approval request reached nobody.** `_notify_loop` awaited each send
unguarded, so one HTTP error killed every future notification while the poll loop
kept running — the channel still reported `polling` and the job waited forever for
a human who was never told.
- Each notification is guarded; a failure is counted and logged, and the loop
  keeps listening.
- `/api/channels` reports `notify_down` and an error count instead of claiming
  `polling` when only inbound works.
- On start the channel re-announces every job currently blocked on a human, with
  its Approve/Reject card, so a notification lost to a restart is recoverable.
- Approval cards name the work (title, project, stage) rather than a bare job id.

### Changed
- Board cards lead with the task title and a localised status; the job id moves to
  a dimmer third line, so the board reads as work rather than identifiers.

## [0.15.1] - 2026-07-31

### Fixed
- `bastet doctor` reported `pytest` as missing while it sat in Bastet's own venv:
  the CLI never called `augment_path()`, so its view of PATH differed from the
  service's. Every CLI command now prepares the same PATH a gate subprocess gets —
  a health report that disagrees with the thing it reports on is worse than none.

## [0.15.0] - 2026-07-31

### Added — the tools the shipped workflows actually need
The built-in presets run `pytest -q`, `npm test --silent`, `make test` and
`npm run test:e2e` at their test gates, and the installer provided none of them —
so a project could reach its test stage and fail on a missing runner after
spending a full agent run (exactly what happened on the validation host).

- `install.sh` now installs **pytest** into Bastet's venv, since the presets
  depend on it.
- `bastet doctor` reports every program the configured workflows need — built-in
  presets *and* the user's own templates — naming the template that needs each:
  `✗ gate tool `pytest` not found — 內建範本 前後端程式開發 的測試關卡會失敗`.
  Compound commands (`pytest -q && npm test`) contribute both programs; absolute
  paths are not reported, since they need no PATH lookup.
- Bastet's venv bin is added to PATH for gate subprocesses (systemd hands the
  service a minimal PATH, so a runner installed next to `bastet` was invisible) —
  **last**, so a project that provides its own runner still wins.
- README documents that gate commands run on the host with the service PATH, and
  that a project with its own environment should use an explicit path in the
  template.

## [0.14.2] - 2026-07-31

### Fixed
- Retry now picks up a template that was **edited**, not only one that was
  swapped. The refresh compared template ids, so fixing a stage's test command in
  place (same template, new version) left the retry running the old command — the
  single most common reason to retry at all. It now compares the stages and
  reports what it refreshed to (`網頁開發 v2`).

## [0.14.1] - 2026-07-31

### Fixed — a private key could not be entered at all
The SSH check on the validation host reported `error in libcrypto`: the stored key
was 398 bytes on a **single line**. The cause was the form, not the key — the
credential field was a one-line `<input>`, so the browser stripped every newline
out of the pasted PEM block, and ssh rejects a key without them.

- The credential value fields (new credential, edit, and the resource form's
  manual ref) are multi-line textareas that keep line breaks.
- `secrets_store.normalise_private_key()` re-wraps a PEM block that still arrives
  as one line: header and footer make the intended structure unambiguous, so this
  is a repair rather than a guess. Anything without both markers is stored
  untouched instead of being mangled by a guess.

## [0.14.0] - 2026-07-31

### Added — SSH is a first-class git transport, not just a testable one
0.13.0 could *test* an SSH repo but an agent still could not clone it: no key on
disk, no git configured to use it.

- A git resource whose endpoint is an SSH URL now materialises its deploy key for
  the run (0600, under `<home>/run-access/<run_id>/ssh`, deleted when the run
  ends) and exports `BASTET_RES_<NAME>_SSH_KEY`, `_SSH_COMMAND` and `_URL`.
  `GIT_SSH_COMMAND` is set from the first SSH repo so a plain `git clone` works,
  and the per-resource variable lets an agent juggling two repos pick
  deliberately. The prompt note says how to clone — and not to copy the key.
- A trailing newline is added if the pasted key lacks one, because ssh rejects a
  key without it (the classic paste failure), and `IdentitiesOnly` keeps ssh from
  silently succeeding with some other key the agent process will not have.
- An SSH repo with no key configured is advertised as broken instead of looking
  usable.
- The resource form now explains the pairing per transport: HTTPS wants the repo
  URL plus an access token, SSH wants `git@host:group/project.git` plus the full
  private key with its public half registered as a deploy key.

## [0.13.0] - 2026-07-31

### Fixed — git resources are tested the way an agent uses them
A live GitLab resource failed both ways, and each failure was ours:

- **A repo URL is not an API host.** The check appended `/api/v4/user` to
  `https://gitlab.com/user/project.git`. Now the endpoint's shape decides:
  `git ls-remote` (the handshake a clone starts with) for a repo URL or an SSH
  URL, and the provider's identity endpoint only when the URL names a host with
  no project.
- **An SSH private key cannot authenticate HTTPS**, and the checker never used
  the key for SSH at all. A key paired with an HTTPS URL — or a token paired with
  an SSH URL — is now reported as the mismatch it is, before touching the network,
  and the row carries a `git-https-with-ssh-key` problem.
- SSH is tested with the configured key only (`IdentitiesOnly`, `BatchMode`,
  `accept-new`), written to a 0600 temp file and deleted afterwards, with hints
  for the usual causes: public key not added, key format wrong (a missing trailing
  newline is the classic), host unreachable.
- HTTPS sends the token in an env-provided `http.extraHeader`, never in the URL.
- **A bot-check interstitial is not a rejected credential.** A Cloudflare "Just a
  moment…" page now reports as `warn` saying the verdict is unknown, instead of
  blaming the credential.

### Added — editing a workflow template in place
- 我的範本 gains **編輯（就地更新）** next to **另存為新範本**. Until now every edit
  path renamed the template, so there was no way to change an existing one — which
  is exactly what a wrong test command needs. Saving bumps the version; jobs
  already running keep the workflow snapshot they started with, and the builder
  says so while editing.

## [0.12.3] - 2026-07-31

### Fixed
- The config-error distinction is now a persisted flag on `gate_results` (and in
  the audit detail) instead of the UI pattern-matching translated prose — which
  the i18n guard rightly rejected, since the match would have broken in every
  other language.

## [0.12.2] - 2026-07-31

### Fixed
- A `tests-pass` gate whose command cannot run is no longer reported as a failing
  test. `npm ERR! Missing script: "test:e2e"` exits 1 just like a real failure, so
  the pipeline said "tests failed" and the obvious next move was to re-run the
  agent — which can never fix a workflow setting. The gate now recognises an
  unrunnable command (exit 126/127, missing npm script, command not found, no
  such module) and reports it as a configuration problem, naming the command and
  pointing at the Templates tab; the board drawer repeats the distinction, and the
  audit record carries a `config_error` flag.

## [0.12.1] - 2026-07-31

### Fixed
- The *inner* verdict parse was still strict. With 0.12.0's better error message
  the retried review showed grok returning two verdict objects back to back, so
  `json.loads` failed with "Extra data" and the gate still reported "no verdict"
  even though the reviewer had answered. grok, codex and agy now read the verdict
  with `last_json_object()` too — doubled objects, prose, or ``` fences all work,
  and the last answer wins.

## [0.12.0] - 2026-07-31

### Fixed — the stuck review job, and the parsing assumption behind it
A live job blocked at an agent-review gate after two "no structured verdict"
rejections and a crash. One root cause: we assumed every CLI emits one JSON
object per line. grok's `--output-format json` pretty-prints across many lines.

- The verdict was never found, because the parser scanned line by line and no
  single line was a complete object — downstream that read as "the reviewer
  produced no verdict", which correctly rejects, so the stage failed twice.
- A pretty-printed array element (`    "some reason"`) *is* valid JSON — a bare
  string — so `json.loads(line).get(...)` raised
  `AttributeError: 'str' object has no attribute 'get'` and killed the third
  attempt mid-stream.
- Both are now handled once, in `executors/base.py`: `parse_event()` returns a
  dict or None (never a str/list/number), and `last_json_object()` reads the whole
  output, pretty-printed JSON, line-delimited objects, or JSON wrapped in prose
  or ``` fences. grok, agy, codex and claude-code all use them; a test fails if
  an executor goes back to parsing raw lines.
- **A rejected review now quotes what the reviewer actually said.** "No
  structured verdict" with no evidence sent us looking for a logic bug; the cause
  can just as easily be a CLI that is not signed in.

### Added — getting a finished card off the board
- `POST /api/jobs/{id}/archive` hides a done/cancelled card and keeps every run,
  gate and usage row (reversible); the board hides archived cards unless asked.
- `DELETE /api/jobs/{id}` removes a finished card and its runs for good, and is
  **refused when the job spent anything** — usage rows hang off its runs, and
  letting reported spend evaporate on a click has no place in a system built on
  honest accounting. Archive is offered instead. Either way it is audited.
- Deleting unlinks the card from the project plan: a row the dispatch created
  disappears with it, a PM-planned task keeps its text and goes back to
  "not dispatched", because the task still needs doing.
- Both buttons live in the board drawer, with the trade-off spelled out.

## [0.11.1] - 2026-07-31

### Fixed
- A breakdown made *before* provenance existed reported neither a source nor
  staleness, so on the validation host the very plan that started this — seven
  tasks describing a direction the conversation had abandoned — still looked
  authoritative. Such a plan is now marked `unverified` and the card warns that
  there is no way to tell which version of the plan it reflects, with the advice
  to clear it and re-run.
- Staleness compares against the time the breakdown was *taken*
  (`source.at`, stamped only by a decomposition) rather than the plan's last-write
  time, which moves whenever a job is linked and would otherwise mask it.

## [0.11.0] - 2026-07-31

### Added — the task breakdown is traceable to the conversation it came from
The breakdown shown on a project could be a stale snapshot of a conversation
that had since changed direction entirely, with nothing saying so. Now:

- **Provenance.** A breakdown records which conversation it read and how many
  messages existed at the time, shown on the project card as
  "拆分來源：<agent> · <when> · 當時對話 N 則".
- **Staleness.** If the conversation has gained messages since, the plan is
  flagged: this is an old snapshot, re-run it to reflect the actual plan.
  Comparison uses the recorded message count, not timestamps — `now()` has
  second resolution, so a decomposition would otherwise flag itself.
- **Re-running preserves dispatched work.** Only the undispatched proposal is
  replaced; rows already linked to a job keep their link, because losing those
  would cut the plan's connection to running jobs.
- **`DELETE /api/projects/{id}/tasks`** clears a stale proposal while keeping
  every task that already has a job, with a button on the card.
- **Decompose from the chat** (`POST /api/chat/sessions/{id}/decompose`), because
  that is where planning actually finishes: the button sits in the conversation,
  posts a system message recording how many tasks were produced, and the tasks
  land on the project awaiting human confirmation.
- Each plan row shows where its work came from (from chat / by the runner /
  dispatched by hand / PM breakdown) alongside the job's live status.

## [0.10.2] - 2026-07-31

### Fixed
- Reconciliation now heals state that drifted *before* the fix existed. The
  event-driven sync in 0.10.1 only covered work dispatched after it shipped, so a
  job created earlier — or a control plane restarted mid-run — kept a stale light
  and an unlinked plan forever. `reconcile()` links every unlinked job to its
  planned task and re-evaluates the status; it runs at startup, and again when
  the project list or a project's lifecycle is read. Both steps are idempotent and
  only write when something actually changed.

## [0.10.1] - 2026-07-31

### Fixed — the project card now tells the truth about its own work
- **A job was executing while the card still read 規劃中.** Nothing reconciled
  the lifecycle status with reality. `sync_from_jobs()` now runs whenever a job
  is dispatched, blocked, finished or cancelled: work in flight moves the project
  to 執行中 (a new internal `activate` transition, never offered as a UI button so
  it cannot skip the human plan gate), and a project whose every planned task has
  a finished job moves to 維護中. It deliberately does **not** finish a project
  mid-plan — undispatched tasks remaining means the run is not over, which would
  otherwise have stopped a runner after its first task.
- **The task breakdown and the board were two accounts of the same work.**
  `link_job()` attaches a dispatched job to the planned task with the same title
  (appending only when there is no match, never duplicating), and it runs inside
  `Orchestrator.dispatch()`, so chat, board and runner all behave identically.
  Each task now carries its job's live status and stage, shown on the row.
- `DispatchRequest.origin` records where the work came from (chat / runner /
  dispatch) instead of guessing from the actor string.

## [0.10.0] - 2026-07-31

### Fixed — retry actually re-reads the project, chat dispatches join the plan
- **A retry used the state that had already failed.** It now re-snapshots the
  project's *current* workflow (unless that template no longer contains the stage
  the job is parked on, in which case it keeps its own snapshot rather than
  stranding the job) and the spec can be corrected in the drawer before the
  re-run. Repo path, credentials and pool resources were already re-read per run;
  the workflow and the spec were the frozen parts.
- **A chat dispatch was invisible to the project tab.** The job is now recorded
  on the project's task plan (marked `origin: chat`), so the board card and the
  project's task list are the same work rather than two views that disagree.
  Dispatching the same conversation twice does not duplicate the entry.
- The board drawer's retry panel shows the failure reason, an agent picker, the
  editable spec, and a toggle for re-reading the workflow.

## [0.9.9] - 2026-07-31

### Fixed
- Opening a job card white-screened the WebUI. The retry work put a `useEffect`
  after the drawer's early `return null`, so the hook count changed between
  renders (React #310). Hooks now run unconditionally, before the return.
- eslint with `react-hooks/rules-of-hooks` is now part of `npm run build`, and a
  test fails if it is removed. TypeScript cannot see this class of bug and React
  only reports it at runtime, as a blank page — exactly the kind of failure that
  should never reach a user twice.

## [0.9.8] - 2026-07-31

### Fixed
- `GET /api/jobs/{id}` now returns each run's `error` and `executor_type`. The
  drawer is where a stuck card is diagnosed, and the reason was being dropped on
  the way out — the DB knew the repo was not a git repo, the UI showed nothing.

## [0.9.7] - 2026-07-31

### Fixed — the first real dispatch, and everything it exposed
- **Repo paths were never expanded.** A project stored as `~/Github/app` was
  handed to subprocess `cwd` verbatim; something then created a literal
  `~/Github/app` directory, so the agent started in an empty non-repo and died.
  `expand_repo_path()` now expands `~` and `$VARS` everywhere a repo path is
  consumed (dispatch, chat, PM decomposition) and paths are normalised on write.
- **A missing or non-git repo failed confusingly.** `git worktree add` failing
  used to fall through to "run in that directory anyway". Dispatch now refuses
  with the resolved path and what to fix; the worktree fallback only applies to
  a directory that really is a git repo.
- **Repo paths must be absolute, and Windows counts.** `check_repo_path()`
  rejects relative paths with an example for the *server's* platform
  (`C:\Users\you\project` on Windows, `/home/you/project` on POSIX) — the same
  string is not valid on both, and pretending otherwise moves the failure to
  dispatch time. The field now says so in all five languages.
- **A failed run recorded an empty error.** The run now always says why, and the
  codex executor drains stderr so a startup failure (bad cwd, missing auth) is
  reported instead of vanishing.
- **A stuck card had no way forward.** `POST /api/jobs/{id}/retry` re-runs the
  current stage — optionally with a different agent, for when the agent itself
  was the problem — and the board drawer shows the failure reason next to the
  retry button. Only blocked/cancelled jobs can be retried, so a running job
  cannot end up with two drivers.
- **The project path could not be edited.** The form was re-seeded from the
  server on every background refresh, so typing snapped back. Repo path and
  description now keep their own state and adopt server values only while
  untouched — the same discipline as the chat composer.
- Test fixtures now use a real git repo, because that is what dispatch requires.

## [0.9.6] - 2026-07-31

### Fixed
- Long chat messages are no longer cut off while being typed. Two causes, both
  fixed: (1) with a CJK input method Enter commits the candidate being composed,
  and the raw `Enter` handler treated that as "send", firing off a half-typed
  message — a shared `onEnterSubmit()` now ignores composition (`isComposing`,
  `keyCode 229`, `Process`) and Shift+Enter, and every Enter-to-submit box in the
  UI goes through it; (2) the draft lived in the component that reloads on every
  WS event, so a background event arriving mid-composition wiped the characters
  in flight — the composer is now its own component owning its draft, and the
  conversation pauses its background reloads while a message is unsent.
- The message box grows with the text (up to 20rem, then scrolls) instead of
  hiding a long message inside three rows, and attachments can be cleared.
- `tests/test_i18n.py` fails if a raw Enter handler or an externally-owned chat
  draft comes back.

## [0.9.5] - 2026-07-31

### Fixed
- A run where nothing could be dispatched (no agent assigned to the tasks' roles
  and no fallback) left the project sitting in `running` with no work and no
  runner — `maybe_complete` had zero jobs to count. The runner now settles
  honestly: back to `ready`, with `project.runner.idle` in the audit log and a
  hint to assign agents. Found by running the control path on the host.

## [0.9.4] - 2026-07-31

### Fixed
- The decomposition prompt now tells the agent not to use tools: everything it
  needs is inline, and a headless executor cannot prompt for a read permission,
  so a tool call is auto-denied and the whole decomposition fails. (agy on the
  validation host failed exactly that way.)

## [0.9.3] - 2026-07-31

### Fixed
- Executors no longer truncate the run summary at 2000 characters. That cap was
  written when the summary was a human-readable label; chat replies and PM task
  plans *are* that string, so a long answer came back cut mid-sentence and a
  task plan longer than 2KB parsed as nothing. One shared `SUMMARY_LIMIT`
  (200KB) across all seven executors, with a test that fails if the old cap
  comes back. Found by the first real decomposition on the validation host.

## [0.9.2] - 2026-07-31

### Fixed
- `RunResult` has no `.error` field, so the two places that reported a failed
  agent run (chat turn, PM decomposition) crashed with AttributeError on the
  failure path itself. Both now report the status they actually have.
- A decomposition that returns no task list quotes what the agent did say, and
  the raw output is written to the audit log — "did not return JSON" with no
  evidence is the least useful error message in the system.

## [0.9.1] - 2026-07-31

### Fixed
- Task-plan extraction survives a real agent answer. The first live
  decomposition returned a summary object followed by the task object; greedy
  brace matching spanned both and died with "Extra data". It now scans every
  `{`/`[`, `raw_decode`s each candidate and takes the first value that actually
  carries tasks — prose, ```json fences and trailing commentary included.

## [0.9.0] - 2026-07-31

### Added — project lifecycle, PM decomposition, run controls
- Projects have a real state (`projects.status`): planning → ready → running ⇄
  paused → maintenance → closed, reopenable. Only declared transitions are
  allowed, each is audited, and the light in the UI *is* that status rather than
  a guess derived from job rows.
- The project-manager agent turns the agreed plan into tasks
  (`POST /api/projects/{id}/decompose`): read-only, with the repo, the workflow
  stages and the planning conversation in view. It is a **proposal** — a human
  edits and confirms it (`PUT /api/projects/{id}/tasks`) before anything runs.
- `ProjectRunner` then dispatches the confirmed tasks in order; each follows the
  project's workflow and role assignments. A task sitting at a gate keeps the
  runner waiting — it never approves anything itself. When every task settles the
  project moves to maintenance (awaiting acceptance).
- Run controls on the card: ▶ run / ⏸ pause / ■ stop / close / reopen. Pause
  stops the *next* dispatch and lets the current task finish; stop cancels jobs
  in flight (new `Orchestrator.cancel_job` kills the streaming run too — a
  cancelled job with a live run keeps spending tokens). After a restart, a
  project still marked running is parked as paused, because its runner is gone.
- Project tab rebuilt: one collapsible card per project, grouped by status
  (planning / ready / running / paused / maintenance / closed), with keyword and
  date-range search. The body loads only when expanded.
- Decomposed tasks become normal jobs, so they appear on the Kanban board and
  move across stage columns as their status changes.

### Added — user & token management
- Role dropdown backed by `GET /api/user-roles`, which reports what each role
  can and cannot do; `tests/test_users_roles.py` pins those claims against real
  endpoints so the UI cannot oversell.
- Tokens: copy the one-time value, disable/enable, rotate (the old token dies
  the moment the new one is issued), change role in place (effective at once, no
  reissue), delete the user.

## [0.8.0] - 2026-07-31

### Added — Chat: the human input and authorisation channel
- New 對話 tab. A session picks who it talks to — an **agent** (answers through
  its own executor and account, read-only, with the project's repo in view) or a
  pool **LLM resource** (direct metered call) — and a scope: project, team or
  global. Project scope carries the real project state into the prompt:
  description, repo, workflow, team roles, recent jobs and the resource pool it
  may draw on.
- Sessions are stored per project (`chat_sessions` / `chat_messages`) and
  creating one against a project that does not exist is refused, so the
  discussion cannot drift from the org the runs execute against.
- File intake: drop in specs, documents and screenshots. Text-like files are
  inlined into the prompt, images ride along as data URLs / base64 image blocks
  where the wire supports it, everything else is listed honestly as
  "not inlined". Attachments live under `<home>/chat/<session>/` and download
  back through the API.
- Every turn is written to Agent Memory OS in the session's scope, and the
  session recalls from the same scope — so a decision made in chat reaches the
  next run's context pack.
- It can act: pending human-approval gates for the project are listed in the
  session with Approve/Reject, and the whole discussion can be dispatched as a
  job (`POST /api/chat/sessions/{id}/dispatch`). Both are audited. The agent
  never dispatches itself — a person presses the button.
- Telegram becomes a second chat channel: pick a responder and a project for the
  channel on the 管理 tab, and plain messages to the bot are answered in a
  per-user session that survives restarts, with documents and photos saved as
  attachments. `/pair`, `/status`, `/jobs` and inline `/approve` are unchanged.
- New dependency: `python-multipart` (file uploads).

## [0.7.3] - 2026-07-31

### Fixed
- Resource rows no longer print `test: [object Object]`: only declared config
  fields are exposed, with install/test kept as state.
- The test verdict panel renders its newline instead of a literal `\n`.

## [0.7.2] - 2026-07-31

### Fixed
- LLM test summary reads the whole response instead of a 400-character snippet,
  so it reports "credential accepted; 14 models available, e.g. …" rather than
  falling back to raw JSON. Probe internals no longer leak into the stored
  verdict.

## [0.7.1] - 2026-07-31

### Fixed
- The gateway now follows a resource's `secret:<id>` credential pointer. Every
  resource created with the new credential picker would have failed its first
  metered request with "unknown secret ref scheme: 'secret'" — found by testing
  a real resource with the new test button.
- An endpoint stored as a full operation URL (`…/chat/completions`) is flagged
  while editing instead of failing at dispatch: the gateway appends its own
  operation path, so such a resource would request
  `…/chat/completions/v1/chat/completions`. The LLM test probes the base so the
  check is still fair, and warns about the shape even when the credential works.

## [0.7.0] - 2026-07-31

### Added — per-resource test button
- `POST /api/resources/{id}/test` exercises a resource the way an agent will,
  with a check per kind: LLM and media list models (never a completion, so
  testing costs no tokens), API/custom-git endpoints get an authenticated GET,
  MCP servers complete a real `initialize` handshake (stdio spawn or streamable
  HTTP, SSE frames included) and report their name plus tool list, skills are
  looked for on the Bastet host, GitHub/GitLab tokens are verified against the
  provider's identity endpoint.
- Three-state verdict — `ok` / `warn` / `failed`. "Reachable but this path is
  not it" is a different bug from "host is down", and collapsing them into one
  red cross sends people debugging the wrong thing. A rejected credential is
  named as such.
- The verdict, what was checked, and when, are stored on the resource and
  audited, so the WebUI still shows it after a reload.

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
