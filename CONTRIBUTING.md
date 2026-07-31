# Contributing

## Ground rules

This project holds itself to a small number of rules that are more important than
any feature. They are enforced by tests, and a change that breaks one of them will
not be accepted even if it works:

1. **Never claim more than was verified.** A version that cannot be determined is
   `unknown`, not "current". An installer that changed nothing says `unchanged`.
   A gate command that could not run is a configuration problem, not a failing
   test.
2. **Accounting cannot be quietly reduced.** Anything that deletes usage rows must
   either refuse or state the amount being written off, and record it.
3. **A memory write can never break a run.**
4. **An agent must not be able to pass a gate by weakening the gate.**
5. **Side effects ask a human.** Deploy, release and merge stages stay
   `human-approve`.

## Setup

```bash
git clone git@github.com:yamantaka520/Bastet-Agent-OS.git
cd Bastet-Agent-OS
pip install -e '.[dev]'
pytest -q                 # 328 tests
ruff check .
```

The web UI:

```bash
cd web
npm install
npm run build             # output is committed to src/bastet_agent_os/ui_dist
```

The build output is committed on purpose, so installing from git serves the UI
without Node on the target host. Rebuild it in the same commit as any UI change.

## Tests

- Every user-visible change needs a test that **fails without the fix**. If you
  cannot make it fail, you have not found the bug yet.
- Integration with Agent Memory OS is tested against **real AMOS**, never a stub:
  the last two memory bugs were API mismatches that a stub would have hidden.
- Name a test after the behaviour, not the function: `test_a_failed_gate_goes_back`
  beats `test_rework_1`.
- The docstring should say why the test exists — ideally the real incident it
  came from.

## Versioning

`src/bastet_agent_os/__init__.py` holds the single `__version__`. Bump it, add a
`CHANGELOG.md` entry, and keep `web/package.json` in step —
`tests/test_version.py` fails the build if they drift.

Write changelog entries for the person who will hit the problem: what broke, what
it looked like, and what changed. "Fixed a bug" helps nobody.

## Internationalisation

Add UI strings to `web/src/i18n/zh-Hant.ts` (the canonical dictionary) and
translate in the other four. They are typed against it, so a missing key fails
`npm run build`. `tests/test_i18n.py` fails on hard-coded CJK in components —
including in comments, so write comments in English.

## Code style

- `ruff check .` must pass.
- Comments explain *why*, and are worth writing when the reason is not obvious
  from the code. A comment that restates the line is noise; one that records the
  incident behind an odd-looking guard is worth keeping.
- Match the surrounding code's idiom rather than importing your own.

## Reporting a problem

Open an issue with: what you expected, what happened, the relevant slice of
`bastet audit`, and `journalctl --user -u bastet` (or the service log) around the
event. Redact tokens — Bastet masks them in its own output but logs you paste are
your responsibility.

Security issues: see [SECURITY.md](SECURITY.md); do not open a public issue.
