# Bastet Agent OS

> ⚠️ Pre-alpha — design phase (M0 complete). See [SPEC.md](SPEC.md) for the full design.

**A local-first operating system for AI-agent teams.** Bastet organizes the
agents you already use — Claude Code (headless or Agent SDK), Codex CLI,
Hermes, Grok Build, Google Antigravity (`agy`), or any OpenAI/Claude-compatible
endpoint — into teams with roles, gated workflows, and centrally governed
resources, so multiple projects can run concurrently under control.

Bastet is a **control plane, not another agent framework**: execution comes
from orchestrating existing agents; its moat is governance and memory —

- **Resource pool** — allocation, quotas, budgets, metering, and routing for
  LLM / MCP / media resources through a built-in OpenAI/Claude-compatible
  gateway.
- **Gated workflows** — stage pipelines (plan → implement → test → code
  review → security review → merge) with a Kanban view, git-worktree or
  container isolation per run, and an append-only audit log.
- **Team memory** — built on [Agent Memory OS](https://github.com/yamantaka520/Agent-Memory-OS):
  teams/projects with a hard ACL, associative recall, dynamic token-budgeted
  context packs, and cross-node federation.

Linux · macOS · Windows / WebUI + CLI / Apache-2.0.

## Languages

The WebUI ships in **繁體中文 · 简体中文 · English · 日本語 · 한국어**. The locale
is picked from the browser (`zh-TW/HK/MO` → traditional, other `zh` →
simplified) and switchable from the header; the choice is remembered per
browser. Workflow roles and gate types are localised by their stable ids, so a
stage stored as `role: "reviewer"` reads correctly in every language.

Adding UI strings: put them in `web/src/i18n/zh-Hant.ts` (the canonical
dictionary) and translate in the other four files — they are typed against it,
so a missing key fails `npm run build`, and `tests/test_i18n.py` fails on a
hard-coded string that skipped `t()`.

Built-in workflow presets keep their authored stage text: the preset name
becomes the template id when you copy it, so the display must match what gets
saved. Copy a preset and rewrite it in any language.

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
finishes), ■ stop (cancels what is in flight), close, reopen. Tasks are ordinary
jobs, so they show up on the Kanban board and move across stage columns.

## Chat: the human end of the loop

The 對話 tab is where a person plans the project by talking about it. A session
picks who answers — an **agent** (through its own executor and account,
read-only, with the project's repo in view) or a **pool LLM** — and a scope:
project, team, or global. Project scope carries the real project state into the
prompt: description, repo, workflow, team roles, recent jobs, and the resources
it may use.

Sessions are stored per project, so the discussion can't drift from the org the
runs execute against. Files, documents and screenshots go in (text is inlined,
images ride along where the wire supports it), every turn is written to Agent
Memory OS in the session's scope so the next run inherits it, and the session is
also where authorisation happens: pending human-approval gates are listed with
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

MCP servers keep the vendor's install command; you run it from the WebUI and
get the full output back, so a failed install can be fixed in place and
retried. Nothing installs implicitly.

Every resource has a **test button**: it does what an agent would, per kind —
lists models for an LLM (a listing, never a completion, so testing costs no
tokens), completes a real MCP `initialize` handshake and reports the server's
tool list, checks a skill source exists on the Bastet host, verifies a git token
against the provider. The verdict is three-state: `ok`, `warn` (it answered, but
not the way we hoped — reachable-but-404 is a different bug from host-down), and
`failed`, with the exact request that was made.

Granted resources are callable by the agents running that project. At run start
Bastet hands them over as env vars (`BASTET_RES_<NAME>_URL` / `_KEY` /
`_TOKEN` / `_MODEL` / `_SOURCE`), an `mcpServers` config file
(`BASTET_MCP_CONFIG`, and `--mcp-config` for Claude Code), and a manifest
written into the task brief. The MCP file contains resolved credentials, so it
lives outside the worktree at 0600 and is deleted when the run ends.

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
than a failing test — re-running the agent cannot fix a missing script.

## Keeping it current

Bastet runs other people's tools, so the Admin tab lists each component — Bastet
itself, Agent Memory OS, the Claude Agent SDK, pytest, and the `claude` /
`codex` / `grok` / `agy` / `hermes` CLIs — with its installed and available
version, updatable one at a time or all at once.

Nothing updates itself. Changing the agents underneath a running project is not
something you could reason about afterwards, so an update happens when you press
the button and is audited. A component whose available version cannot be
determined (an official install script with no version query) reports `unknown`
instead of implying it is current, and an installer that ran cleanly without
moving the version reports `unchanged` rather than claiming success.

## Versioning

`src/bastet_agent_os/__init__.py` holds the single `__version__`;
`pyproject.toml` reads it, `web/package.json` matches it, and the WebUI prints
it beside the title (from `GET /api/version`, so it is the version actually
running). Every user-visible change bumps it and adds a `CHANGELOG.md` entry —
`tests/test_version.py` fails the build if the four drift apart.

## 繁體中文

**Local-first 的 AI agent 團隊作業系統。** 把你既有的 agent（Claude Code、
Codex CLI、Hermes、任何 OpenAI/Claude 相容端點）組織成有角色、有流程、有
資源治理的執行團隊，讓多個專案可控地併發推進。詳見 [SPEC.md](SPEC.md)。

## Quickstart (M1)

```bash
pip install -e '.[dev]'         # from a clone; PyPI release comes later
bastet init                     # creates ~/.bastet (db, api token, config)
bastet serve                    # control plane + gateway on 127.0.0.1:8890
```

In another terminal:

```bash
# register a project (1:1 with an AMOS project), an agent, and dispatch
bastet project add myproj /path/to/repo
bastet agent add cc-worker --name "Claude Code Worker"
bastet dispatch myproj "Fix the failing test in tests/test_foo.py" --agent cc-worker

bastet runs                     # run status
bastet run <run_id>             # detail: usage ledger, diff artifact
bastet usage                    # cost by project/agent/precision
bastet audit                    # append-only audit trail
bastet doctor                   # health checks
```

The Kanban web UI lives at `http://127.0.0.1:8890/ui` — paste the token from
`~/.bastet/api_token`, watch jobs move across stage columns live, and
approve/reject `human-approve` gates from the job drawer. Multi-stage
pipelines come from templates:

```bash
bastet template add standard-dev.yaml
bastet role-assign myproj reviewer-agent reviewer
bastet dispatch myproj "..." --agent cc-worker --template standard-dev
```

To meter traffic through the gateway instead of the subscription path,
register an LLM resource + grant and pass `--resource`:

```bash
bastet resource add anthropic-api --endpoint https://api.anthropic.com \
  --flavor anthropic --secret-ref keyring:bastet/anthropic
bastet grant add <resource_id> project:myproj --budget-usd 5 --max-concurrency 2
bastet dispatch myproj "..." --agent cc-worker --resource <resource_id>
```

## Status

| Milestone | Scope | Status |
|---|---|---|
| M0 | SPEC, data model, repo skeleton | ✅ done |
| M1 | Resource pool + gateway + `claude-code` executor + CLI dispatch + minimal dashboard | ✅ done (final acceptance pending) |
| M2 | Workflow templates, review gates, Kanban UI, WS events | ✅ done |
| M3 | Multi-project concurrency, queueing, container isolation, `bastet-lite` + full dynamic context, multi-user auth | ✅ done |
| M4 | Telegram channel, media resources, in-run interactions, `claude-sdk`/`codex`/`hermes` executors | ✅ done |
| M5 | Federation: shared org view over AMOS sync ([docs](docs/FEDERATION.md)) | ✅ done |
