# Core Engine Redesign

Status: implementation in progress; foundation slice landed  
Date: 2026-08-30

## Why this is a redesign

The validation project has repeatedly exposed a structural mismatch between what
Bastet promises and what its data model can express. Project execution is still a
linear task loop; workflow stages are a linear array; planning chat has no round
lifecycle; role coverage is generic; delivery records that a branch was pushed but
does not make integration into the target branch a normal completion invariant.
Fixing individual retry paths does not close those gaps.

This document replaces patch-led evolution with explicit engine invariants. New
work should be rejected when it weakens one of them.

## Core invariants

1. **A project is a sequence of planning rounds, not one endless chat.** A round
   moves through `discovery -> analysis -> proposed -> approved -> frozen ->
   executing -> accepted`. Once approved, its source conversation is immutable.
2. **One active execution round per project.** While its task graph is unsettled,
   no later round may enter discovery. New defects, ideas and suggestions go to a
   durable intake queue and are copied into the next round.
3. **Planning is PM plus system analysis.** The PM owns customer communication;
   a system analyst challenges feasibility, boundaries, dependencies, risks and
   acceptance criteria. Their visible, bounded negotiation must converge or
   escalate within five exchanges before a proposal can become ready.
4. **The plan is a validated DAG.** Every task has a stable id, dependencies,
   produced/consumed artifacts, role, capabilities, acceptance evidence and
   delivery contract. Acyclic validation and dependency closure happen before
   approval. Ready nodes run concurrently up to project and resource limits.
5. **Stages may also form a DAG.** Workflow templates declare `needs`, artifacts
   and join strategy. UI/UX, visual design and core implementation can begin after
   a shared architecture contract and join at integration; list position alone
   never implies a dependency.
6. **Handoffs are adversarial acceptance, not summaries.** A receiving agent must
   inspect evidence and may raise bounded challenges. Up to five exchanges are
   recorded; unresolved challenges block or return to a writable predecessor.
7. **Evidence is typed and attributable.** Functional tests are only one class.
   Architecture, UX, visual, accessibility, security, performance, operations,
   delivery and Git-integrity evidence are first-class gates where relevant.
8. **Role coverage is selected by work shape.** The minimum software role catalog
   includes product planner, PM, system analyst/architect, UX researcher, UI
   designer, visual artist, frontend, backend/core, integrator, QA, security,
   operations and release/integration maintainer. Templates declare required and
   optional roles and refuse approval when required coverage is missing.
9. **Development completion includes integration hygiene.** A development task is
   not complete merely because a remote feature branch exists. The declared
   delivery policy must prove clean worktree, commits on the owned branch, review,
   up-to-date target, conflict resolution, required gates on the merge candidate,
   integration into the target branch, remote confirmation and immutable receipt.
10. **Progress is observable and interactive.** Web and Telegram receive graph
    progress, stage summaries, blocks, challenge exchanges and round summaries.
    Authorized users can ask about a task and receive an answer grounded in its
    current run, handoffs and evidence.
11. **Skills are schedulable capabilities.** A skill has source, version/digest,
    compatible executor, install target, install/test command, health state and
    scope. Plans can require skills; admission either resolves an installed,
    healthy grant or creates an explicit supply/install block before dispatch.
12. **No automatic recovery may manufacture authority or infinite work.** Human
    approval remains required for material side effects, while retry, challenge
    and PM/SA exchanges are bounded and auditable.

## Target model

### Planning round

Each project owns ordered `planning_rounds`. A round references one primary
customer session, its immutable final summary, the PM/SA negotiation transcript,
the approved task graph and the acceptance result. `chat_sessions.state` is one
of `open`, `frozen` or `intake`; frozen sessions reject new messages. Intake
items are append-only and are consumed by exactly one later round.

The task-breakdown action is enabled only when the round is `proposed`, the PM/SA
negotiation has converged, a concrete solution is present and required role/
capability coverage passes. The direct "turn this chat into one job" endpoint is
removed. Project-page decomposition is also removed; that page reviews and
approves the graph produced by the planning round.

### Project task graph

