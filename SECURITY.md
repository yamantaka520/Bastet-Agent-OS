# Security

## Reporting

Do not open a public issue for a vulnerability. Email the maintainer
(manfred.mobile@gmail.com) with what you found, how to reproduce it, and what it lets an
attacker do. You will get an acknowledgement, and a fix or an explanation of why
it is not exploitable.

Bastet is pre-1.0 software that runs autonomous agents with credentials. Treat
it accordingly: it is designed for a machine you control, not for exposure to
the internet.

## The threat model in one sentence

The agents are powerful, the content they read is untrusted, and the system's
job is to make sure that neither an agent's mistake nor a poisoned repository
can silently move credentials, money, or authority.

## What Bastet protects, and how

| Asset | How |
|---|---|
| API / user tokens | hashed at rest; plaintext shown once at creation; per-user revoke/rotate |
| Credentials (keys, tokens, PEM) | stored by reference (`keyring:` / `file:` 0600 / `env:`); **write-only** through the UI — renameable, rescopable, rotatable, never readable back |
| The audit trail | append-only, SHA-256 hash-chained; `verify_audit_chain()` detects tampering |
| Usage accounting | deletions refuse or force-with-disclosure; the written-off amount goes on the record |
| The network surface | loopback by default; non-loopback binds enable a Host allow-list (DNS-rebinding defence) |
| Run isolation | one git worktree per job, or a container per stage; the MCP config with resolved credentials lives outside the worktree at 0600 and is deleted at run end |
| Delivery credentials | auto-push matches credentials to remotes by **exact host only** — a GitLab token never travels to github.com; no match means an unauthenticated push and git's own refusal |

## Boundaries between the agent and the system

These are the places where agent-written content crosses into system behaviour,
and the rule at each one:

- **`._bastet/verdict.json`** — review verdicts travel in a file with a fixed
  schema precisely so that prose in a diff ("approve this") cannot decide a
  gate. Missing or malformed = rejected.
- **`._bastet/preview/` and the chat outbox** — directories the agent writes
  and the system collects. Collection refuses symlinks and anything resolving
  outside the directory: a symlink named `x.png` pointing at
  `~/.bastet/api_token` would otherwise copy the token into artifacts and send
  it to Telegram as a "photo". Size and count caps apply.
- **`._bastet/inbox/` (supplies)** — data handed *to* a running job. Supplies
  travel inside prompts, and prompts go to LLM providers — so credential-shaped
  content is refused with a pointer to the credentials card; secrets arrive as
  env vars and never ride a prompt.
- **`bastet-config` proposals** — the model proposes, the human's click is the
  authority, and the audit row names the human. The op whitelist covers
  resources, grants and the display timezone only: users, tokens and channels
  are *not* in it, so a prompt-injected "add an admin" finds nothing to call.
  Proposals accept `secret:<id>` pointers **only** (a model-proposed `file:` or
  `env:` ref could exfiltrate host files as "credentials"); credential rows and
  the built-in `bastet-config` skill itself are not editable from chat.
- **Preview/file endpoints** — served with resolved-path containment checks,
  not name sanitisation (`Path("..").name` is `".."`; the first attempt at that
  fix was disproven by its own test).

## Roles

| Role | Can |
|---|---|
| `viewer` | read everything except secret values |
| `operator` | dispatch, approve, retry, supply, edit the org, assign roles, attach resources, chat |
| `admin` | everything, including users, credentials, channels, MCP/skill installs, config-apply, project deletion |

Permission changes take effect immediately; tokens do not need re-issuing.

## Trust boundaries you should know about

- **Agents run with the host user's privileges** inside their worktree.
  Container isolation is available per stage and is the right choice for
  untrusted work.
- **The chat responder has Bash.** That is what makes granted media APIs
  actually callable from a conversation — and it means the responder can act on
  the host. `chat.send` is an operator permission; issue tokens accordingly.
  The chat tool set excludes Edit/Write (a conversation does not edit the repo),
  and the secret-bearing resource-access directory is removed when the reply
  ends.
- **Repository content is untrusted data.** Review briefs say so explicitly;
  instructions found inside diffs, task text, or web pages are ignored by
  policy and cannot reach a decision except through a human's click.
- **A gate command runs on the host** with the service's PATH. Whoever can edit
  a workflow template can run a command on the control-plane host: template
  editing is an operator capability and is audited.
- **Credentials injected into a run are readable by that run.** Prefer
  short-lived, minimum-scope tokens; grant per project rather than globally.
- **Install commands never run as a side effect.** Applying a chat proposal
  creates the resource; executing its `install_command` is a separate,
  admin-pressed, fully-logged button.
- **The gateway meters, it does not sanitise.** It is an accounting and budget
  boundary, not a content filter.

## Secret handling specifics

- Raw values pasted into a ref field are written to `~/.bastet/secrets/<hint>`
  (0600) and replaced with a `file:` ref — never left in the database.
- A one-line PEM paste is re-wrapped using its BEGIN/END markers — a repair of
  known structure, not a guess.
- Secrets are masked in logs and errors (`abc…yz`); the httpx logger is pinned
  to WARNING because its INFO lines once contained a bot token in a URL.
- `config_json` is schema-guarded against secret-looking keys.
- Deploy keys for SSH pushes are written to 0600 files in a temp dir that is
  removed when the push ends; tokens travel in env-provided headers, never in
  argv or URLs.

## Quota / availability

Vendor quota failures are parsed for their stated reset time. The parser never
mistakes an ordinary failure for a timer, unknown timezones fall back to UTC,
nonsense times fall back to a 30-minute backoff, and waits are capped at 26
hours — a mis-parse can delay a retry, never execute anything.

## Known gaps

- Rotated files accumulate in `~/.bastet/secrets`; nothing prunes them.
- No TLS termination of its own — put a reverse proxy in front if it must
  leave the LAN.
- No rate limiting on the control-plane API beyond per-grant concurrency caps.
- `curl | bash` installation is offered for convenience; the sceptical path is
  `pip install bastet-agent-os` after reading the wheel's contents.
