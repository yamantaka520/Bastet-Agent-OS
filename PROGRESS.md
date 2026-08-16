# Bastet Agent OS Progress

Last updated: 2026-08-17

## Current project status

- Released: **v0.24.0**. Version arc v0.1.0 → v0.24.0 in three weeks
  (2026-07-28 → 2026-08-17), ~110 commits. See [CHANGELOG.md](CHANGELOG.md) for
  the full trail and [docs/HISTORY.md](docs/HISTORY.md) for why each decision
  went the way it did.
- Milestones M0–M6 complete, plus two hardening arcs: 08-02 → 08-06 (the media
  loop, quota self-wait, per-stage time budgets, retry semantics that respect
  human intent) and 08-16 → 08-17 (headless runs cannot be prompted — stdin is
  closed and the env says CI; every run heartbeats even when its executor is
  silent, and the board separates "alive" from "talking").
- Test suite: **414 passing**, `ruff` clean; CI green on Linux/macOS and
  inside the shipped Docker base image (Windows legs are declarative).
- Distribution: [PyPI](https://pypi.org/project/bastet-agent-os/) (wheel carries
  the built WebUI — no Node on the host) and
  [Docker Hub](https://hub.docker.com/r/yamantaka520/bastet-agent-os)
  (amd64+arm64, chromium included for Playwright).
- Executors: `claude-code`, `claude-sdk`, `codex`, `grok`, `agy`, `hermes`,
  `bastet-lite`. Standard tooling tracked by the maintenance card: pytest,
  Pillow, Playwright (+chromium), turbovec.
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
| Executors | claude 2.1.2xx, codex 0.145+, grok 0.2.1xx, agy 1.1.x, hermes 0.19+ |
| Memory | Agent Memory OS 1.8.1 with turbovec — semantic recall **active** |
| Live project | CatsWalker (a real Three.js game), driven end-to-end through the workflow engine |

### Verified there, end to end

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
  output carries the MR link).
- **`~/.bastet/secrets` accumulates** rotated credential files.
- **Telegram bot token** from an early session should still be rotated.
- **Release workflow secrets** (PyPI Trusted Publisher + Docker Hub) not yet
  configured — releases are published manually. The workflow no longer goes red
  over it: the tag/version check and the wheel build still run, and publishing
  is gated on `vars.PUBLISH_TO_PYPI` / the Docker Hub secrets, reporting a
  skip notice instead of a failure — and the `pypi` environment job only exists
  when the switch is on, so no failed deployment record appears either.
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
