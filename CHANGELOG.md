# Changelog

All notable changes to Bastet Agent OS. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

Every user-visible change bumps `__version__` in
`src/bastet_agent_os/__init__.py` and adds a section here; `web/package.json`
follows the same number and the WebUI prints it beside the title.
`tests/test_version.py` fails the build if the three drift apart.

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
