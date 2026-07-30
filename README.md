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
