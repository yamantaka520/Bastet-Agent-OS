# Bastet Agent OS Progress

Last updated: 2026-08-31

## Current project status

- Unreleased core-redesign foundation: durable planning rounds with frozen source
  sessions and next-round intake; bounded planning negotiation records; stable task
  ids and validated dependency DAGs; a dependency-aware project runner that claims
  ready nodes concurrently under `max_parallel` and persists `job_deps`; direct
  whole-chat single-card dispatch and project-page decomposition removed; chat
  decomposition gated on a proposed solution; expanded product/system-analysis/
  architecture/UX/UI/visual/integration/release roles. The assigned PM and system
  analyst now negotiate visibly in the customer session: each exchange persists,
  emits a live UI event, survives retries without resetting its lifetime maximum
  of five, and only an explicit system-analysis `accept` unlocks task decomposition.
  Workflow-stage DAG execution is now live: durable ready-node scheduling,
  bounded parallelism, isolated Git branch/worktrees, shared terminal joins,
  restart recovery and branch-local retry. Parallel human gates are approved by
  explicit stage, so one approval cannot accidentally release a sibling or the
  downstream join. A receiving Agent now reviews the latest dependency handoffs
  before its stage starts; challenges alternate with the source for at most five
  durable exchanges and resolve to acceptance, predecessor rework, or human ruling.
  All eight built-in workflow families now use explicit DAGs rather than serial
  lists. Development presets separate system analysis, UX, UI, visual art,
  implementation, integration, security and release roles. Every preset declares
  required typed evidence; the Jobs API and board expose its live evidence matrix.
  A failed graph gate automatically reworks only its writable source subgraph.
  Development workflow sinks now reject `none` and branch-only delivery: the new
  `integration` mode fetches the current remote target, merges, runs its trusted
  candidate gate, pushes without force, and verifies the remote receipt SHA;
  `production` retains version tagging, deployment, and online verification.
  Telegram now renders graph-node starts/passes, handoff reviews/challenges,
  delivery transitions and evidence-grounded completion summaries. `/job` reads
  the durable task snapshot; `/ask` gives the configured responder a task-scoped
  system context. Parallel human gates receive distinct run-bound callback tokens.
  Validation: 595 non-loopback plus 15 loopback Python tests (610 total), ruff clean.

- Released: **v0.35.2**. Version arc v0.1.0 → v0.35.2 in one month
  (2026-07-28 → 2026-08-30), ~132 commits. See [CHANGELOG.md](CHANGELOG.md) for
  the full trail and [docs/HISTORY.md](docs/HISTORY.md) for why each decision
  went the way it did.
- v0.35.0–v0.35.2 make delivery part of the completion contract: required branches
  must reach the remote, and production cards must publish main plus an
  immutable version tag, deploy, and verify the exact live commit before
  `job.done` can exist. Failed delivery remains blocked and resumable with a
  durable receipt instead of rerunning already-accepted Agent stages. v0.35.1
  additionally guarantees the pre-deploy gate passes on the merged candidate
  before either the target branch or release tag is published; v0.35.2 preserves
  an existing Bastet-owned repair worktree and its regenerated evidence.
- v0.34.17 reuses a passed reviewer precheck after unrelated executor failure
  when its command, clean HEAD, and audit evidence still match; v0.34.16 carries
  the Pi account's last interactively proven provider/model
  route into unattended card runs when the Agent has no explicit model;
  v0.34.15 reuses identical repair/precheck evidence at the same clean HEAD;
  v0.34.14 carries repair evidence through PM/incident retries; v0.34.13 makes
  exhausted PM recovery evidence-aware instead of permanently
  count-latched; v0.34.12 closes the rework loop with mandatory original-gate verification
  before re-review and durable immediate PM diagnosis; v0.34.11 makes Pi
  account credentials deterministic for extension providers;
  v0.34.10 safely loads account-profile Pi provider packages while keeping
  repository extensions disabled and resolves the exact provider/model route.
