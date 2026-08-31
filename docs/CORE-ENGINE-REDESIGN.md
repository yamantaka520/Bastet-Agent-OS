# Core Engine Redesign

Status: implementation in progress; foundation slice landed  
Date: 2026-08-31

## Why this is a redesign

The validation project has repeatedly exposed a structural mismatch between what
Bastet promises and what its data model can express. Project execution is still a
legacy task/workflow lists cannot express all desired graph semantics; role and
evidence coverage are still being migrated; delivery previously recorded only a
parked branch rather than making target integration a normal completion invariant.
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

## Implemented delivery invariant

Development preset sinks now freeze `delivery_modes: [integration, production]`
into the job workflow snapshot. Dispatch rejects `none` and branch-only contracts
for those workflows. `integration` uses the same trusted merge-candidate path as a
production release without deploying: fetch the current remote target, merge,
run the configured pre-deploy gate, push without force, then read the remote ref
back and require its SHA to equal the immutable delivery receipt. Production adds
version-source validation, an atomic version tag, deployment, and online commit
verification. The verifier must emit a structured receipt with `status=verified`
and the exact target, version and commit SHA; a successful process exit alone is
not deployment evidence.

## Implemented Telegram task observability

Telegram notifications now consume the same durable job graph used by the Web
board. Node start/pass, handoff review and challenge, delivery pending/success/
failure, and final completion messages include graph counts and persisted evidence
rather than an opaque job id. `/job <job_id>` returns the current nodes, evidence,
open challenges, latest error/summary and delivery receipt. `/ask <job_id> <question>`
uses a separate task-scoped chat session whose system prompt embeds that trusted
snapshot. Project-bound channels reject cross-project queries and notifications.
Parallel human gates use run-bound callback tokens so each Telegram approval names
one exact stage.

## Implemented Skill supply contract

`skill:<id>` is now a schedulable workflow requirement rather than prompt text.
A managed Skill resource declares its stable id, version, source, install target,
expected SHA-256, compatible executors, explicit admin-run install command and an
optional health command. Installation is successful only after the target exists,
its deterministic file/tree digest matches and the health command passes; those
receipts persist with the resource. Project grants determine visibility.

Before an Agent starts, stage admission resolves every required Skill against an
enabled, granted, installed, healthy and executor-compatible resource. A miss
creates a durable `skill.supply_required` block and Telegram instruction without
running the Agent or consuming rework. Only verified managed Skills are exposed to
an executor. Legacy source-only Skill resources remain compatible as prompt assets,
but cannot satisfy a workflow capability contract.

## Implemented whole-graph admission

One structured admission report now evaluates the complete project task graph and
every stage in its selected workflow before confirmation, project start/restart,
or direct job creation. Declared task and stage roles must have enabled, funded
assignments; every stage needs at least one executor whose direct/Gateway route is
compatible; host capabilities must be known and have a trusted delivery path; and
managed Skills must resolve for the exact candidate executor. The report identifies
the task, stage, role, requirement and rejected candidates rather than returning a
generic routing failure.

The conversation's task-breakdown action remains disabled after PM/SA agreement if
workflow admission is not ready, and shows the concrete blockers. The project page
uses the same report. Confirmation rejects invented/unassigned task roles; start is
admitted before the lifecycle moves to `running`; restart recovery refuses an
inadmissible graph; direct dispatch scans later stages before inserting a job.
Runtime checks remain as drift protection when a resource or agent changes after
admission.

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

1. **Foundation (released in 0.36.0):** durable planning rounds/intake,
   immutable sessions, task ids/dependencies, DAG validation and lifecycle APIs.
2. **Scheduler (project and stage DAG foundations implemented):** concurrent
   ready-node claiming, dependency persistence, failure propagation, restart
   recovery, per-project and per-resource limits.
3. **Planning (implemented foundation):** visible,
   persisted PM/system-analysis negotiation with a lifetime five-exchange cap;
   solution readiness and server-side decomposition gate; role/capability
   coverage lint blocks task breakdown/confirmation until declared roles,
   executor routes and Skill requirements are satisfiable.
