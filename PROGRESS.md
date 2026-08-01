# Bastet Agent OS Progress

Last updated: 2026-08-02

## Current project status

- Released: **v0.19.0** — on [PyPI](https://pypi.org/project/bastet-agent-os/)
  and [Docker Hub](https://hub.docker.com/r/yamantaka520/bastet-agent-os)
  (amd64 + arm64). Version arc v0.1.0 → v0.19.0 in six days (2026-07-28 →
  2026-08-02), 90 commits. See [CHANGELOG.md](CHANGELOG.md) for the
  full trail and [docs/HISTORY.md](docs/HISTORY.md) for why each decision went the
  way it did.
- Milestones M0–M6 complete: control plane, gateway, workflow engine, Kanban,
  multi-project concurrency, multi-user auth, Telegram channel, federation, and
  the self-healing rework loop.
- Test suite: **349 passing**, `ruff` clean. 40 test modules.
- Executors implemented: `claude-code`, `claude-sdk`, `codex`, `grok`, `agy`,
  `hermes`, `bastet-lite`.
- WebUI in five languages, typed against a canonical dictionary so a missing
  translation fails the build.

## Validation deployment

Bastet runs as a systemd **user** service on a second machine, which is where
every feature is confirmed against real agents rather than fakes:

| | |
|---|---|
| Host | Ubuntu 26.04 LTS, Python 3.14.4 |
| Install | `~/.bastet/venv`, non-editable, from the GitHub source |
| Service | `systemctl --user` unit, `bastet serve` on `0.0.0.0:8890` |
| Executors | `claude` 2.1.220, `codex` 0.145.0, `grok` 0.2.112, `agy` 1.1.9, `hermes` 0.19.0 |
| Memory | Agent Memory OS 1.8.1 with turbovec 0.8.0 — semantic recall **active** |
| Live project | CatsWalker (a real game project), running through the workflow engine |

### Verified there, end to end

- **The rework loop.** A repo with a deliberately wrong `add()` went: dispatch →
  `gate.failed` (pytest assertion) → `job.rework` → a real Claude Code agent
  fixed it → `gate.passed` → `job.done`, with `rework_count=1` and no human step.
  The same loop is running on CatsWalker (`E2E 測試` failed, was handed back, and
  the card finished).
- **Work preservation.** The job's branch `bastet/<job_id>` holds the agent's
  commit; the project's own branch is untouched. This was found *broken* during
  validation — cleanup was deleting uncommitted work — and fixed in v0.18.0.
- **Run memory for every executor.** Claude Code runs now write
  `note` / `warning` / `decision` memories attributed to the agent's AMOS id with
  `project:` + `team:` visibility grants. The store went from 0 rows (the bug) to
  live entries from the real project.
- **Notifications.** Telegram channel polling with `notify_errors: 0`; rework and
  blocked messages carry the failing output, and a blocked card offers a retry
  button inline.
- **Maintenance card.** All 11 components report real installed versions; four
  honestly report `unknown` because their installers expose no version query.
- **Audit search.** Free text across actor/action/target/detail, category facets
  drawn from the table, inclusive date range.
- **Project deletion.** Refuses with the reason when work is in flight or when
  runs spent money; `force` records the written-off amount in the audit row.
- **Restart recovery.** A card whose driver died with the process is re-driven at
  startup. Found the hard way: a deploy restart left a live CatsWalker card at
  `in_progress` for half an hour with nothing behind it, and no button in the
  product would touch it (the project runner only resumes projects with
  undispatched plan tasks; `retry` only accepts blocked jobs).
- **Large tool output.** asyncio's 64 KiB per-line limit was killing runs with
  `Separator is found, but chunk is longer than limit` — one big file read or test
  log and a stage that had worked for minutes was gone. All five CLI executors now
  read with a 32 MiB limit and survive an over-limit line.

## Open items

- **Telegram bot token rotation.** A token was pasted in plaintext during an
  early session. It works, and it should still be rotated in BotFather.
- **`~/.bastet/secrets` accumulates** rotated credential files; nothing prunes
  them yet.
- **CatsWalker's remaining PM tasks** are mid-flight; one card sits at
  `in_progress`.
- **`bastet doctor` on a fresh shell** can report `claude` as missing when the
  installer has not been re-sourced onto PATH. Harmless, but it reads as a
  failure.

## Ground rules this project holds itself to

These are not aspirations; they are enforced by tests and reviewed on every
change:

1. **Never claim more than was verified.** A component whose version cannot be
   determined says `unknown`. An installer that changed nothing says `unchanged`.
   A gate that could not run is a configuration problem, not a failing test.
2. **Accounting cannot be quietly reduced.** Deleting a job with usage rows is
   refused; deleting a whole project states the amount being written off and
   records it.
3. **A memory write can never break a run** — enforced at the module boundary,
   not per call site.
4. **The agent must not be able to pass a gate by weakening it.** The rework
   brief names every shortcut explicitly.
5. **Side effects ask a human.** Deploy, release and merge stages stay
   `human-approve`; the runner never approves anything itself.
