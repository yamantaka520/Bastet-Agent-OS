"""Database ownership for supervisor work that can spend Agent time."""

from bastet_agent_os import execution_leases
from bastet_agent_os.db import Db


def test_lease_has_one_owner_and_an_expired_owner_can_be_replaced(seeded):
    other = Db(seeded.path)
    try:
        assert execution_leases.acquire(
            seeded, kind="pm-diagnosis", target_id="job1", owner_id="server-a", ttl_s=60)
        assert not execution_leases.acquire(
            other, kind="pm-diagnosis", target_id="job1", owner_id="server-b", ttl_s=60)
        assert execution_leases.renew(
            seeded, kind="pm-diagnosis", target_id="job1", owner_id="server-a", ttl_s=60)
        assert not execution_leases.release(
            other, kind="pm-diagnosis", target_id="job1", owner_id="server-b")

        seeded.write("UPDATE execution_leases SET expires_at='2000-01-01T00:00:00+00:00'")
        assert execution_leases.acquire(
            other, kind="pm-diagnosis", target_id="job1", owner_id="server-b", ttl_s=60)
        assert other.one("SELECT owner_id FROM execution_leases")["owner_id"] == "server-b"
    finally:
        other.close()


def test_release_is_scoped_to_the_current_owner(seeded):
    assert execution_leases.acquire(
        seeded, kind="pm-diagnosis", target_id="job1", owner_id="server-a", ttl_s=60)
    assert execution_leases.release(
        seeded, kind="pm-diagnosis", target_id="job1", owner_id="server-a")
    assert seeded.one("SELECT * FROM execution_leases") is None


async def test_stale_pm_owner_cannot_apply_a_diagnosis(orch, seeded):
    from bastet_agent_os import pm_supervisor

    assert execution_leases.acquire(
        seeded, kind="pm-diagnosis", target_id="job1", owner_id="server-a", ttl_s=60)
    seeded.write("UPDATE execution_leases SET expires_at='2000-01-01T00:00:00+00:00'")
    assert execution_leases.acquire(
        seeded, kind="pm-diagnosis", target_id="job1", owner_id="server-b", ttl_s=60)

    outcome = await pm_supervisor.diagnose(
        orch, seeded.one("SELECT * FROM jobs WHERE id='job1'"), lease_owner="server-a")

    assert outcome == {"action": "skipped", "reason": "PM diagnosis lease lost"}
    assert not seeded.query("SELECT * FROM audit_log WHERE action='job.pm_diagnosis_started'")