4. **Workflow graph (runtime foundation implemented):** stage dependency,
   artifact, workspace-isolation and challenge contracts validate and persist;
   legacy lists normalize to linear dependencies. A branching job now claims all
   ready nodes up to `stage_max_parallel`, provisions separate Git branches and
   worktrees for unordered nodes, applies per-agent and resource concurrency
   limits, commits each passed output, and merges dependency heads at one shared
   terminal join. Conflicts block without leaving a half-merged tree. Restart
   recovery returns orphaned nodes to ready; retry invalidates only the failed
   branch and its descendants; completed checkouts are removed while branches
   and commit receipts remain. `human-approve` is attributable per node, and a
   sibling approval cannot release a downstream join early. Before a challenged
   node starts, its receiver reviews the latest handoff from every dependency;
   source and receiver then alternate for at most five durable exchanges,
   resulting in acceptance, predecessor rework, or human ruling.
5. **Evidence/delivery (implemented):**
   every built-in family declares required evidence dimensions and assigns each
   dimension to a non-auto gate. Job detail exposes the frozen stage, gate,
   verdict, run and commit for every evidence row. Mandatory target-main and
   remote receipts are enforced by the integration and production delivery modes.
6. **Experience (implemented foundation):** graph UI, frozen/intake conversations,
   Telegram progress and grounded questions, verified Skill install/supply form
   and stage admission.

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
`human_ruling`. The fourth slice connects durable stage nodes to the orchestrator,
runs isolated writable siblings concurrently, joins them into the job branch,
exposes node state in the Jobs API and board, and emits live node lifecycle events.
The fifth and sixth slices add graph-node human approval and automatic, restart-
durable receiver/source challenge turns before execution. Rework resets the source
subgraph; an unresolved fifth exchange blocks for human ruling. The seventh slice
migrates all eight built-in families to explicit DAGs, separates system-analysis,
UX, UI, visual-art, implementation, integration, security and release roles in
development work, and adds typed evidence contracts plus the live job evidence
matrix. Graph gate rejection now performs bounded source-subgraph rework while
preserving passed siblings. The eighth slice enforces integration/production
delivery with trusted candidate gates and remote target receipts. The ninth adds
Telegram graph progress, grounded `/job` and `/ask`, and run-bound approvals. The
tenth makes managed Skills installable, digest/health verified and resolvable as
`skill:<id>` stage admission contracts; missing supply blocks without consuming
Agent time or rework. The eleventh centralizes whole-graph admission across chat,
plan confirmation, project start/restart and direct dispatch, and removes the
runner's silent fallback when a task explicitly names an unassigned role. The
twelfth makes dispatch and stage ownership database invariants: a frozen plan node
has one durable `(project, plan, task) -> job` receipt committed atomically with
its job and stage graph, while every stage crosses `ready -> running` through a
compare-and-set before workspace or executor side effects. Duplicate runners reuse
the receipt, competing drivers lose the node CAS cleanly, restart recovery reclaims
only orphaned nodes, and a maintenance fence keeps project runners parked but alive
until release. Strict claiming also invalidates the complete downstream subgraph
when a linear ruling restarts at its writable rework target.

The thirteenth closes the remaining process-local supervisor claim. PM diagnosis
uses a renewable SQLite execution lease, so two server processes cannot spend two
Agents on the same blocked card; a killed owner yields after lease expiry. The
`bastet reliability-rehearsal` acceptance command starts fresh OS processes with
independent database connections and proves: one atomic task dispatch receipt,
one stage CAS winner, recovery after terminating a stage owner before run creation,
one PM-diagnosis winner, and expired-lease reclamation. It operates only on a
temporary Bastet home and temporary Git repository.

The fourteenth closes the release-path acceptance gap. `bastet delivery-rehearsal`
creates a real bare remote and a nonlinear workflow whose UI and core roots execute
concurrently in separate Git worktrees. Their commits pass reviewed handoffs into a
terminal join. A separate clone advances remote main before delivery, forcing the
integration path to merge the current remote target rather than a stale local main.
The receipt is accepted only when the remote target SHA exactly matches it and its
tree contains both parallel results, the join result, and the concurrent remote change.

The fifteenth removes the last implicit production-verification claim. A trusted
`verify_command` must now emit a JSON provider receipt whose `status`, `target`,
`version`, and `commit_sha` are bound to the delivery contract and freshly integrated
commit. The engine parses and compares every field; empty output, legacy exit-zero-only
checks, stale provider commits, wrong versions, and wrong targets block the card and
cannot produce `job.deployed`. Incomplete production profiles are rejected before a
job is created, so Agent work is not spent on an impossible delivery contract.

