# Workflow Operations Manual — 工作流操作手冊

The complete reference for how a task card moves: every stage field, every gate,
what happens on failure, and every way a human can intervene. This is the
operations half; [USER_GUIDE.md](USER_GUIDE.md) covers the UI tab by tab.

Everything here is behaviour that exists and is pinned by tests. Where a rule
came from a real incident, the incident is named — that is why the rule exists.

## 1. The lifecycle of a card

```
dispatch ──► stage 1 ──gate──► stage 2 ──gate──► … ──► done ──► auto-push
                │                 │
                │ gate fails      │ gate pending (human-approve)
                ▼                 ▼
          rework: back to      blocked, previews attached,
          a writable stage     Approve/Reject on WebUI + Telegram
                │
                │ cycles exhausted / on_fail: block / nothing writable
                ▼
             blocked (a person decides; lease renewal reopens the loop)
```

A job snapshots its template's stages at dispatch, so editing the template never
changes a running card — except through **retry with workflow refresh** (below),
which is explicit.

## 2. Stage fields

```yaml
- name: 頁面實作                 # unique within the template
  role: frontend-engineer       # who runs it (project role assignment decides the agent)
  gate: auto                    # auto | tests-pass | agent-review | human-approve
  gate_config:
    command: pytest -q          # tests-pass only; runs on the Bastet host
  read_only: false              # true for reviewers/auditors — they cannot write
  isolation: worktree           # worktree (default) | container
  max_retries: 1                # extra attempts on EXECUTOR failure (crash/timeout)
  timeout_s: 7200               # per-stage run budget; 0 = dispatch default (3600)
  on_fail: rework               # rework (default) | block
  rework_target: 頁面實作        # who fixes a failed gate; default: nearest earlier writable stage
  max_cycles: 3                 # rework budget before stopping for a human
  requires: [browser.playwright] # control-plane capability contract
```

**`timeout_s`** exists because a heavy stage (a 50–70 minute Three.js
optimisation pass) was killed four times at the fixed 3600s mark, losing an
hour of work each time. The run token's TTL follows the effective budget.

**`max_retries` vs `max_cycles`**: retries re-run the *same stage* after an
executor failure (process crash, timeout); cycles count how many times a
*failed gate* may hand the card back to an earlier stage. They solve different
problems and are budgeted separately.

**`requires` is an execution contract, not prompt text.** Before an Agent
attempt begins, Bastet probes every requirement through the control-plane
provider. `browser.playwright` launches Chromium and renders a page from the
Bastet host process. Merely finding a Chrome binary does not pass the probe.
The stage must also provide a managed delivery path: either a `tests-pass`
command or an operator-controlled `gate_config.precheck_command`. A missing or
crashing capability blocks immediately, preserves `max_retries` and
`max_cycles`, and posts the diagnosis in the project room.

For a browser-backed AI review, use a trusted precheck:

```yaml
gate: agent-review
requires: [browser.playwright]
gate_config:
  precheck_command: npm run test:e2e
```

Bastet runs the command outside the Agent sandbox and injects its output into
the review prompt. The reviewer may reason over that evidence but cannot forge
or silently replace it.

## 3. Gate semantics

| Gate | Who decides | How |
|---|---|---|
| `auto` | nobody | finishing the stage is the pass |
| `tests-pass` | the engine | runs `gate_config.command` in the worktree; exit 0 passes. No agent is involved in the verdict |
| `agent-review` | the stage's agent | must write `{"verdict": "approve"}` or `{"verdict": "reject", "reasons": […]}` to `._bastet/verdict.json`. Prose never decides; a missing/malformed verdict is a rejection |
| `human-approve` | a person | WebUI drawer, chat tab, or Telegram inline buttons |

A `tests-pass` command that **cannot run at all** (missing script, command not
found) is flagged as a configuration problem, not a failing test — and is still
handed back to an agent that can add the missing script or dependency, with
instructions not to fake a green exit.

For an incremental suite, retain `command` as the fallback and declare named
cases with explicit coverage:

```yaml
gate: tests-pass
gate_config:
  command: pytest -q
  cases:
    - id: context-unit
      command: pytest -q tests/test_context.py
      covered_paths: ["src/context/**", "tests/test_context.py"]
```

A passing case is skipped after rework only when its evidence commit is still
an ancestor and the handoff change set does not intersect `covered_paths`.
Missing coverage, rewritten history, or a previous failure always re-runs it.

## 4. When a gate fails: the rework loop

The card goes **back** to a stage that can fix it, and the target **advances**
if the same gate keeps failing: the failing stage first (an implementer whose
own tests fail should fix them), then the nearest earlier writable stage,
skipping read-only reviewers, clamping at the earliest writable stage. An
explicit `rework_target` overrides the walk entirely. Counted per stage per
episode. A human must explicitly renew the recovery lease to start the walk over.

