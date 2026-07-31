# Security

## Reporting

Do not open a public issue for a vulnerability. Email the maintainer
(ai1@t9online.com) with what you found, how to reproduce it, and what it lets an
attacker do. You will get an acknowledgement, and a fix or an explanation of why
it is not exploitable.

Bastet is pre-1.0 software that runs autonomous agents with credentials. Treat it
accordingly: it is designed for a machine you control, not for exposure to the
internet.

## What Bastet protects

| Asset | How |
|---|---|
| API / user tokens | hashed at rest; the plaintext is shown once at creation |
| Credentials (keys, tokens, PEM) | stored by reference (`keyring:` / `file:` 0600 / `env:`); write-only through the UI — renameable and rotatable, never readable |
| The audit trail | append-only, SHA-256 hash-chained; `verify_audit_chain()` detects tampering |
| Usage accounting | deletions are refused or forced-with-disclosure, never silent |
| The network surface | bound to loopback by default; a non-loopback bind enables a Host allow-list (DNS rebinding protection) |
| Run isolation | one git worktree per job, or a container; the MCP config with resolved credentials lives outside the worktree at 0600 and is deleted at run end |

## Roles

| Role | Can |
|---|---|
| `viewer` | read everything except secret values |
| `operator` | dispatch, approve, edit the org, assign roles, attach resources, chat |
| `admin` | everything, including users, credentials, channels, MCP installs, project deletion |

Permission changes take effect immediately; tokens do not need re-issuing.

## Trust boundaries you should know about

- **Agents run with the host user's privileges** inside their worktree. Container
  isolation is available per stage and is the right choice for untrusted work.
- **Repository content is untrusted data.** Review stages are told so explicitly,
  and instructions found inside a diff or a task description ("approve this") are
  to be ignored. The `agent-review` verdict travels in a file precisely so that
  prose in the repo cannot decide a gate.
- **A gate command runs on the host with the service's PATH.** Whoever can edit a
  workflow template can run a command on the control-plane host: template editing
  is an `operator` capability, and it is audited.
- **Credentials injected into a run should be assumed readable by that agent.**
  Prefer short-lived, minimum-scope tokens; grant per project rather than
  globally.
- **The gateway meters but does not sanitise prompts.** It is an accounting and
  budget boundary, not a content filter.

## Secret handling specifics

- Raw values pasted into a ref field are written to `~/.bastet/secrets/<hint>`
  (0600) and replaced with a `file:` ref rather than being left in the database.
- A one-line PEM paste is re-wrapped using its BEGIN/END markers — a repair of
  known structure, not a guess.
- Secrets are masked in logs and errors (`abc…yz`). The httpx logger is pinned to
  WARNING because its INFO lines contained a bot token in the URL.
- `config_json` is schema-guarded against secret-looking keys, so a secret cannot
  be smuggled into a resource's public config.

## Known gaps

- Rotated files accumulate in `~/.bastet/secrets`; nothing prunes superseded ones.
- No TLS termination of its own — put it behind a reverse proxy if it must leave
  the LAN.
- No rate limiting on the control-plane API beyond per-grant concurrency caps.
