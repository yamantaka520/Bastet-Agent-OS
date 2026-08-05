"""Quota failures are timers, not errors.

The live case: `You've hit your session limit · resets 1:30am (Asia/Taipei)`
blocked a card for hours; retries during the window died in seconds each, and a
human had to wait for the vendor's clock. The orchestrator now reads the clock.
"""

from datetime import UTC, datetime

import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os import quota_wait
from bastet_agent_os.executors.base import RunResult

NOON = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def test_the_live_message_parses_to_the_stated_taipei_time():
    out = quota_wait.parse_reset(
        "You've hit your session limit · resets 1:30am (Asia/Taipei)", now=NOON)

    # 1:30am Taipei = 17:30 UTC the same day (noon UTC = 8pm Taipei, so next
    # 1:30am Taipei is tomorrow their time) + 3min margin
    assert out == "2026-08-04T17:33:00+00:00"


def test_pm_times_and_bare_hours():
    assert quota_wait.parse_reset("rate limit — resets 3pm (UTC)", now=NOON) \
        == "2026-08-04T15:03:00+00:00"
    # a stated time that already passed today means tomorrow
    assert quota_wait.parse_reset("usage limit resets 11:00 (UTC)", now=NOON) \
        == "2026-08-05T11:03:00+00:00"


def test_a_quota_failure_with_no_time_gets_the_default_backoff():
    out = quota_wait.parse_reset("429 too many requests", now=NOON)

    assert out == "2026-08-04T12:30:00+00:00"


def test_an_ordinary_failure_is_not_a_timer():
    assert quota_wait.parse_reset("SyntaxError: invalid syntax", now=NOON) is None
    assert quota_wait.parse_reset("", now=NOON) is None


def test_an_unknown_timezone_or_nonsense_time_stays_safe():
    # unknown zone falls back to UTC; nonsense hour falls back to backoff
    out = quota_wait.parse_reset("limit reached, resets 1:30am (Mars/Olympus)",
                                 now=NOON)
    assert out == "2026-08-05T01:33:00+00:00"    # 1:30am UTC tomorrow + margin
    assert quota_wait.parse_reset("quota exceeded resets 99:99", now=NOON) \
        == "2026-08-04T12:30:00+00:00"


@pytest.mark.asyncio
async def test_a_quota_blocked_job_parks_with_a_timer(orch, seeded):
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(RunResult(
        status="failed",
        summary="You've hit your session limit · resets 1:30am (Asia/Taipei)"))

    job_id = orch.dispatch(req(template_id="dev", title="美術整合"))
    await orch.wait_idle()

    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "blocked"
    assert job["resume_at"] is not None            # parked, not dead
    waited = seeded.one("SELECT detail_json FROM audit_log "
                        "WHERE action='job.quota_wait'")
    assert waited is not None
    assert "resume_at" in waited["detail_json"]


@pytest.mark.asyncio
async def test_the_loop_resumes_a_due_job(orch, seeded):
    """The whole point: nobody presses anything."""
    import asyncio

    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(RunResult(status="failed",
                            summary="rate limit exceeded, please retry"))
    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()
    assert seeded.one("SELECT resume_at FROM jobs WHERE id=?",
                      (job_id,))["resume_at"] is not None

    # the window passes; the sweep finds it due
    seeded.write("UPDATE jobs SET resume_at='2020-01-01T00:00:00+00:00' WHERE id=?",
                 (job_id,))
    SCRIPT.append(RunResult(status="succeeded", summary="quota back, done"))
    sweep = asyncio.get_event_loop().create_task(orch.quota_resume_loop())
    try:
        for _ in range(80):
            await asyncio.sleep(0.05)
            row = seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))
            if row["status"] == "done":
                break
        await orch.wait_idle()
    finally:
        sweep.cancel()

    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "done"
    assert job["resume_at"] is None                # retry cleared the timer
    resumed = seeded.one("SELECT 1 AS x FROM audit_log WHERE action='job.retry' "
                         "AND actor='user:server:quota-reset'")
    assert resumed is not None


@pytest.mark.asyncio
async def test_a_manual_retry_beats_the_clock_and_clears_it(orch, seeded):
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(RunResult(status="failed", summary="usage limit reached"))
    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    SCRIPT.append(RunResult(status="succeeded", summary="fine now"))
    orch.retry(job_id, user="manfred")
    await orch.wait_idle()

    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert (job["status"], job["resume_at"]) == ("done", None)