Standing still does not converge: with the target pinned to the failing stage,
a live E2E gate sent the same failing test back to the tester nine times across
four hours while nobody touched the product code it was failing on. The card
carries the gate's real output (up to 8 000 chars; the assertion is at the end, so the tail
is what is kept). The brief that travels with it forbids the shortcuts by name:
don't edit the test command, delete the test, make the assertion trivially
true, add skip/xfail, or touch the workflow config. The cheapest way to pass a
gate is to weaken it, and an agent told only "make it green" will.

The loop stops for a human in exactly three cases:

1. the stage declares `on_fail: block` (a deploy step should not be looped);
2. no earlier stage can write (an all-read-only pipeline);
3. `max_cycles` is spent — an agent that failed three times is not converging.

Every hand-back is audited as `job.rework`, counted on the board card (🔧), and
written into team memory as a `warning` so the next agent does not repeat the
mistake.

**And then the PM takes over.** A card blocked for a *business* reason — cycles
spent, an acceptance dispute, a missing ruling — is handed to the project's `pm`
agent, which reads the spec, the gate output, the rework note and the run
history and picks one bounded action: retry, hand the stage to another agent,
file a ruling into the job's inbox and retry, or escalate. Hard limits: two
audit-counted interventions per episode (an explicit recovery-lease renewal
refreshes them, the PM's
own retries cannot), escalations latch until a human retries, `human-approve`
gates and quota waits are never touched, and the diagnosis run is read-only.
Escalation is the last resort — a checkable fact (which commit is the baseline,
how to obtain evidence) the PM is expected to settle itself, and its escalation
`reason` must be phrased as a question, because the card presents it as one with
a box for the answer and one button that files the ruling and retries.
That ruling retry starts at the failed stage's writable `rework_target`; a
ruling that asks for a code change must not merely re-run the read-only
reviewer. It preserves the spent recovery budget unless the operator separately
renews the recovery lease.

## 5. Execution failures are not gate failures

A run that crashes, times out, or is cancelled blocks the card (after
`max_retries`). Two special cases:

**Quota / rate limits wait themselves out.** A failure that looks like a vendor
limit (session limit, usage limit, 429, overloaded, low credit) parks the card
with a `resume_at`: the reset time stated in the message when there is one —
`resets 1:30am (Asia/Taipei)` parses, vendor timezone and all — or a 30-minute
backoff. A background sweep retries due cards automatically (audited as
`server:quota-reset`); the Telegram message says 「會自己續跑 —— 不需要你做什麼」.
A manual retry beats the clock. Ordinary failures are never mistaken for
timers; unknown timezones fall back to UTC.

**A depleted balance is not a timer.** `402 Payment Required` / "balance
exhausted" means only money will fix it, so the agent is marked depleted and
**every** routing path skips it — role mapping, explicit override, alternate
selection, job default, PM selection. The stall becomes recoverable *by
routing*: the supervisor swaps in a funded stand-in without spending a PM
intervention. Only a human clears the flag (the Agents card,
`POST /api/agents/{id}/undeplete`, or a retry that explicitly names the agent);
automated retries cannot. Without this the router kept re-dispatching a dead
agent every rework cycle, undoing the PM's correct handovers twice over.

**A restart is not a death.** A card whose driver died with the service process
is re-driven from its current stage at startup (`job.resumed`). A paused or
closed project's card is not restarted, but it is blocked with the real reason
instead of claiming to run.

## 6. Human interventions

| Action | Where | Semantics |
|---|---|---|
| **Approve / Reject** | drawer, chat, Telegram | decides a `human-approve` gate. Approving the last stage completes the card — with the same delivery (memory, event, push) as any other completion |
| **Retry** | drawer, Telegram 🔁 button | re-runs the current stage without silently reopening bounded recovery budgets, clears any quota timer, and optionally: |
| **File ruling & retry** | stuck-card PM question | stores the ruling, adopts the current workflow, and restarts at the failed stage's writable `rework_target`; recovery budgets remain unchanged |
| — with a different agent | drawer dropdown | a **one-shot override** that outranks the role mapping for the retried stage, then clears — chosen because the mapping once kept handing a retry back to the very agent whose vendor was broken |
| — with workflow refresh | checkbox (default on) | picks up the template's current version when its stages changed — fixing a stage's test command in place is the most common reason to retry |
| — with recovery-lease renewal | explicit checkbox (default off) | after the environment/contract was actually repaired, resets the rework counter and opens a new bounded PM diagnosis episode |
| — with an edited spec | drawer textarea | replaces the card's spec before re-running |
| **任務補給 (supplies)** | drawer | hand data to a running job: deploy targets, project ids, decisions, rulings. Included in every later run's brief (marked as overriding the spec) and dropped into a live worktree's `._bastet/inbox/`. Credential-shaped content is refused — supplies travel in prompts, credentials travel as env vars |
| **Pause / Stop** | project card | pause stops the *next* dispatch; stop cancels what is in flight |

