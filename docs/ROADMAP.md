# Roadmap

Last updated: 2026-08-30 · current version 0.35.x

M0–M6 are done ([PROGRESS.md](../PROGRESS.md) says what was verified where). What
follows is ordered by how much it would change daily use, not by how interesting
it is to build.

## Next

- **Core engine redesign (accepted 2026-08-30).** Replace the linear project
  runner and stage array with durable planning rounds plus validated task/stage
  DAGs; add bounded visible PM↔system-analysis negotiation, frozen planning
  sessions with next-round intake, adversarial handoff challenges, role/evidence
  coverage, mandatory development integration receipts, Telegram progress/Q&A,
  and schedulable Skill installation. See
  [CORE-ENGINE-REDESIGN.md](CORE-ENGINE-REDESIGN.md).

- **Prune `~/.bastet/secrets`.** Rotated credential files accumulate; nothing
  removes the superseded ones.
- **Cost ceilings that stop work.** Budgets are recorded per grant and enforced
  at dispatch; a per-project daily ceiling that pauses the runner (rather than
  failing a run) is a better shape for the same intent.
- **Review UI for branch deliveries.** A normal `branch` delivery still leaves
  reviewable work on `bastet/<job_id>` for a deliberate merge; a diff view with
  a merge button belongs on the board. Explicit `production` delivery is now a
  separate audited path that atomically publishes the target and release tag.

- **Async media fetcher.** Media stages must currently poll a vendor's async
  generation to completion within their own run. A background fetcher that
  claims the result later (before the URL expires) and files it into the
  project would lift that limit.

## Shipped since this file was first written

PyPI + Docker Hub distribution (0.19), config-by-conversation and auto-push
(0.20), the media loop and honest retry semantics (0.21), quota self-wait,
Playwright tooling and per-stage time budgets (0.22), and durable timezone-aware
scheduled workflows with restart-safe dispatch and overlap suppression (next
release after 0.35.2). Details in
[CHANGELOG.md](../CHANGELOG.md).

## Under consideration

- **More executors.** The plugin interface exists precisely so this is cheap.
  Candidates are whatever vendors ship next; a new CLI should not touch the engine.
- **Cross-host job placement.** Federation shares the org view; it does not yet
  let one host dispatch onto another's executors.
- **Web-based repo browsing.** Today you inspect a worktree over SSH. A read-only
  file view in the drawer would close the loop for reviewing what an agent did.
- **Richer gate types.** A `metric-threshold` gate (coverage, bundle size,
  latency) is the obvious next one after `tests-pass`.

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
