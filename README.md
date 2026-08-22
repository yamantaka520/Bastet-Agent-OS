# 🐈 Bastet Agent OS

[![PyPI](https://img.shields.io/pypi/v/bastet-agent-os?label=pypi)](https://pypi.org/project/bastet-agent-os/)
[![Python](https://img.shields.io/pypi/pyversions/bastet-agent-os?label=python)](https://pypi.org/project/bastet-agent-os/)
[![CI](https://img.shields.io/github/actions/workflow/status/yamantaka520/Bastet-Agent-OS/ci.yml?branch=main&label=CI)](https://github.com/yamantaka520/Bastet-Agent-OS/actions/workflows/ci.yml)
[![Docker pulls](https://img.shields.io/docker/pulls/yamantaka520/bastet-agent-os?logo=docker&label=docker%20pulls)](https://hub.docker.com/r/yamantaka520/bastet-agent-os)
[![License](https://img.shields.io/github/license/yamantaka520/Bastet-Agent-OS?label=license)](https://github.com/yamantaka520/Bastet-Agent-OS/blob/main/LICENSE)

**A local-first operating system for AI-agent teams.** Bastet organizes the
agents you already use — Claude Code (CLI or Agent SDK), Codex CLI, Grok Build,
Google Antigravity (`agy`), Hermes, or any OpenAI/Claude-compatible endpoint —
into teams with roles, gated workflows, and centrally governed resources, so
several projects can run concurrently under control.

Bastet is a **control plane, not another agent framework**. Execution comes from
orchestrating agents that already exist; what Bastet adds is governance, a
workflow engine that keeps going when things fail, and team memory.

Linux · macOS · Windows · WebUI + CLI ·
[繁體中文說明](README.zh-Hant.md)

## Screenshots

Live captures from the validation deployment — a real Three.js game project
(CatsWalker) driven end-to-end by the workflow engine.

**A project card, expanded** — the workflow's five stages, which role runs each,
and the actual agents assigned (Codex, Claude Code, agy); PM decomposition
confirmed, 30/30 tasks delivered:

![Project card with workflow, role assignments and task plan](https://raw.githubusercontent.com/yamantaka520/Bastet-Agent-OS/main/assets/screenshot-project.png)

**A finished card's drawer** — the spec, the 🔧 rework note, and the run
history: three implement↔review cycles the loop resolved by itself, per-stage
agents and cost:

<img src="https://raw.githubusercontent.com/yamantaka520/Bastet-Agent-OS/main/assets/screenshot-drawer.png" alt="Job drawer: rework history, per-stage runs with agents and cost" width="55%">

**The maintenance card** — every component's installed vs available version;
the honest states are the point (a yellow 有新版 for the Claude Agent SDK, and
`unknown` rather than a guessed "current" for tools that cannot say):

![System settings and the maintenance card with component versions](https://raw.githubusercontent.com/yamantaka520/Bastet-Agent-OS/main/assets/screenshot-admin.png)

## Why

Running one coding agent is easy. Running a *team* of them across several
projects is where it falls apart:

- A failing test stops everything and waits for a human, even though the agent
  that wrote the code is the one equipped to fix it.
- Nobody can say afterwards what ran, on whose account, at what cost.
- Every project starts from zero, because nothing the agents learned was kept.
- Credentials end up pasted into a dozen config files.
- "Which of these six CLIs is out of date?" has no answer.

Bastet answers those in one place, on your own machine, with an append-only
audit trail behind every state change.

## Features

- **A workflow engine that continues.** Stage pipelines (plan → implement →
  test → review → merge) with four gate types. A failed gate hands the card
  *back* to a stage that can fix it, with the failure output attached, and the
  pipeline carries on. A vendor quota failure parks with the reset time parsed
  from the vendor's own message and resumes itself. It stops for a human only
  when it genuinely cannot proceed. → [When a gate says no](#when-a-gate-says-no)
- **Project lifecycle with a light.** planning → ready → running ⇄ paused →
  maintenance → closed, as a real state machine: only declared transitions, each
  one audited. Run / pause / stop controls on the card.
- **Kanban board with liveness.** Task cards move across stage columns live
  over WebSocket; an in-progress card shows a stage progress bar and a
  heartbeat (the run's last output line, 🟠 when silent), so working and stuck
  are distinguishable. The drawer takes 任務補給 — data handed to a running job
  mid-flight — and human approvals arrive with real evidence: screenshots and
  summaries the agent left in `._bastet/preview/`, sent as photos on Telegram.
- **Resource pool + metering gateway.** LLM / MCP / API / skill / git resources
  with per-resource visibility (global / team / project), a credential picker
  that stores a reference rather than a copy, a real test button per resource,
  and an OpenAI/Claude-compatible gateway that meters what runs spend.
- **Team memory.** Built on [Agent Memory OS](https://github.com/yamantaka520/Agent-Memory-OS):
  every run writes what it did, what a gate rejected and how the job ended,
  attributed to the agent that ran and scoped to the project. Context packs are
  read *as that agent*, so AMOS's ACL applies.
- **Chat as the input channel — and the output channel.** Plan a project by
  talking to an agent or a pool LLM, attach files and screenshots, dispatch from
  the conversation — and configure Bastet itself there: ask for a new media API
  resource and the agent proposes a `bastet-config` card that *you* apply.
  Generated media (an image, a TTS clip) come back inline via the run's outbox.
  Telegram is the second such channel, with inline approvals and previews.
- **Finished work ships itself.** Every completion commits to the job's own
  `bastet/<job_id>` branch and pushes it to the project's remote with the
  project's own credentials — your branches are never written to; merging stays
  a human act.
- **Governance you can read.** Three-tier roles, per-user tokens, budgets and
  concurrency caps per grant, worktree or container isolation per run, and a
  hash-chained audit log with search.
- **Five-language WebUI.** 繁體中文 · 简体中文 · English · 日本語 · 한국어.

## How it compares

|  | Bastet Agent OS | An agent framework (LangGraph, CrewAI…) | A hosted agent platform |
|---|---|---|---|
| What it is | control plane over agents you already run | the agent runtime itself | someone else's runtime |
| Where it runs | your machine / your LAN | your process | their cloud |
| Execution | delegates to claude / codex / grok / agy / hermes | you write the loop | their loop |
| On failure | hands the work back to a stage that can fix it | your code decides | opaque |
| Accounting | usage ledger + audit log per run | none | their dashboard |
| Memory | AMOS, requester-scoped ACL | none / bring your own | their store |
| Credentials | referenced from one place, never copied | in your config | uploaded |

Bastet does not replace an agent framework — you can run one *inside* a stage.

## Install

One command on the machine that will run the control plane:

```bash
curl -fsSL https://raw.githubusercontent.com/yamantaka520/Bastet-Agent-OS/main/install.sh | bash
```

Or from [PyPI](https://pypi.org/project/bastet-agent-os/) / [Docker Hub](https://hub.docker.com/r/yamantaka520/bastet-agent-os):

```bash
pip install bastet-agent-os          # the wheel carries the built WebUI
docker run -v bastet:/data -p 8890:8890 yamantaka520/bastet-agent-os
```

It creates `~/.bastet/venv`, installs Bastet + Agent Memory OS + the Claude Agent
SDK + pytest, installs the executor CLIs with their vendors' own installers, runs
`bastet init`, and finishes with `bastet doctor`. Details, flags and the
per-executor login steps: [docs/INSTALLATION.md](docs/INSTALLATION.md).

From PyPI (the wheel carries the built WebUI — no Node needed), or Docker:

```bash
pip install bastet-agent-os "agent-memory-os[full]" && bastet init && bastet serve
```

```bash
docker run -d -p 8890:8890 -v bastet-home:/data yamantaka520/bastet-agent-os
```

The container holds the control plane, gateway, WebUI and `bastet-lite`; the
vendor executor CLIs stay on a host because their logins are interactive and
their credentials are yours — [docs/INSTALLATION.md](docs/INSTALLATION.md#docker).

From a clone, for development:

```bash
pip install -e '.[dev]'
bastet init            # ~/.bastet: db, api token, config
bastet serve           # control plane + gateway on 127.0.0.1:8890
```

The WebUI is at `http://127.0.0.1:8890/ui` — paste the token from
`~/.bastet/api_token`.

## Quickstart

```bash
# an org: a team, a project bound to a real repo, an agent
bastet team add meow "Meow Team"
bastet project add catswalker ~/Github/catswalker --team meow
bastet agent add cc-worker --name "Claude Code Worker" --executor claude-code

# a workflow, then work through it
bastet template add standard-dev.yaml
bastet role-assign catswalker cc-worker engineer
bastet dispatch catswalker "Fix the failing test in tests/test_booking.py" \
  --agent cc-worker --template standard-dev

bastet runs                  # what is running
bastet run <run_id>          # detail: usage ledger, diff artifact
bastet usage                 # cost by project / agent / precision
bastet audit                 # append-only trail
bastet doctor                # health, including gate tools
```

To meter traffic through the gateway instead of a subscription CLI, register an
LLM resource and pass `--resource`:

```bash
bastet resource add anthropic-api --endpoint https://api.anthropic.com \
  --flavor anthropic --secret-ref keyring:bastet/anthropic
bastet grant add <resource_id> project:catswalker --budget-usd 5 --max-concurrency 2
bastet dispatch catswalker "..." --agent cc-worker --resource <resource_id>
```

Full walkthrough of every tab and command:
[docs/USER_GUIDE.md](docs/USER_GUIDE.md).

## Architecture

```
                    ┌─────────────────────────────────────────┐
   WebUI (React) ───┤  FastAPI: REST + WebSocket event bus    │
   CLI (Typer)   ───┤  auth: api token · user tokens · roles  │
   Telegram      ───┤                                         │
                    └───────┬──────────────────┬──────────────┘
                            │                  │
                  ┌─────────▼────────┐  ┌──────▼──────────────┐
                  │  Orchestrator    │  │  Gateway /v1/*      │
                  │  stages · gates  │  │  OpenAI + Anthropic │
                  │  rework loop     │  │  metering, budgets  │
                  └───┬──────────┬───┘  └──────┬──────────────┘
                      │          │             │
        ┌─────────────▼───┐  ┌───▼──────────┐  │
        │ Executor plugin │  │ git worktree │  │
        │ claude-code     │  │ or container │  │
        │ claude-sdk      │  │ per run      │  │
        │ codex · grok    │  └──────────────┘  │
        │ agy · hermes    │                    │
        │ bastet-lite ────┼────────────────────┘
        └─────────────────┘
                      │
        ┌─────────────▼───────────────────────────────────┐
        │ SQLite (WAL): projects · jobs · runs · gates     │
        │ usage_ledger · grants · audit_log (hash chain)   │
        └─────────────┬───────────────────────────────────┘
                      │
        ┌─────────────▼───────────────────────────────────┐
        │ Agent Memory OS: team/project ACL, context packs │
        └─────────────────────────────────────────────────┘
```

Design rationale and the data model: [SPEC.md](SPEC.md). How the project got
here, and why each decision went the way it did:
[docs/HISTORY.md](docs/HISTORY.md).

## When a gate says no

A failing test is an ordinary event in a development loop, so it does not stop
the board. The card goes **back** to a stage that can fix it — past read-only
reviewers to the last stage that writes — carrying the gate's real output, and
the pipeline continues without anyone being asked to intervene.

The brief that travels with it names the shortcuts explicitly: do not edit the
test command, delete the test, make the assertion trivially true, add
skip/xfail, or touch the workflow config. The cheapest way to pass a gate is to
weaken it, and an agent told only "make it green" will.

Three things still stop and ask you:

| Situation | Why a human |
|---|---|
| `on_fail: block` on the stage | a deploy or release step should not be retried in a loop by an agent |
| nothing earlier can write | a pipeline of read-only stages has nobody able to act |
| cycles exhausted (`max_cycles`, default 3) | an agent that has failed three times is not converging |

Whatever the agents produce is committed to the job's own `bastet/<job_id>`
branch when the run ends, so a finished loop leaves reviewable work rather than a
diff file. Your own branch is never written to: merging is a deliberate stage.

Each hand-back is audited as `job.rework` and counted on the card. The
notification for one reads as progress (what failed, who is fixing it, cycle N of
M); the notification for a genuine stop carries the failing output and a retry
button.

## Project lifecycle

A project has a state, shown as a light on its card: **planning → ready →
running ⇄ paused → maintenance → closed** (and reopen). Only declared
transitions are allowed and each one is audited, so the light is the truth, not
a guess derived from job rows.

Between planning and execution sits a human. The project-manager agent turns the
agreed plan into a task list (read-only: it sees the repo, the workflow stages
and the planning conversation), you edit and confirm it, and only then does the
runner dispatch — task by task, each following the project's workflow and role
assignments. A task waiting at a gate keeps the runner waiting; it never
approves anything itself. When every task settles the project moves to
maintenance, awaiting your acceptance.

Controls on the card: ▶ run, ⏸ pause (stops the *next* dispatch, current task
finishes), ■ stop (cancels what is in flight), close, reopen, delete.

## Chat: the human end of the loop

The 對話 tab is where a person plans the project by talking about it. A session
picks who answers — an **agent** (through its own executor and account,
read-only, with the project's repo in view) or a **pool LLM** — and a scope:
project, team, or global. Project scope carries the real project state into the
prompt: description, repo, workflow, team roles, recent jobs, and the resources
it may use.

Sessions are stored per project, so the discussion cannot drift from the org the
runs execute against. Files, documents and screenshots go in, every turn is
written to Agent Memory OS in the session's scope, and the session is also where
authorisation happens: pending human-approval gates are listed with
Approve/Reject, and the whole discussion can be dispatched as a job. The agent
never dispatches itself — a person presses the button.

Telegram is the second such channel: give a channel a responder and a project on
the Admin tab, and plain messages to the bot are answered in a per-user session
that survives restarts, attachments included.

## Resource pool

Resources are classified (`llm` · `mcp` · `api` · `skill` · `git` · media) and
each one carries its own visibility scope — global, team, or project. The
credential field is a picker over the credentials saved on the Admin tab: the
resource stores a `secret:<id>` pointer, so rotating a key updates every
resource that uses it. Kinds that need no credential (skills) don't show one.

MCP servers keep the vendor's install command; you run it from the WebUI and get
the full output back, so a failed install can be fixed in place and retried.
Nothing installs implicitly.

Every resource has a **test button**: it does what an agent would, per kind —
lists models for an LLM (a listing, never a completion, so testing costs no
tokens), completes a real MCP `initialize` handshake and reports the server's
tool list, checks a skill source exists on the Bastet host, verifies a git
credential against the provider over HTTPS or SSH. The verdict is three-state:
`ok`, `warn` (it answered, but not the way we hoped — reachable-but-404 is a
different bug from host-down), and `failed`, with the exact request that was
made.

Granted resources are callable by the agents running that project. At run start
Bastet hands them over as env vars (`BASTET_RES_<NAME>_URL` / `_KEY` / `_TOKEN` /
`_MODEL` / `_SOURCE`), an `mcpServers` config file (`BASTET_MCP_CONFIG`, and
`--mcp-config` for Claude Code), and a manifest written into the task brief. The
MCP file contains resolved credentials, so it lives outside the worktree at 0600
and is deleted when the run ends.

## Workflow gate tools

A `tests-pass` gate runs its command **on the Bastet host**, with the service's
PATH — not inside a project's virtualenv. The shipped presets use `pytest -q`,
`npm test`, and `make test`, so `install.sh` installs pytest alongside Bastet and
`bastet doctor` reports every program the configured templates need, naming the
template that needs it:

```
  ✓ gate tool `npm` → /usr/bin/npm
  ✗ gate tool `pytest` not found — 內建範本 前後端程式開發 的測試關卡會失敗
```

Bastet's own venv is placed **last** on PATH, so a project that provides its own
runner wins. For a project with its own environment, put the explicit path in the
template's command (`.venv/bin/pytest -q`, `npx vitest run`).

A command that cannot run at all is reported as a configuration problem rather
than a failing test — and handed back to an agent that can add the missing
script or dependency, with instructions not to fake a green exit.

## Team memory

Every run writes to Agent Memory OS, whichever executor drove it: what each
stage did (attributed to that agent's AMOS id), what a gate rejected, and how the
job ended. Context packs are read *as the running agent*, so AMOS's ACL applies
and one project's memories stay out of another project's runs.

Semantic recall needs `turbovec` (it ships with `agent-memory-os[full]`). Without
it AMOS silently falls back to keyword matching, so the memory tab states which
mode is live and the maintenance card lists the package.

## Keeping it current

Bastet runs other people's tools, so the Admin tab lists each component — Bastet
itself, Agent Memory OS, turbovec, the Claude Agent SDK, pytest, and the `claude`
/ `codex` / `grok` / `agy` / `hermes` CLIs — with its installed and available
version, updatable one at a time or all at once.

Nothing updates itself. Changing the agents underneath a running project is not
something you could reason about afterwards, so an update happens when you press
the button and is audited. A component whose available version cannot be
determined (an official install script with no version query) reports `unknown`
instead of implying it is current, and an installer that ran cleanly without
moving the version reports `unchanged` rather than claiming success.

## Languages

The WebUI ships in **繁體中文 · 简体中文 · English · 日本語 · 한국어**. The locale
is picked from the browser (`zh-TW/HK/MO` → traditional, other `zh` →
simplified) and switchable from the header; the choice is remembered per browser.
Workflow roles and gate types are localised by their stable ids, so a stage
stored as `role: "reviewer"` reads correctly in every language.

Adding UI strings: put them in `web/src/i18n/zh-Hant.ts` (the canonical
dictionary) and translate in the other four files — they are typed against it, so
a missing key fails `npm run build`, and `tests/test_i18n.py` fails on a
hard-coded string that skipped `t()`.

## Documentation

| Document | What it covers |
|---|---|
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | install.sh, requirements, executor logins, running as a service, upgrading |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | every tab and every CLI command, end to end |
| [docs/USER_GUIDE.zh-Hant.md](docs/USER_GUIDE.zh-Hant.md) | 操作手冊 — the operating manual in Traditional Chinese |
| [docs/WORKFLOWS.md](docs/WORKFLOWS.md) | the workflow operations manual: every stage field, every gate, every stop and what to do about it |
| [docs/CAUTIONS.md](docs/CAUTIONS.md) | 注意事項 — every operational pitfall hit in production, and the boundary rules |
| [docs/HISTORY.md](docs/HISTORY.md) | the project journey and why each decision went the way it did |
| [docs/ROADMAP.md](docs/ROADMAP.md) | what is next, and what is deliberately not |
| [docs/FEDERATION.md](docs/FEDERATION.md) | the shared org view across hosts |
| [SPEC.md](SPEC.md) | design specification and data model (繁體中文) |
| [CHANGELOG.md](CHANGELOG.md) | every released version |
| [PROGRESS.md](PROGRESS.md) | current status snapshot |
| [COMPATIBILITY.md](COMPATIBILITY.md) | supported platforms, Python versions, executors |
| [SECURITY.md](SECURITY.md) | threat model, secret handling, reporting |
| [CONTRIBUTING.md](CONTRIBUTING.md) | how to work on this repo |

## Versioning

`src/bastet_agent_os/__init__.py` holds the single `__version__`;
`pyproject.toml` reads it, `web/package.json` matches it, and the WebUI prints it
beside the title (from `GET /api/version`, so it is the version actually
running). Every user-visible change bumps it and adds a `CHANGELOG.md` entry —
`tests/test_version.py` fails the build if they drift apart.

## Development

```bash
pip install -e '.[dev]'
pytest -q                     # 349 tests
ruff check .
cd web && npm install && npm run build   # output lands in src/bastet_agent_os/ui_dist
```

The web build output is committed, so a pip install from git serves the UI
without needing Node on the target host.

## Status

| Milestone | Scope | Status |
|---|---|---|
| M0 | SPEC, data model, repo skeleton | ✅ done |
| M1 | Resource pool + gateway + `claude-code` executor + CLI dispatch + dashboard | ✅ done |
| M2 | Workflow templates, review gates, Kanban UI, WS events | ✅ done |
| M3 | Multi-project concurrency, queueing, container isolation, `bastet-lite`, multi-user auth | ✅ done |
| M4 | Telegram channel, media resources, in-run interactions, `claude-sdk`/`codex`/`hermes` executors | ✅ done |
| M5 | Federation: shared org view over AMOS sync | ✅ done |
| M6 | Self-healing workflow loop, run memory for every executor, maintenance card | ✅ done |
| 0.19–0.22 | Supplies, previews, heartbeats, timezone; config-by-chat; auto-push; the media loop; quota self-wait; per-stage time budgets; PyPI + Docker | ✅ done |
| 0.23–0.24 | Docs set, badges and live screenshots; nothing a run spawns can wait on a prompt; every run heartbeats; verdict schemas bound to review gates only | ✅ done |
| 0.25–0.30 | PM-level supervision of business stalls; depleted agents leave the routing rotation; rework walks backwards; the card shows the PM's question; per-stage commits with the engine's scratch excluded; interrupt the dead, not the merely quiet | ✅ done |

Validated on a real deployment (Ubuntu 26.04, Python 3.14, systemd user service)
driving a live project. See [PROGRESS.md](PROGRESS.md) for what is verified and
what is still open.

## License

Apache-2.0. Built on [Agent Memory OS](https://github.com/yamantaka520/Agent-Memory-OS).
