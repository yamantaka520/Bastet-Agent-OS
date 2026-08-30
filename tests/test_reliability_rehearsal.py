"""Acceptance harness uses real spawned processes, not shared Python state."""

from bastet_agent_os.reliability_rehearsal import run


def test_two_process_kill_restart_rehearsal(tmp_path):
    report = run(tmp_path / "rehearsal")

    assert report["ok"] is True
    assert report["dispatch"]["jobs"] == report["dispatch"]["receipts"] == 1
    assert report["stage_claim"]["winners"] == 1
    assert report["kill_restart"]["recovered_nodes"] == 1
    assert report["pm_diagnosis"]["winners"] == 1
    assert report["pm_diagnosis"]["expired_lease_reclaimed"] is True


def test_rehearsal_is_exposed_by_the_cli():
    from typer.testing import CliRunner

    from bastet_agent_os.cli import app

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "reliability-rehearsal" in result.stdout
