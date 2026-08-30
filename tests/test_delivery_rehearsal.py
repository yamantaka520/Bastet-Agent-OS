"""Full temporary Git DAG-to-remote delivery acceptance receipt."""

from bastet_agent_os.delivery_rehearsal import run


async def test_parallel_dag_join_and_remote_integration_rehearsal(tmp_path):
    report = await run(tmp_path / "delivery")

    assert report["ok"] is True
    assert report["parallel_roots"]["max_active"] >= 2
    assert len(report["parallel_roots"]["stage_branches"]) == 2
    assert report["remote"]["concurrent_change_preserved"] is True
    assert report["remote"]["job_branch"] != report["remote"]["target_main"]
    assert report["delivery"]["status"] == "succeeded"
    assert report["delivery"]["commit_sha"] == report["remote"]["target_main"]
    assert report["delivery"]["receipt_matches"] is True