- v0.34.9 adds model-specific Pi credential admission, non-blocking E2E gates,
  per-Agent login/model terminals, and atomic editable Agent ids.
- v0.32.0 adds a durable maintenance/drain fence, explicit handoff delivery and
  acknowledgement, and persistent context golden-case evaluation.
- Milestones M0–M6 complete, plus three hardening arcs: 08-02 → 08-06 (the media
  loop, quota self-wait, per-stage time budgets, retry semantics that respect
  human intent), 08-16 → 08-17 (headless runs cannot be prompted — stdin is
  closed and the env says CI; every run heartbeats even when its executor is
  silent, and the board separates "alive" from "talking"), and **08-19 → 08-22**
  — the arc where one card exposed six defects in a row and each fix was
  verified on it:
  1. **PM-level supervision** (0.25.0): a card blocked for a *business* reason
     is diagnosed by the project's PM agent, which retries, hands the stage
     over, files a ruling into the job inbox, or escalates with an answerable
     question. Two audit-counted interventions per episode; only an explicit
     recovery-lease renewal reopens the budget; human gates and quota waits are
     never touched.
  2. **A depleted agent leaves the rotation** (0.26.0/0.26.1): a vendor's
     `402 … balance exhausted` marks the agent, and every routing path skips
     it — otherwise the router kept re-dispatching a dead agent and undoing the
     PM's correct handovers.
  3. **Rework walks backwards** (0.27.0): the hand-back target advanced instead
     of standing still, so a failing test finally reached someone who could fix
     the code rather than the tester who kept re-running it.
  4. **The card asks what the PM asks** (0.28.0): escalations are the last
     resort and are shown on the card as an answerable question with one button
     that files the ruling and retries.
  5. **Every stage commits; scratch never does** (0.29.0/0.30.0): stage
     boundaries commit to the job branch, `._bastet/` is excluded, and a
     sandboxed agent can use git in its worktree again (0.29.1).
  6. **Interrupt the dead, not the merely quiet** (0.30.0): liveness decides
     interruption, so a 20-minute test is no longer executed at the 15-minute
     silence mark.
- Test suite: **601 passing**, `ruff` clean; CI green on Linux/macOS and
  inside the shipped Docker base image (Windows legs are declarative).
- Releases are automated: a `v*` tag publishes to PyPI (Trusted Publishing) and
  pushes the multi-arch image to Docker Hub.
