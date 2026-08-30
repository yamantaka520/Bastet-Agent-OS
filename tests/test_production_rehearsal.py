"""Local HTTP provider canary for production delivery receipts."""

from bastet_agent_os.production_rehearsal import run


async def test_production_provider_success_and_stale_canary(tmp_path):
    report = await run(tmp_path / "production")

    assert report["ok"] is True
    assert report["success"]["status"] == "done"
    assert report["success"]["delivery_status"] == "succeeded"
    assert report["success"]["commit_sha"] == \
        report["provider"]["live_receipt"]["commit_sha"]
    assert report["stale_canary"] == {
        "job_id": report["stale_canary"]["job_id"],
        "status": "blocked",
        "delivery_status": "failed",
        "blocked_by_receipt": True,
    }
    assert report["git"]["tags"] == ["v1.4.0", "v1.4.1"]