Every task node declares a stable id, `needs`, role, required capabilities,
consumed and produced artifacts, acceptance evidence, and delivery policy. The
scheduler claims all ready nodes atomically, limited by project concurrency,
resource grants and conflicting write scopes. A failed prerequisite blocks its
descendants without preventing independent branches from progressing. Restart
recovery reconstructs readiness from durable node/job state rather than an
in-memory loop cursor.

### Workflow graph and challenge protocol

Workflow stages use the same dependency vocabulary. Each edge carries an artifact
contract. Before work starts, the receiver either accepts predecessor evidence or
opens a structured challenge containing a claim, evidence gap and requested
resolution. The predecessor responds; after five messages the engine requires a
decision (`accepted`, `rework`, or `human_ruling`). The transcript becomes part of
the handoff and is visible in Web and Telegram.

### Template families

The library is organized by work shape rather than a generic development pipe:
product discovery/system design, full-stack, frontend/UI, visual assets, backend,
mobile, integration/migration, defect/incident, security, operations/release,
research/content/media and recurring maintenance. Each family defines required
roles, parallel branches, joins, evidence, Git/delivery policy and Skill needs.

Template linting rejects missing joins, cycles, unreachable nodes, unsatisfied
artifacts, self-review, writable review stages and development templates without
integration delivery.

## Delivery sequence

1. **Foundation (implemented, unreleased):** durable planning rounds/intake,
   immutable sessions, task ids/dependencies, DAG validation and lifecycle APIs.
2. **Scheduler (project task DAG implemented, stage DAG pending):** concurrent
   ready-node claiming, dependency persistence, failure propagation, restart
   recovery, per-project and per-resource limits.
3. **Planning (negotiation implemented, coverage lint pending):** visible,
   persisted PM/system-analysis negotiation with a lifetime five-exchange cap;
   solution readiness and server-side decomposition gate; role/capability
   coverage lint remains pending.
4. **Workflow graph (contract implemented, runtime pending):** stage dependency,
   artifact, workspace-isolation and challenge contracts now validate and persist;
   legacy lists normalize to linear dependencies. The runtime still needs to
   schedule isolated stage worktrees and join their outputs before built-in
   templates can safely become parallel graphs. Until then, dispatch admission
   explicitly rejects non-linear stage graphs instead of silently running list
   order through the legacy single-cursor driver.
5. **Evidence/delivery:** evidence matrix and mandatory merge/integration receipt
   for development work.
6. **Experience:** graph UI, frozen/intake conversations, Telegram progress and
   grounded questions, Skill install/supply wizard.

The first slice also removes the whole-chat single-card dispatch API (HTTP 410),
removes project-page PM decomposition, gates chat decomposition on a `proposed`
round, exposes frozen/intake UI, and expands the role catalog with product
planning, system analysis, architecture, UX, UI, visual art, integration and
release management. The second slice runs the assigned PM and system analyst as
separate read-only agents, persists and displays each proposal/challenge, emits
live exchange events, resumes without resetting the five-exchange lifetime cap,
and unlocks decomposition only after an `accept` verdict. The third slice adds
cycle/dependency/artifact/parallel-write linting, durable per-stage node state,
template-editor graph fields, and structured handoff challenges. Only a receiving
agent can open or accept a challenge; source and receiver responses are recorded
in the project room, bounded at five exchanges, and unresolved discussions become
`human_ruling`. This is the admission and persistence layer, not yet a claim that
the stage runtime executes parallel writable branches.

No phase is shipped solely from unit tests. It needs migration, restart and race
tests, a real multi-branch rehearsal, and an end-to-end receipt proving the target
branch contains the result.

## Requirement traceability

The fifteen user concerns map respectively to: redesign invariants; task/stage
DAGs; role and evidence coverage; planning-round lifecycle; visible bounded
PM/system-analysis negotiation; proposal-gated breakdown; removal of whole-chat
single-card dispatch; removal of project-page decomposition; parallel stage DAGs;
bounded handoff challenges; template-family linting; Telegram progress/questions;
integration delivery; migration-level acceptance; and schedulable Skill install.
