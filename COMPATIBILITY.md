# Compatibility

Current version: 0.34.8

## Platforms

| Platform | Status |
|---|---|
| Linux (Ubuntu 24.04 / 26.04) | supported — the validation host runs Ubuntu 26.04 with a systemd user service |
| macOS (Apple Silicon, Intel) | supported — primary development platform |
| Windows 10/11 | runs (control plane + CLI), but the test suite is not green there yet — the CI leg is informational. Treat as experimental; container isolation needs Docker Desktop |

## Python

| Version | Status |
|---|---|
| 3.14 | tested (host and development) |
| 3.13 | supported |
| 3.12 | supported |
| 3.11 | minimum |

## Executors

| Executor | Kind | Accounts | Notes |
|---|---|---|---|
| `claude-code` | Claude Code CLI | multiple, via `CLAUDE_CONFIG_DIR` | subscription or API path |
| `claude-sdk` | Claude Agent SDK (in-process) | one | needs `claude-agent-sdk` |
| `codex` | OpenAI Codex CLI | multiple, via `CODEX_HOME` | `/v1/responses` metering supported |
| `grok` | xAI Grok Build CLI | multiple, via `GROK_HOME` | pretty-prints JSON; the parser is tolerant of it |
| `agy` | Google Antigravity | one global Google login | login is browser-based |
| `hermes` | NousResearch Hermes | default or isolated `HERMES_HOME` | direct path uses the logged-in provider; Gateway path uses a temporary Bastet profile and requires OpenAI flavor + model |
| `pi` | Pi Coding Agent | multiple, via `PI_CODING_AGENT_DIR` | ephemeral JSONL runs; explicit writable/read-only tool allowlists; OpenAI and Anthropic Gateway profiles supported |
| `openclaw` | OpenClaw `agent exec` | multiple, via `OPENCLAW_HOME` | direct path only in this release; isolated temporary run state; writable code/light-task stages only |
| `bastet-lite` | built in | n/a | credentials come from the resource pool |

Each vendor CLI must be installed **and logged in** on the control-plane host.
Logins are interactive by design and cannot be automated for you.

Before creating a run, Bastet filters role candidates by their executor route
contract. Gateway-only/direct-only mismatches, wrong API flavors, missing models,
missing grants, and unsupported read-only work are rejected during routing rather
than counted as executor failures. Explicitly choosing an incompatible agent is
an actionable error; automatic routing moves to the next compatible candidate.

## LLM endpoints (gateway)

| Wire format | Status |
|---|---|
| Anthropic Messages (`/v1/messages`) | supported, metered |
| OpenAI Chat Completions (`/v1/chat/completions`) | supported, metered |
| OpenAI Responses (`/v1/responses`) | passthrough with metering (used by codex) |
| Any OpenAI-compatible vendor | works if it honours one of the above |

## Agent Memory OS

| AMOS | Status |
|---|---|
| 1.8.x | tested (1.8.1 on the validation host) |
| with `[full]` / `[semantic]` | turbovec + numpy enable vector recall; without them AMOS falls back to keyword matching and Bastet says so |

## Browsers

The WebUI is built with Vite + React and targets current Chrome, Edge, Safari and
Firefox. It needs WebSocket support for the live board and the login wizard
terminal.
