# 🐈 Bastet Agent OS

**A local-first operating system for AI-agent teams.** Bastet organizes the
agents you already use — Claude Code (CLI or Agent SDK), Codex CLI, Grok Build,
Google Antigravity (`agy`), Hermes, Pi, OpenClaw, or any OpenAI/Claude-compatible endpoint —
into teams with roles, gated workflows, and centrally governed resources, so
several projects can run concurrently under control.

Bastet is a **control plane, not another agent framework**. Execution comes from
orchestrating agents that already exist; what Bastet adds is governance, a
workflow engine that keeps going when things fail, and team memory.

- 📖 **Source, docs, screenshots:** https://github.com/yamantaka520/Bastet-Agent-OS
- 📦 **PyPI (the other install path):** https://pypi.org/project/bastet-agent-os/
- 🈶 **繁體中文說明:** https://github.com/yamantaka520/Bastet-Agent-OS/blob/main/README.zh-Hant.md

## Quick start

```bash
docker run -d --name bastet -p 8890:8890 \
  -v bastet-data:/data \
  -v /path/to/your/repos:/repos \
  yamantaka520/bastet-agent-os:latest

docker exec bastet cat /data/api_token
```

Open `http://localhost:8890/ui` and paste that token. All state — SQLite
database, credentials, artifacts, and the Agent Memory OS store — lives under
`/data`, so that one volume is the whole backup.

Projects point at repository paths **as the container sees them**
(`/repos/your-project`), so mount the directory that holds your work.

## Supported tags

`latest` follows the most recent release; version tags (`0.23.2`, `0.23.1`, …)
are immutable. Platforms: `linux/amd64` and `linux/arm64`.

## What is — and is not — in the image

Python 3.12, Bastet (control plane, gateway, WebUI, and the `bastet-lite`
executor), `git`, and the standard gate/media tooling: Agent Memory OS with
semantic recall, `pytest` for test gates, Pillow for media assets, and
**Playwright with chromium** for preview screenshots.

**The vendor CLIs are deliberately absent** (claude, codex, grok, agy, hermes):
their logins are interactive and their credentials are yours, so they do not
belong baked into a public image. Two ways to get real executor work done:

- configure an **OpenAI/Claude-compatible endpoint as a resource** — that runs
  from this image as-is, and the gateway meters it; or
- run Bastet **on a host** (`pip install bastet-agent-os`) where those CLIs are
  already logged in, and keep this image for gateway/board/memory duty, or
  install the CLIs into your own derived image.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `BASTET_HOME` | `/data` | every piece of state |
| `AGENT_MEMORY_HOME` | `/data/agent-memory` | the memory store |

Credentials are **not** environment variables. Add them in the WebUI under
管理 → 憑證 (Admin → Credentials), where they are stored by reference, masked in
logs, and never readable back.

The container runs as **uid 1000**, and `/data` is created and chowned in the
image — mount a host directory only if it is writable by that uid (a named
volume just works). The server binds `0.0.0.0` because the container boundary
replaces the loopback default; the Host allow-list stays on.

## Security note

Bastet runs autonomous agents with real credentials on the machine you give it.
It is built for a host you control, ships no TLS of its own, and belongs behind
a reverse proxy if it ever leaves your LAN. Read
[SECURITY.md](https://github.com/yamantaka520/Bastet-Agent-OS/blob/main/SECURITY.md)
first — it documents the threat model, the boundaries where agent-written
content crosses into system behaviour, and the known gaps.

## License

Apache-2.0. Pre-1.0 software: read
[CAUTIONS.md](https://github.com/yamantaka520/Bastet-Agent-OS/blob/main/docs/CAUTIONS.md)
for the production pitfalls.