- Distribution: [PyPI](https://pypi.org/project/bastet-agent-os/) (wheel carries
  the built WebUI — no Node on the host) and
  [Docker Hub](https://hub.docker.com/r/yamantaka520/bastet-agent-os)
  (amd64+arm64, chromium included for Playwright).
- Executors: `claude-code`, `claude-sdk`, `codex`, `grok`, `agy`, `hermes`,
  `pi`, `openclaw`, `bastet-lite`. Standard tooling tracked by the maintenance card: pytest,
  Pillow, Playwright (+chromium), turbovec.
- Resource kinds: llm, mcp, api, skill, git, image, video, music, tts, stt and
  **model3d** (3D model/animation generation); a resource can be reclassified
  when a truer category arrives after it.
- WebUI in five languages, typed against a canonical dictionary so a missing
  translation fails the build.

## Validation deployment

Bastet runs as a systemd **user** service on a second machine, where every
feature is confirmed against real vendor CLIs before release:

| | |
|---|---|
| Host | Ubuntu 26.04 LTS, Python 3.14.4 |
| Install | `~/.bastet/venv`, from PyPI |
| Service | `systemctl --user` unit, `bastet serve` on `0.0.0.0:8890` |
| Executors | claude 2.1.2xx, codex 0.147.x, grok 0.2.1xx, agy 1.0.1x, hermes 0.19+ |
| Memory | Agent Memory OS 1.8.1 with turbovec — semantic recall **active** |
| Live project | CatsWalker (a real Three.js game), driven end-to-end through the workflow engine |

### Verified there, end to end

- **The 08-19 → 08-22 arc, on one card** (INT-01, a five-branch integration):
  each fix above was confirmed against it in production, in order — Grok1 taken
  out of rotation on a real `402`; the PM diagnosing and handing over twice; the
  hand-back reaching 頁面實作 instead of looping on the tester; the reviewer
  approving once evidence could bind to a commit; stage commits appearing on the
  branch (`bastet(<stage>): …`) and the agent then committing on its own,
  including a change that made its E2E log bind to HEAD automatically — the
  first thing it could not have done while the sandbox blocked git.

- **The rework loop**, across many real cards: failed tests and rejected reviews
  hand back, converge, and the pipeline finishes without a human step; honest
  stops when they cannot (cycles spent, unverifiable criteria).
- **Work preservation and delivery**: every completion commits to
  `bastet/<job_id>` and pushes to GitLab through the project's granted
  credential — both the driver-loop and human-approval completion paths.
- **Quota self-wait**: `resets 1:30am (Asia/Taipei)` parsed, card resumed itself
  after the vendor's clock passed.
- **The media loop**: a chat agent read vendor docs (WebFetch), proposed a
  conforming resource (bastet-config), the human applied it, the agent generated
  a real image through the granted credential, self-corrected on a real API
  constraint, and the file rendered inline in the conversation. Workflow cards
  produced 52-PNG sprite sets, Playwright viewport screenshots for approvals,
  and 3D-integration builds — all delivered as branches.
- **Restart recovery, heartbeats, supplies, previews, timezone display** — each
  validated by the incident that motivated it (see docs/HISTORY.md).

## Open items

- **Async media fetcher**: generation that outlives its run has no background
  claimer; the rule today is poll-to-completion inside the run.
- **Scheduled workflows**: the 持續維護 preset wants a cron-like trigger.
- **Merge assistance**: finished branches are reviewed/merged by hand (the push
  output carries the MR link). Live shape of the gap: an integration card can
  accumulate a dozen commits on `bastet/<job_id>` while `main` stays where it
  was, and nothing in the engine will move it — by design, since merging is an
  authority decision, but the last mile is still manual.
- **`~/.bastet/secrets` accumulates** rotated credential files.
- **Telegram bot token** from an early session should still be rotated.
- **A one-agent role dead-ends less gracefully than it should.** Dispatch now
  falls back to any funded agent on the project, but the stand-in may hold a
  very different role; a per-role stand-in list would be honest about it.
- **The E2E stage's own time budget is undeclared** on the live 網頁開發 preset.
  Since 0.30.0 a silent stage is bounded by `timeout_s` rather than by the
  supervisor's patience, so long suites (20-level FPS runs) should say how long
  they may take.
- **The Windows CI leg is red by declaration** (`continue-on-error`): ~35 tests
  assume POSIX fake-executor scripts, forward-slash paths and 0600 bits. Real
  Windows support needs its own pass.
- **GitHub's hosted runners occasionally never pick up a job** ("not acquired by
  Runner of type hosted"), which cancels it at ~15 min and reddens the run. Not
  a repository failure — re-run the job.

## Ground rules this project holds itself to

Enforced by tests, reviewed on every change:

1. **Never claim more than was verified.** `unknown` beats a guessed "current";
   `unchanged` beats a claimed "updated"; a gate that could not run is a
   configuration problem, not a failing test.
2. **Accounting cannot be quietly reduced.** Deletions with usage rows refuse or
   disclose the written-off amount, on the record.
3. **A memory write can never break a run** — enforced at the module boundary.
4. **An agent must not be able to pass a gate by weakening it** — the shortcuts
   are named in every rework brief.
5. **Side effects ask a human.** Deploys, releases, merges, and anything that
   changes who-can-act stay behind human gates; the model proposes,
   the click is the authority.