## 7. Previews: what the approver sees

A `human-approve` stage's brief instructs the agent to leave evidence in
`._bastet/preview/` — screenshots (Playwright with chromium is standard tooling
on the host: `playwright screenshot --viewport-size=1280,800 'http://localhost:PORT' 檔名.png`),
an HTML snapshot, or a Markdown summary. Bastet copies previews out before the
worktree is removed, shows images inline in the approval panel, and sends them
as photos with the Telegram approval card. A gate with no preview says so —
an approval request without evidence is a request to sign blind.

## 8. Media stages

When the project has media resources granted (image / video / music / tts /
stt), the brief adds two hard rules, both from live incidents:

- **Download the artefact, not the URL.** Vendor download links expire (48h at
  one provider); only a real file in the worktree survives to the branch.
- **Never background the generation and end the turn.** A headless run is
  one-shot: its children are reaped the moment it ends and no completion
  notification can ever arrive — one card burned three cycles starting a
  pipeline and exiting. Poll in the foreground until the files exist; batch
  large sets; if time runs out, keep what finished and say exactly where you
  stopped.

In chat, an agent's generated files return to the conversation via
`$BASTET_CHAT_OUTBOX` (8 files / 50 MB caps) and render inline.

## 9. Delivery: what happens at done

1. Whatever the agents produced is committed to **`bastet/<job_id>`** — the
   job's own branch. Your branches are never written to; merging is a
   deliberate human act.

   Commits happen at **every stage boundary**, not only at the end:
   `bastet(<stage>): <title>`. So the branch history reads as the pipeline
   actually ran (rework included) and every stage starts from a clean tree —
   which is what lets a reviewer bind test evidence to the content under
   review. `._bastet/` (previews, verdict files, the inbox) is *never*
   committed; it is the engine↔agent boundary, and committing it made each run
   dirty the tree again by regenerating those files.

   Agents may also commit themselves. A linked worktree keeps its git metadata
   in the *main* repo (`.git/worktrees/<name>`), outside a `workspace-write`
   sandbox, so sandboxed executors are granted that directory explicitly —
   without it every git write failed with what looks like a broken disk
   (`cannot lock ref 'ORIG_HEAD': Read-only file system`).
2. The task's explicit delivery contract decides the terminal transition.
   `branch` must push `bastet/<job_id>` successfully. `production` must also
   merge a freshly fetched target, atomically push it with the immutable
   `v<version>` tag, run the trusted deployment profile, and verify the exact
   commit online. The verify command must finish by emitting a JSON receipt with
   `status=verified` and the exact `target`, `version`, and `commit_sha`; a zero exit
   code without that machine-checkable evidence is a failure. A failure is audited,
   blocks the card, preserves the worktree,
   and invokes PM diagnosis; `job.done` is written only after the durable
   receipt succeeds. `none` retains the old optional best-effort branch push.
3. The run's summary, any gate rejections, and the completion are written to
   team memory, attributed to the agent that ran, scoped to the project.

Both completion paths deliver identically — the driver loop and a final-stage
human approval.

Before enabling a real production profile, run `bastet production-rehearsal` to
exercise the provider-neutral contract in isolation. It proves one exact HTTP
readback succeeds and one stale readback blocks. Then run the equivalent canary
against the actual provider using that project's trusted deploy and verify commands.

For the built-in mobile adapters, use `bastet store-canary --job <id>` to read the
exact frozen submission with project-scoped credentials. If no Bastet submission exists
yet, `--project <id> --submission <receipt.json>` can preflight an existing provider
object, but that mode does not independently establish the file's commit provenance.

Mobile store delivery is asynchronous. The built-in `mobile-app` workflow already
ends at a release-manager `human-approve` stage, which is mandatory for
`app_store_connect` and `google_play`. Store adapters must emit the common identity
fields plus one monotonic milestone (`uploaded` → `submitted` → `approved` →
`published`) and retain Apple's or Google's raw `provider_status`. A receipt below
the profile's `release_goal` schedules another verification poll; it does not rerun
the upload command. A `rejected` milestone is terminal failure.

An official-adapter submission command also receives
`BASTET_DELIVERY_IDEMPOTENCY_KEY`. It must use that key for lookup-or-create and return
the same value as `idempotency_key` in its submission receipt. Bastet persists that
receipt independently of delivery attempts. A retry caused by a later status failure
therefore skips submission and reuses the provider object ID; changing release identity
under the same job is rejected.