The sixteenth makes that provider boundary executable rather than documentary.
`bastet production-rehearsal` builds two real release cards against temporary Git
and a local HTTP provider. The first atomically publishes main plus `v1.4.0`, runs
the deployment command, fetches the provider's live JSON over HTTP, and binds the
successful delivery receipt to that response. The second publishes `v1.4.1` but
deliberately leaves the provider on `v1.4.0`; the online receipt mismatch must keep
the card blocked and produce no `job.deployed` audit. The command touches no configured
project, remote, Bastet home, or external production service.

The seventeenth extends delivery beyond synchronous web deployments. App Store
Connect and Google Play profiles declare provider identity, app/package identity,
release goal (`uploaded`, `submitted`, `approved`, or `published`) and poll interval.
Their unique terminal stage must be `human-approve`; upload or review submission
cannot be hidden behind an automatic gate. A receipt below the declared goal moves
the card to durable `waiting_external`, stores raw provider status and schedules a
CAS-protected five-minute verification lease without rerunning build, upload,
submission or Agent work. Startup immediately reclaims a crashed poll owner; a second
live server reclaims it after lease expiry and continues from the same receipt.
Published completes once; rejected blocks and never emits `job.deployed`.

The eighteenth replaces environment-specific status scripts with optional built-in
official API adapters. A trusted upload command must first emit a structured submission
receipt bound to the integrated commit, version, target and provider identity, plus the
exact Apple `app_store_version_id` or Google `version_code`. Only then does Bastet sign
an App Store Connect ES256 JWT or Google service-account RS256 assertion and query that
exact object. Apple verifies the returned version belongs to the configured app. Google
creates an uncommitted read edit, selects exactly one matching versionCode on the chosen
track, and deletes the edit without commit. Credentials enter only through granted
secret environment variables; neither profile nor evidence contains them. Unknown
provider states, ambiguous versions, wrong app relationships, missing credentials and
HTTP failures all fail closed.

The nineteenth adds `bastet store-canary` as the credentialed acceptance boundary.
Job mode reloads the frozen profile, integrated SHA and durable submission receipt,
resolves only the exact store credential variables granted to that project/team, and
performs one official status read without mutating job or delivery state. Project/file
mode supports preflight of an existing TestFlight/internal-track object but labels its
weaker provenance explicitly. Secret resolution and the sanitized result are audited;
secret values never enter output or audit. `ok` means authentication and exact-object
observation succeeded, while `meets_release_goal` remains separate, so even a rejected
object cannot be mistaken for a successful release.

The twentieth closes the ordinary retry duplication window for store submissions.
Before invoking the trusted submitter, the engine writes a `delivery_actions` row with
a deterministic SHA-256 idempotency key derived from job, provider, integrated commit,
version and target, and exports it as `BASTET_DELIVERY_IDEMPOTENCY_KEY`. The submitter
must implement lookup-or-create and echo the exact key in its structured receipt. A
successful receipt is persisted before official status verification; later delivery
attempts validate and reuse it, so an API/authentication outage cannot upload or submit
again. A changed release identity fails closed. The task API/UI exposes sanitized
action status and a shortened key but not raw command output. This provides durable
at-most-once orchestration after receipt persistence; the command's own idempotent
lookup remains necessary for the narrow process-death interval during its execution.

The twenty-first closes that remaining process-death interval for explicitly configured
mobile profiles without taking ownership of upload or review mutation. With
`submission_recovery=official_api`, every unfinished action first authenticates to the
provider and performs an exact read-only lookup. Apple requires one `VALID` build bound
to app, release version, platform and build number, plus an App Store version attached
to that exact build. Google opens an uncommitted read edit and requires the configured
track to contain the exact versionCode, then deletes the edit. A match reconstructs the
submission receipt using the original deterministic action key; absence permits the
trusted command, while API errors, ambiguity and conflicting build attachment fail
closed before the command. The project UI exposes the recovery mode and immutable
provider identifiers. This makes retries convergent across the external-success/local-
crash boundary while leaving actual upload and review submission under the existing
human-approved command contract.

The twenty-second adds the first narrowly scoped provider mutation. Google Play
`internal` profiles may set `submission_adapter=official_api`, eliminating the custom
upload shell command while retaining the terminal release-manager approval. The
adapter confines `artifact_path` to the integrated worktree, requires a non-empty AAB,
computes SHA-256 and rejects a reused versionCode unless Play reports the identical
digest. It opens one edit, preserves all existing releases, appends only the requested
`draft` or `completed` internal release, validates, and commits with
`changesInReviewBehavior=ERROR_IF_IN_REVIEW`; its safer
`changesNotSentForReview=true` default is explicit and configurable. Any pre-commit
failure deletes the edit. A crash after commit is recovered by matching track,
versionCode and the provider bundle SHA-256 back to the same local artifact before the
durable receipt is reconstructed. The adapter refuses production or any non-internal
track, and Apple mutation remains outside this slice.

