# Bastet Agent OS — User Guide

Everything the WebUI and the CLI can do, in the order you would actually use it.
For getting Bastet onto a machine see [INSTALLATION.md](INSTALLATION.md).

## 1. Concepts

| Term | What it is |
|---|---|
| **Team** | the top of the org. Owns projects; memory can be shared at this level. |
| **Project** | 1:1 with a real git repo **on the Bastet host**, and with an AMOS project. Has a lifecycle state and a light. |
| **Agent** | an executor + an account + optional model config. `claude-code`, `claude-sdk`, `codex`, opt-in `codex-app-server`, `grok`, `agy`, `hermes`, `bastet-lite`. |
| **Role** | what an agent is *for* in a project (`engineer`, `reviewer`, `pm`, `ops-engineer`…). A stage asks for a role; the project's role assignment decides who runs it. |
| **Workflow template** | an ordered list of stages, each with a gate. |
| **Gate** | how a stage's exit is judged: `auto`, `tests-pass`, `agent-review`, `human-approve`. |
| **Project room** | project-local PM assignments, agent discussion, and structured stage handoffs; members follow project role assignments. |
| **Job** | one task moving through a workflow. A card on the board. |
| **Run** | one stage of one job executed by one agent. Carries usage and cost. |
| **Resource** | something an agent may call: an LLM endpoint, an MCP server, an API, a skill, a git remote. |
| **Grant** | who may use a resource (global / team / project), with budget and concurrency caps. |
| **Credential** | a secret, stored by reference. Resources point at it; nothing copies it. |

## 2. First run

1. Open `http://<host>:8890/ui`, paste the token from `~/.bastet/api_token`.
2. Pick your language from the header (remembered per browser).
3. **組織 tab** → create a team, then a project pointing at a repo path *on the
   Bastet host*, then an agent. If the executor is installed but not logged in,
   use the login wizard on the agent's account.
4. **模板 tab** → pick a built-in workflow preset, or copy one and edit it.
   Assign it to the project.
5. **組織 tab** → assign your agents to the roles the workflow asks for. This
   also grants the agent AMOS project membership, which is what lets it recall the
   project's memory.
6. **對話 tab** → talk through what you want built, with the project in scope.
7. Confirm the task plan on the **專案 tab**, then press ▶.

## 3. Tabs

### 看板 (Board)

Task cards in stage columns, moving live over WebSocket. A card shows its title
first (the id is secondary), its stage and status, its template, and 🔧 how many
times a gate handed it back.

An in-progress card shows a **stage progress bar** (n of m stages) and a
**heartbeat** — two separate facts on purpose: **alive** (the process has not
exited, confirmed every 20 seconds even when the executor prints nothing) and
**talking** (the last output line, and how long ago). 🟠 means one of two
things: no heartbeat for three minutes (probably dead), or alive but silent for
over ten minutes — what a run blocked on a child process waiting for input looks
like from outside. `updated_at` can tell you none of this. The engine acts on
liveness, never on silence: a genuinely slow, quiet stage is bounded by its own
`timeout_s`, not by the supervisor's patience.

Click a card for the drawer: the spec, per-stage runs with cost, gate results,
the diff, in-run interaction requests, and — when it is stuck — a retry that can
switch agent, refresh the workflow from the template, or edit the spec first.
Human-approve gates are approved or rejected here, **with the previews the stage
left** (screenshots inline, HTML/Markdown snapshots one click away). When the
project's PM has escalated something, the drawer opens with **its question** and
a box for your answer: one button files the ruling into the job's inbox and
retries. Finished
cards can be archived (kept, hidden) or deleted (refused if they spent money —
the accounting is the product; archive those instead).

**任務補給 (supplies)** — hand data to a job after dispatch: a deploy target, a
Firebase project id, a decision the spec could not contain. It is included in
every later run's brief (marked as overriding the original spec), and a live
worktree also receives it as a `._bastet/inbox/` file. Credential-shaped content
is refused with a pointer to the credentials card — a supply travels inside a
prompt, and prompts go to LLM providers; credentials arrive as env vars and never
do. Pending interaction requests also take an optional free-text message along
with allow/deny.

### 對話 (Chat)

