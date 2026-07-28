# Bastet Agent OS

> ⚠️ Pre-alpha — design phase (M0 complete). See [SPEC.md](SPEC.md) for the full design.

**A local-first operating system for AI-agent teams.** Bastet organizes the
agents you already use — Claude Code, Codex CLI, Hermes, or any
OpenAI/Claude-compatible endpoint — into teams with roles, gated workflows,
and centrally governed resources, so multiple projects can run concurrently
under control.

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

## 繁體中文

**Local-first 的 AI agent 團隊作業系統。** 把你既有的 agent（Claude Code、
Codex CLI、Hermes、任何 OpenAI/Claude 相容端點）組織成有角色、有流程、有
資源治理的執行團隊，讓多個專案可控地併發推進。詳見 [SPEC.md](SPEC.md)。

## Status

| Milestone | Scope | Status |
|---|---|---|
| M0 | SPEC, data model, repo skeleton | ✅ done |
| M1 | Resource pool + gateway + `claude-code` executor + CLI dispatch + minimal dashboard | ⏳ next |
| M2 | Workflow templates, Kanban, worktree/container isolation, review gates, audit | planned |
| M3 | Multi-project concurrency, resource arbitration, `bastet-lite` + full dynamic context | planned |
| M4 | Telegram channel, media resource kinds, `codex`/`hermes` executors | planned |
| M5 | Federation (multi-node) | planned |
