# Installation

Bastet runs on one machine — the *control plane host*. That host is where the
repos live, where the executor CLIs are logged in, and where gate commands run.
Everyone else uses its WebUI or CLI over the network.

## Requirements

| | |
|---|---|
| OS | Linux, macOS, or Windows |
| Python | 3.11 – 3.14 |
| git | required — every run happens in a git worktree of the project's repo |
| Node | **not** required on the host; the built UI is committed to the repo |
| Docker | optional, only for container-isolated stages |

Each executor you intend to use must be installed *and logged in* on this host.
Logging in is interactive (OAuth or a vendor key) so it cannot be automated for
you — see [Executor logins](#executor-logins).

## One-click install (macOS / Linux)

```bash
curl -fsSL https://raw.githubusercontent.com/yamantaka520/Bastet-Agent-OS/main/install.sh | bash
```

What it does:

1. creates `~/.bastet/venv`
2. installs Bastet, `agent-memory-os[full]` (which brings turbovec for semantic
   recall), `claude-agent-sdk`, and `pytest` (the shipped workflow presets use it
   as a gate command)
3. installs the executor CLIs using **each vendor's own official installer** —
   Claude Code, OpenAI Codex, xAI Grok Build, Google Antigravity (`agy`),
   NousResearch Hermes
4. runs `bastet init`, installs the auto-restart service, and finishes with
   `bastet doctor`

Flags:

| Flag | Effect |
|---|---|
| `--minimal` | Bastet + AMOS only; skip the executor CLIs |
| `--executors "claude,codex"` | install only these |
| `--upgrade` | re-run the installers even when a tool is already present |
| `--no-service` | don't install the boot/login auto-restart service |
| `--lan` | bind `0.0.0.0` for LAN access (the Host allow-list stays on) |
| `BASTET_REPO=…` | override the pip source (default: GitHub `main`) |

`bastet-lite` needs no installation — it is built in, and takes its credentials
from the resource pool.

## Manual install

```bash
python3 -m venv ~/.bastet/venv
~/.bastet/venv/bin/pip install "git+https://github.com/yamantaka520/Bastet-Agent-OS.git"
~/.bastet/venv/bin/pip install "agent-memory-os[full]" claude-agent-sdk pytest
~/.bastet/venv/bin/bastet init
~/.bastet/venv/bin/bastet serve
```

There is no PyPI release yet; installation is from source. That is also what the
maintenance card's update button uses.

For development, from a clone:

```bash
pip install -e '.[dev]'
pytest -q
```

## What `bastet init` creates

```
~/.bastet/
├── bastet.db          SQLite (WAL): projects, jobs, runs, gates, usage, audit
├── api_token          the admin token — this is the WebUI login
├── config.json        host/port, allowed hosts, AMOS console URL
├── worktrees/         one git worktree per running job
├── artifacts/         per-job diffs
├── secrets/           credential files (0600) for raw values that were pasted
└── venv/              only when installed by install.sh
```

Nothing here is shared: the token is the only credential, and it is a file on
this host.

## Running it

```bash
bastet serve                      # 127.0.0.1:8890
bastet serve --host 0.0.0.0       # LAN
```

Then open `http://<host>:8890/ui` and paste the token from `~/.bastet/api_token`.

### As a service (boot / login auto-start)

```bash
bastet service install     # systemd user unit / launchd agent / Task Scheduler
bastet service status
bastet service uninstall
```

On Linux this is a **user** unit, so:

```bash
systemctl --user status bastet
systemctl --user restart bastet
journalctl --user -u bastet -f
```

If the service should keep running when you are not logged in, enable lingering:
`sudo loginctl enable-linger $USER`.

### LAN access and the Host guard

Binding a non-loopback address turns on an allow-list built from this machine's
own names and addresses, plus anything in `config.json`'s `allowed_hosts`. A
request whose `Host` header is not on that list is refused with 403 — this is DNS
rebinding protection and it is not weakened by LAN mode, because a rebound
request carries the attacker's domain, not your address.

## Executor logins

Each vendor CLI keeps its own credentials and each login is interactive, so
Bastet cannot do it for you. Two ways:

1. **On the host, in your own terminal** — `claude`, `codex`, `grok`, `agy`,
   `hermes` each have their own login command.
2. **From the WebUI's login wizard** (組織 tab → the agent's account) — a real
   terminal in the browser, driving the CLI on the host. Use this when you are
   not sitting at that machine.

`bastet doctor` reports which executors are installed. The 組織 tab shows a
three-state label per executor: 未安裝 / 未設定（installed but not logged in）/
ready.

Multiple accounts per executor are supported: each account gets its own profile
directory under `~/.bastet/executor-profiles/<id>`, injected per run through
`CLAUDE_CONFIG_DIR` / `CODEX_HOME` / `GROK_HOME`.

## Gate tools

A `tests-pass` gate runs its command on this host with the *service's* PATH.
`bastet doctor` lists every program the configured templates need and names the
template that needs it:

```
  ✓ gate tool `npm` → /usr/bin/npm
  ✗ gate tool `pytest` not found — 內建範本 前後端程式開發 的測試關卡會失敗
```

Bastet's own venv goes **last** on PATH, so a project that provides its own
runner wins. For a project with its own environment, put the explicit path in the
template (`.venv/bin/pytest -q`, `npx vitest run`).

## Upgrading

From the WebUI: 管理 tab → 維護 card → 更新, per component or all at once. It
lists Bastet itself, Agent Memory OS, turbovec, numpy, the Claude Agent SDK,
pytest, and each executor CLI, with installed vs available versions. Nothing
updates itself, and updating Bastet asks you to restart the service.

By hand:

```bash
~/.bastet/venv/bin/pip install --upgrade \
  "git+https://github.com/yamantaka520/Bastet-Agent-OS.git"
systemctl --user restart bastet          # or: bastet service restart
curl -s localhost:8890/api/version
```

**Note for non-editable installs from git:** pip will not reinstall when the
version number has not changed. Add `--force-reinstall --no-deps` when you are
pulling the same version's newer commit.

## Verifying the install

```bash
bastet doctor            # database, AMOS, executors, gate tools, service
curl -s localhost:8890/api/version
```

`doctor` run in the same shell as the installer can report `claude` as missing
because PATH has not been re-sourced yet — harmless, and it clears in a new
shell.

## Uninstall

```bash
bastet service uninstall
rm -rf ~/.bastet          # database, token, config, worktrees, artifacts
```

The executor CLIs and Agent Memory OS (`~/.agent-memory`) are separate installs
and are left alone.
