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
             blocked (a person decides; retry refills the loop)
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
```

**`timeout_s`** exists because a heavy stage (a 50–70 minute Three.js
optimisation pass) was killed four times at the fixed 3600s mark, losing an
hour of work each time. The run token's TTL follows the effective budget.

**`max_retries` vs `max_cycles`**: retries re-run the *same stage* after an
executor failure (process crash, timeout); cycles count how many times a
*failed gate* may hand the card back to an earlier stage. They solve different
problems and are budgeted separately.

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

## 4. When a gate fails: the rework loop

The card goes **back** to a stage that can fix it — past read-only reviewers to
the last stage that writes (or the explicit `rework_target`) — carrying the
gate's real output (up to 8 000 chars; the assertion is at the end, so the tail
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

**A restart is not a death.** A card whose driver died with the service process
is re-driven from its current stage at startup (`job.resumed`). A paused or
closed project's card is not restarted, but it is blocked with the real reason
instead of claiming to run.

## 6. Human interventions

| Action | Where | Semantics |
|---|---|---|
| **Approve / Reject** | drawer, chat, Telegram | decides a `human-approve` gate. Approving the last stage completes the card — with the same delivery (memory, event, push) as any other completion |
| **Retry** | drawer, Telegram 🔁 button | re-runs the current stage. **Refills the rework budget** (a human pressing retry after fixing the world is a fresh lease), clears any quota timer, and optionally: |
| — with a different agent | drawer dropdown | a **one-shot override** that outranks the role mapping for the retried stage, then clears — chosen because the mapping once kept handing a retry back to the very agent whose vendor was broken |
| — with workflow refresh | checkbox (default on) | picks up the template's current version when its stages changed — fixing a stage's test command in place is the most common reason to retry |
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
2. The branch is **pushed** to the project's remote: `origin` if the repo has
   one, else a granted git resource's URL, with credentials matched by exact
   host only (a GitLab token never travels to github.com). Opt out per project
   with `git_auto_push: false`. A failed or timed-out push is audited and
   non-fatal — the work is safe locally.
3. The run's summary, any gate rejections, and the completion are written to
   team memory, attributed to the agent that ran, scoped to the project.

Both completion paths deliver identically — the driver loop and a final-stage
human approval.

## 10. Liveness: telling working from stuck

An in-progress card shows a stage progress bar and a **heartbeat**: the run's
last output line and how long ago — 🟢 fresh, 🟠 after three silent minutes
with a "possibly stuck" hint. `updated_at` cannot make this distinction because
long stages legitimately go minutes between database writes.

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
| 🟠 blocked: 已返工 N 次仍未通過 | the loop did not converge | read the gate output; fix the cause (or the criterion); retry refills the loop |
| 🟠 blocked: 設定問題 | a gate command cannot run in this repo | fix the template's command (retry offers workflow refresh) or supply the missing dependency |
| 🟠 blocked: execution failed/timeout | the executor died | check the error; consider `timeout_s` on the stage; retry (optionally with another agent) |
| 🟠 blocked: 服務重啟時中斷… | project was paused/closed during a restart | resume the project, then retry |