**Configuring Bastet from the conversation**: ask for a resource ("幫我接上
ElevenLabs 的 TTS") and the responder reads the built-in `bastet-config` skill
and replies with a proposal card — the listed actions apply when *you* press
套用, the audit names you, and raw keys are refused (`secret:<id>` pointers
only; credentials still come from the Admin card). Users, tokens and channels
are deliberately outside this protocol.

Pick who answers — an agent (its own executor and account, read-only, with the
project's repo in view) or a pool LLM — and a scope: project, team, global.
Project scope puts real state in the prompt: description, repo, workflow, roles,
recent jobs, and the resources the project may use.

Attach files, documents and screenshots. Text is inlined; images ride along where
the wire supports it. **Media flow back too**: files an agent responder
generates (via the project's granted image/TTS/video resources) land in its
`$BASTET_CHAT_OUTBOX` and return as attachments on the reply — images inline,
audio with a player, video with controls (8 files / 50 MB each). Every turn is written to AMOS in the session's scope, so the
next run inherits the decisions. Pending human-approve gates are listed here with
Approve/Reject, and the discussion can be dispatched as a job — the agent never
dispatches itself.

Sessions are stored per project, so a conversation cannot drift from the org that
the runs execute against. Long messages are safe to type: Enter submits, Shift+
Enter adds a line, and IME composition never submits early.

### 專案 (Projects)

One collapsible card per project, grouped by status: 規劃中 / 待執行 / 執行中 /
維護中 / 已結案. Search by keyword or date range.

Each card carries the light, progress, and the controls that its state allows:

| Control | What it does |
|---|---|
| ▶ 執行 | dispatch the confirmed plan, task by task |
| ⏸ 暫停 | stop the *next* dispatch; the current task finishes |
| ■ 停止 | cancel what is in flight |
| 結案 / 重啟 | close, or reopen a closed project |

When a job finishes its pipeline, its `bastet/<job_id>` branch is **pushed to
the project's remote** automatically (origin, or a granted git resource), using
the project's git credentials. Your own branch is never pushed; opt out with
`git_auto_push: false` in the project config.
| 刪除 | remove the project (admin only) — see below |

Expanded, a card shows the task plan (with provenance: which conversation it came
from, and a warning if that conversation has moved on since), per-task job status,
role coverage against the workflow's needs, attached resources, and the
credentials visible to it.

**PM decomposition**: the project-manager agent turns the agreed plan into a task
list. It is read-only — it sees the repo, the stages and the conversation, and
writes nothing. You edit and confirm; only then does the runner dispatch.

**Deleting a project** takes its jobs, runs, gates, worktrees, role assignments,
project-scoped grants and chat sessions. The audit trail stays. It refuses, with
the reason, when work is in flight or when runs spent money; forcing it records
the written-off amount in the audit row.

### 資源 (Resources)

The pool, grouped by kind: `llm`, `mcp`, `api`, `skill`, `git`, media. Each
resource has its own visibility (global / team / project), a credential chosen
from the Admin tab's list (stored as a pointer, so rotating the key updates every
resource using it), and a **測試** button.

What the test actually does, per kind:

| Kind | Test |
|---|---|
| `llm` | lists models — a listing, never a completion, so testing costs nothing |
| `mcp` | a real `initialize` handshake, then reports the server's tool list |
| `git` | verifies the credential against the provider over HTTPS or SSH |
| `skill` | checks the source exists on the Bastet host |
| `api` | a shape check plus a reachability probe |

Verdicts are three-state: `ok`, `warn` (it answered, but not the way we hoped —
reachable-but-404 is a different bug from host-down), `failed`. The exact request
is shown either way.

MCP resources keep the vendor's install command. Press install and you get the
full output back, so a failed install can be fixed in place and retried. Nothing
installs implicitly.

### 組織 (Org)

Teams, projects, agents, executor accounts, and role assignments. The executor
dropdown labels each option 未安裝 / 未設定 / ready, so you can see why an agent
cannot run. Per-agent model selection and provider quota display live here too.

### 模板 (Templates)

The workflow design desk:

- **範本庫** — built-in presets, click to expand the flow: 前後端程式開發,
  網頁開發, 手機 APP 開發, 市場調查, 學術研究, 影片製作, 運維處理, 持續維護.
  Copy one to edit it.
- **我的範本** — your templates, editable in place (version increments) or
  copyable, assignable to projects, deletable.
- **角色定義 Prompt** — what each role *means* when an agent plays it. Prepended
  to that stage's brief. Built-ins are seeded once; your edits are never
  overwritten.
- **專案 ↔ 工作流對應** — which project runs which workflow. Closed projects are
  hidden here.

Stage options when editing: role, gate, gate command, read-only, isolation
(worktree / container), `max_retries` (executor-level retries), and the rework
controls: `on_fail` (`rework` | `block`), `rework_target`, `max_cycles`.

### 記憶 (Memory)

Team memory search, plus a browse view of what was written recently, filterable
by scope, with counts. It states which recall mode is live — vector (turbovec
installed) or keyword-only — and links to the full AMOS console, with the command
to start it if it is not running.

Memories come from two places: chat turns (in the session's scope) and runs (what
each stage did, what a gate rejected, how a job ended), attributed to the agent
that ran.

### 管理 (Admin)

- **系統設定** — the display timezone (IANA name, with a one-click "use the
  host's zone"). Storage stays UTC — that is what makes the audit trail
  comparable across machines — the setting only changes rendering, and it takes
  effect immediately for every user.
- **使用者** — create users with a real role (viewer / operator / admin), copy,
  disable, revoke, rotate or delete their tokens. Permissions take effect
  immediately; no re-issue needed.
- **憑證與機敏資料** — Token / KEY / password entries, layered by visibility.
  Multi-line values (PEM keys, service-account JSON) paste in whole; a one-line
  paste is repaired using the BEGIN/END markers. Values are write-only: you can
  rename, rescope and rotate, never read back.
- **通知頻道** — Telegram: pair a user, set a responder (agent or pool LLM) and a
  project. Plain messages get answered; gate approvals arrive as inline buttons;
  a blocked job arrives with a retry button.
- **系統設定** — the display timezone (common list plus any IANA name, one
  click to adopt the host's zone). Applies to every timestamp in the UI for
  every user immediately; storage stays UTC so audit trails compare across
  machines.
- **維護** — every component with installed vs available version, updatable
  individually or all at once: Bastet, Agent Memory OS, turbovec, the Claude
  Agent SDK, pytest, **Pillow** (media assets), **Playwright** (browser E2E and
  approval screenshots; chromium ships with the installer), and each executor
  CLI. See [Keeping it current](../README.md#keeping-it-current).

### 稽核 (Audit)

The append-only, hash-chained trail. Search by free text (actor, action, target,
detail), category (drawn from what is actually in the table), actor, and an
inclusive date range.

## 4. CLI reference

`bastet --help` lists everything. The commands you will use:

```bash
# org
bastet team add <id> "<name>"
bastet project add <id> <repo_path> --team <team>
bastet agent add <id> --name "<name>" --executor claude-code
bastet role-assign <project> <agent> <role>
bastet user add <name> --role operator          # prints the token once

# work
bastet dispatch <project> "<task>" --agent <agent> [--template <id>] [--resource <id>]
bastet jobs                       # cards
bastet job <job_id>               # detail
bastet approve <job_id> [--reject] --comment "..."
bastet runs                       # runs
bastet run <run_id>               # usage ledger + diff artifact

# workflow
bastet template add <file.yaml>
bastet template list

# resources
bastet resource add <name> --endpoint <url> --flavor anthropic --secret-ref keyring:...
bastet grant add <resource_id> project:<id> --budget-usd 5 --max-concurrency 2

# operations
bastet doctor                     # health, executors, gate tools
bastet usage                      # cost by project / agent / precision
bastet audit                      # the trail
bastet channel list
bastet service install|status|uninstall
bastet gc                         # sweep worktrees left by terminal jobs
bastet pricing-update             # refresh the price book
bastet whoami
```

## 5. Workflow templates

A template is a name and an ordered list of stages:

```yaml
name: standard-dev
stages:
  - name: 需求規劃
    role: pm
    gate: human-approve

  - name: 實作
    role: engineer
    gate: tests-pass
    gate_config:
      command: pytest -q
    max_cycles: 3            # how many times a failed gate may hand it back
    timeout_s: 7200          # this stage is heavy; don't kill it at the default hour

  - name: 程式審查
    role: reviewer
    gate: agent-review
    read_only: true
    rework_target: 實作      # a reviewer cannot fix what it rejected

  - name: 合併發布
    role: engineer
    gate: human-approve
    on_fail: block           # never loop a release step
```

| Field | Meaning |
|---|---|
| `role` | which role runs this stage |
| `gate` | `auto` / `tests-pass` / `agent-review` / `human-approve` |
| `gate_config.command` | required for `tests-pass`; runs on the Bastet host |
| `gate_config.precheck_command` | trusted host check whose evidence is injected into an `agent-review` stage |
| `requires` | capabilities Bastet must probe and provide before the Agent starts, e.g. `[browser.playwright]` |
| `read_only` | the stage may not write (reviewers, auditors) |
| `isolation` | `worktree` (default) or `container` |
| `max_retries` | retries for an *executor* failure (crash, timeout) |
| `on_fail` | `rework` (default) or `block` |
| `rework_target` | which stage gets the work back; default is the nearest earlier writable stage |
| `max_cycles` | rework cap, default 3 |
| `timeout_s` | per-stage run budget in seconds; 0 (default) inherits the dispatch value (3600). Heavy stages — an hour-long optimisation pass — need this or the kill at the default mark loses the whole run |

### Gate semantics

- **`auto`** — the stage finishing is the pass.
- **`tests-pass`** — the engine runs the command in the job's worktree; exit code
  decides. No agent is involved in the verdict.
- **`agent-review`** — the stage must write `{"verdict": "approve"}` (or
  `{"verdict": "reject", "reasons": [...]}`) to `._bastet/verdict.json`. Prose
  never decides; a missing verdict is a rejection.
- **`human-approve`** — waits for a person, via the board, the chat tab, or
  Telegram (inline Approve/Reject buttons). The stage's brief asks the agent to
  leave evidence in `._bastet/preview/` — screenshots, an HTML snapshot, or a
  Markdown summary. Bastet copies those out before the worktree is removed,
  shows them in the approval panel, and sends the images as photos with the
  Telegram card. A gate with no preview says so, instead of presenting a bare
  diff as if that were normal.

### When a gate fails

The card goes back to a stage that can fix it, carrying the gate's output, and
the pipeline continues. It stops and asks a human only when the stage is
`on_fail: block`, nothing earlier can write, or the cycles are spent. Full
explanation: [When a gate says no](../README.md#when-a-gate-says-no) and the
complete reference in [WORKFLOWS.md](WORKFLOWS.md).

### When the executor fails

A crash or timeout blocks the card after the stage's `max_retries`. A vendor
**quota / rate limit** does not: the card parks with the reset time parsed from
the vendor's own message (`resets 1:30am (Asia/Taipei)` included) or a
30-minute backoff, and retries itself — the Telegram message says 「會自己續跑
—— 不需要你做什麼」. **Retry semantics**: a manual retry preserves the bounded
rework/PM circuit breaker unless you explicitly renew the recovery lease; it
still clears any quota timer. Picking a different agent on retry is a
one-shot override that outranks the role mapping for that stage only; the
workflow-refresh checkbox picks up in-place template edits.

## 6. Resources at run time

A granted resource reaches the agent three ways:

```bash
BASTET_RES_<NAME>_URL      # endpoint
BASTET_RES_<NAME>_KEY      # api key   (resolved from the credential)
BASTET_RES_<NAME>_TOKEN    # git token
BASTET_RES_<NAME>_MODEL    # default model
BASTET_RES_<NAME>_SOURCE   # skill source
BASTET_MCP_CONFIG          # path to an mcpServers JSON (0600, outside the worktree)
```

plus a manifest written into the task brief, so the agent knows what it has. For
Claude Code the MCP config is also passed as `--mcp-config`. The MCP file holds
resolved credentials, so it lives outside the worktree and is deleted when the run
ends.

Git resources support HTTPS and SSH. For SSH, Bastet writes the deploy key to a
0600 temp file and sets `GIT_SSH_COMMAND` with `IdentitiesOnly`, `BatchMode` and
`accept-new`, so a run never prompts and never picks up an unrelated agent key.

## 7. Operations checklist

```bash
bastet doctor                             # first stop for anything odd
journalctl --user -u bastet -f            # what the service is doing
bastet audit | tail -40                   # what changed, and who did it
bastet usage                              # what it cost
bastet gc                                 # worktrees left by terminal jobs
```

Things worth knowing when something looks wrong:

- **A card is blocked with a 設定問題.** The gate command cannot run in that repo.
  Fix the command in the template (the retry offers to refresh the workflow), or
  let the loop hand it to an agent to add the missing script.
- **An agent "has" a role but sees no project memory.** Role assignment grants
  AMOS project membership; re-assign to trigger it.
- **The board shows nothing after a restart.** The runner resumes on startup
  (`project.runner.resumed` in the audit trail); if a project was parked, press ▶.
- **Recall finds nothing sensible.** Check the memory tab's mode line — without
  turbovec, AMOS is doing keyword matching.
- **A notification never arrived.** The channel card shows `status` and
  `notify_errors`; a channel whose notify loop died reports `notify_down` rather
  than continuing to claim `polling`.