For crash recovery before that receipt reaches SQLite, set
`submission_recovery=official_api` and freeze Apple `build_number`/`platform` or Google
`version_code`. Bastet then performs a non-mutating provider lookup before every
not-yet-succeeded action. An exact Apple app/version/build attachment or Google
track/versionCode reconstructs the receipt and suppresses the command. Provider errors,
multiple matches and conflicting Apple build attachments stop the delivery instead of
riskily retrying a side effect.

Apple submission and review are separate operations, and approval can still leave a
version at Pending Developer Release. Google Play edits are not live until committed;
managed publishing can further hold reviewed changes before publication. Templates
must therefore state whether their completion promise is submission, approval, staged
rollout, or actual public publication.

Official provider references: [Apple app and submission statuses](https://developer.apple.com/help/app-store-connect/reference/app-information/app-and-submission-statuses),
[Apple review submissions](https://developer.apple.com/documentation/appstoreconnectapi/reviewsubmission),
[Google Play edits](https://developers.google.com/android-publisher/edits),
[Google Play track release states](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.tracks),
and [Google Play managed publishing](https://support.google.com/googleplay/android-developer/answer/9859654).

## 10. Liveness: telling working from stuck

An in-progress card shows a stage progress bar and a **heartbeat** — which is
two separate facts, deliberately:

- **alive**: the process has not exited, confirmed every 20 seconds even when
  the executor prints nothing (some print nothing until they finish);
- **talking**: the last output line, and how long ago it was said.

🟢 when both look healthy. 🟠 in two different situations: no heartbeat for
three minutes (probably dead), or alive but **silent for over ten minutes** —
which is what a run blocked on a child process waiting for input looks like
from outside. `updated_at` cannot make any of these distinctions, because long
stages legitimately go minutes between database writes.

**What the engine does about it matters more than the colour.** Interruption is
decided by *liveness*, never by silence: a lost heartbeat (three minutes, nine
missed beats) means the run is gone and it is interrupted with the worktree
kept; a quiet-but-alive stage is bounded by its own `timeout_s`, which is what
declaring a time budget is for. Killing on silence alone once executed an agent
that had honestly reported "FPS bench is still running. Waiting for it (20
levels × 60s)" — four times, because a 20-minute test can never finish inside a
15-minute patience. If a stage is legitimately slow and quiet, give it
`timeout_s`; the amber badge is information, not a verdict.

If a card is genuinely stuck, the diagnosis order that has worked in practice:

1. the drawer's failure output (the real error, not the status);
2. `bastet audit` for the card's event trail;
3. the run heartbeat text — what was it doing last;
4. `journalctl --user -u bastet` around the timestamps.

## 11. Every stop, and what it means

| You see | It means | Do |
|---|---|---|
| ⏸ waiting for human approval | the designed stop | approve/reject (previews attached) |
| ⏳ 額度用盡，會自己續跑 | vendor quota; timer set from the vendor's own message | nothing — or retry to beat the clock |
| 🔧 自動返工 n 次 (in progress) | the loop is fixing a failed gate | nothing; it reports if it cannot converge |
| 🟠 blocked: 已返工 N 次仍未通過 | the loop did not converge | read the gate output; fix the cause, then explicitly renew the recovery lease if a new loop is justified |
| 🟠 blocked: 設定問題 | a gate command cannot run in this repo | fix the template's command (retry offers workflow refresh) or supply the missing dependency |
| 🟠 blocked: execution failed/timeout | the executor died | check the error; consider `timeout_s` on the stage; retry (optionally with another agent) |
| 🟠 explicitly assigned agent … is incompatible | its direct/Gateway path, API flavor, model, read-only support, or grant cannot satisfy this stage | choose a compatible agent or assign the right LLM resource; the blocked card and PM budget are left untouched |
| 🟠 blocked: 服務重啟時中斷… | project was paused/closed during a restart | resume the project, then retry |
| 🤖 PM 監督介入 | the PM diagnosed a business stall and acted (retry / handover / ruling) | nothing; the notice says what it decided and why |
| 🤖 PM 需要你的裁定 | the PM escalated a decision it may not make | answer the question on the card; one button files the ruling and retries |
| 💳 額度用盡，已暫停派工 | a vendor said `402`; that agent left the routing rotation | top up, then clear it on the Agents card — work is already routed around it |
| 🟠 還活著，但已沉默 N 分鐘 | the process lives but has said nothing; often a child waiting on input | check `pgrep -P <pid>` for a child at 0% CPU; a legitimately slow stage wants `timeout_s` |
