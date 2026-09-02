# Roadmap

Last updated: 2026-09-02 · current version 0.37.x

M0–M6 are done ([PROGRESS.md](../PROGRESS.md) says what was verified where). What
follows is ordered by how much it would change daily use, not by how interesting
it is to build.

## Shipped since this file was first written

PyPI + Docker Hub distribution (0.19), config-by-conversation and auto-push
(0.20), the media loop and honest retry semantics (0.21), quota self-wait,
Playwright tooling and per-stage time budgets (0.22), durable timezone-aware
scheduled workflows with restart-safe dispatch and overlap suppression, and
preview-first safe pruning of unreferenced managed credential files,
receipt-bound branch review with verified promotion, and timezone-aware project
daily cost ceilings that durably pause and automatically resume the runner.
The 0.36 core redesign adds planning rounds, concurrent task/stage DAGs, visible
PM↔system-analysis negotiation, frozen session intake, challenged handoffs,
role/evidence coverage, verified mainline delivery, Telegram progress/Q&A and
managed Skill contracts. Async media resources can register durable vendor
claims; the host polls, safely downloads the result into the run worktree and
automatically resumes verification after an Agent process has exited. Details in
[CHANGELOG.md](../CHANGELOG.md).

The 0.37 visual acceptance layer adds immutable commit-bound repository browsing,
structured numeric threshold gates, and CI detection of sync-conflict copies that
would otherwise poison generated Web assets or TypeScript type discovery. Raster
artwork is previewed from the same immutable commit only after bounded size and
magic-signature checks; generated-directory conflict copies are safely removed at
both edges of the Web build.

## Under consideration

- **More executors.** The plugin interface exists precisely so this is cheap.
  Candidates are whatever vendors ship next; a new CLI should not touch the engine.
- **Cross-host job placement.** Federation shares the org view; it does not yet
  let one host dispatch onto another's executors.

## Deliberately not

- **Becoming an agent framework.** Bastet delegates execution. Writing another
  agent loop would compete where the vendors are strongest and abandon the reason
  this project exists.
- **A hosted version.** Local-first is the point: your repos, your keys, your
  audit log, your machine.
- **Auto-updating components.** Changing the agents underneath a running project
  is not something anyone could reason about afterwards. Updates stay a button
  press.
- **Letting an agent approve its own side effects.** Deploy, release and merge
  stages keep asking a human, and the rework loop is capped so "self-healing"
  never means "unbounded spend".