The twenty-third adds the corresponding narrow Apple promotion path without pretending
that processed-build promotion is binary upload. After the terminal human approval,
an App Store profile may select `submission_adapter=official_api`; the adapter requires
one exact `VALID` build for app, marketing version, platform and build number, looks up
or creates the matching App Store version, and refuses any conflicting attachment. It
then attaches that build and, unless the declared goal is only `uploaded`, looks up or
creates a review submission plus version item and sets `submitted=true`. Recovery now
requires the exact review item to be submitted-or-later for a submitted goal, closing
the crash gap between build attachment and review submission. The release type is
restricted to `MANUAL`; Apple binary upload, metadata authoring, and automated release
remain separate failure domains.

The twenty-fourth closes that remaining binary boundary with Apple's Build Upload API
without blocking a runner on asynchronous processing. A worktree-contained IPA or
macOS PKG is bound to its size and SHA-256. The adapter looks up or creates one exact
app/version/build/platform upload and one matching file reservation, validates that
Apple's operations cover every byte exactly once, and executes the pre-signed PUTs with
bounded concurrency while refusing to forward authorization. It then commits the file
with `uploaded=true` and `SHA_256`. A durable `build_upload` receipt parks delivery in
`waiting_external`; restart and scheduled polls reuse the same upload/file, retry safe
ranges, and resume the already-approved mutation only after the exact build is `VALID`.
Duplicate identities, conflicting files or checksums, unsafe/gapped ranges and provider
failure all stop closed. This separates transfer, Apple processing and review state
while still converging them through one delivery contract.

The twenty-fifth makes review submission contingent on evidence rather than assuming
that an attached build is ready. Before creating or submitting a review—and again when
recovering an already-submitted review—the adapter reads the exact version's included
localizations and review detail. Its explicit configurable policy requires at least one
localization, defaults localized text checks to `description`, `supportUrl`, and
`whatsNew`, can require named locales, always checks the review contact, and requires
demo credentials only when the provider marks them necessary. It emits the checked
locales and fields into the immutable receipt and reports exact missing paths before
any review mutation. Metadata authoring, screenshots, and App Information locale parity
remain separate work rather than being silently inferred.

The twenty-sixth follows the provider relationships instead of accepting a text-only
facsimile of readiness. It lists App Info and requires its complete locale set to equal
the App Store Version locale set, as Apple's submission contract specifies; ambiguous
App Info records require a configured exact ID. For every version localization it then
reads screenshot sets with included screenshots and accepts only provider-processed
`COMPLETE` assets. At least one processed screenshot per locale is the default, while
profiles can name platform-specific display types or explicitly delegate screenshot
evidence to another gate. `UPLOAD_COMPLETE`, missing sets/types, locale mismatch,
ambiguous provider objects and malformed responses all fail before review mutation.
The receipt preserves App Info identity, parity, screenshot counts and display types;
visual-content review and screenshot authoring remain outside the trusted adapter.

Provider adapters normalize official state into milestones while retaining the raw
status. Apple distinguishes `WAITING_FOR_REVIEW`, `IN_REVIEW`,
`PENDING_DEVELOPER_RELEASE`, and `READY_FOR_DISTRIBUTION`; Google Play track releases
distinguish `draft`, `inProgress`, `halted`, and `completed`, with managed publishing
review/readiness handled separately by its verifier. The engine therefore does not
equate API upload, edit commit, review acceptance, or staged rollout with public sale.

No phase is shipped solely from unit tests. Multiprocess dispatch/restart/race now
has an executable acceptance receipt, and the multi-branch DAG-to-remote release path
now has an end-to-end Git receipt. The production provider boundary is now
machine-checkable. Provider-neutral and fake official-endpoint tests are executable
locally; each real provider still needs a sandbox/TestFlight or internal-track
credentialed canary before release.

## Requirement traceability

The fifteen user concerns map respectively to: redesign invariants; task/stage
DAGs; role and evidence coverage; planning-round lifecycle; visible bounded
PM/system-analysis negotiation; proposal-gated breakdown; removal of whole-chat
single-card dispatch; removal of project-page decomposition; parallel stage DAGs;
bounded handoff challenges; template-family linting; Telegram progress/questions;
integration delivery; migration-level acceptance; and schedulable Skill install.
