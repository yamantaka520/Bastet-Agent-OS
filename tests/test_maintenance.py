"""Maintenance must be honest about what it does and does not know.

The failure mode worth testing is not "does pip work" — it is a maintenance
screen that says everything is current because it could not find out.
"""

import pytest

from bastet_agent_os import maintenance


@pytest.fixture
def fake_run(monkeypatch):
    """Replace the subprocess layer; each test scripts what the tools answer."""
    calls: list = []
    answers: dict = {}

    def run(command, timeout):
        text = command if isinstance(command, str) else " ".join(command)
        calls.append(text)
        for needle, reply in answers.items():
            if needle in text:
                return reply
        return 1, "not scripted"

    monkeypatch.setattr(maintenance, "_run", run)
    return type("Fake", (), {"calls": calls, "answers": answers})()


def test_pip_component_compares_installed_against_index(fake_run):
    fake_run.answers["pip show pytest"] = (0, "Name: pytest\nVersion: 8.1.0\n")
    fake_run.answers["index versions pytest"] = (0, "pytest (8.3.2)\n  LATEST: 8.3.2\n")

    row = maintenance.check("pytest")

    assert row["installed"] == "8.1.0"
    assert row["available"] == "8.3.2"
    assert row["state"] == "outdated"


def test_matching_versions_are_current(fake_run):
    fake_run.answers["pip show pytest"] = (0, "Version: 8.3.2\n")
    fake_run.answers["index versions pytest"] = (0, "LATEST: 8.3.2\n")

    assert maintenance.check("pytest")["state"] == "current"


def test_no_index_answer_is_unknown_not_current(fake_run):
    """A git-source install has nothing to compare against. Claiming `current`
    there is the lie this whole card exists to avoid."""
    fake_run.answers["pip show bastet-agent-os"] = (0, "Version: 0.17.0\n")
    fake_run.answers["index versions"] = (1, "ERROR: no matching index page")

    row = maintenance.check("bastet-agent-os")

    assert row["installed"] == "0.17.0"
    assert row["available"] is None
    assert row["state"] == "unknown"


def test_uninstalled_pip_component_is_missing(fake_run):
    fake_run.answers["pip show claude-agent-sdk"] = (1, "WARNING: not found")
    fake_run.answers["index versions"] = (0, "LATEST: 1.0.0\n")

    assert maintenance.check("claude-agent-sdk")["state"] == "missing"


def test_absent_cli_is_missing(monkeypatch):
    monkeypatch.setattr(maintenance.shutil, "which", lambda program: None)

    row = maintenance.check("claude")

    assert row["state"] == "missing"
    assert row["installed"] is None
    assert "install.sh" in row["source"]      # the card can offer the fix


def test_cli_version_is_parsed_but_availability_stays_unknown(monkeypatch, fake_run):
    monkeypatch.setattr(maintenance.shutil, "which", lambda program: f"/usr/bin/{program}")
    fake_run.answers["codex --version"] = (0, "codex-cli 0.44.1 (build 9f2)\n")

    row = maintenance.check("codex")

    assert row["installed"] == "0.44.1"
    assert row["state"] == "unknown"          # the installer has no version query


def test_cli_that_will_not_report_a_version_still_counts_as_installed(
        monkeypatch, fake_run):
    monkeypatch.setattr(maintenance.shutil, "which", lambda program: f"/usr/bin/{program}")
    fake_run.answers["grok --version"] = (2, "unknown flag")

    assert maintenance.check("grok")["installed"] == "installed"


def test_unknown_component_is_rejected():
    with pytest.raises(ValueError):
        maintenance.check("definitely-not-a-component")


def test_update_that_moves_the_version_reports_updated_and_audits(db, monkeypatch):
    versions = iter(["8.1.0", "8.3.2"])          # before, then after

    def run(command, timeout):
        text = command if isinstance(command, str) else " ".join(command)
        if "pip show" in text:
            return 0, f"Version: {next(versions)}\n"
        if "install --upgrade" in text:
            return 0, "Successfully installed pytest-8.3.2\n"
        return 0, "LATEST: 8.3.2\n"

    monkeypatch.setattr(maintenance, "_run", run)

    out = maintenance.update(db, "pytest", "user:admin")

    assert out["status"] == "updated"
    assert (out["from"], out["to"]) == ("8.1.0", "8.3.2")
    assert out["restart_required"] is False   # pytest is not Bastet
    actions = [r["action"] for r in db.query(
        "SELECT action FROM audit_log WHERE target_id='pytest' ORDER BY id")]
    assert actions == ["maintenance.update.start", "maintenance.update.updated"]


def test_successful_command_with_no_version_change_is_unchanged(db, fake_run):
    """`pip install --upgrade` exits 0 when there is nothing to do. Reporting
    that as "updated" would have people believe a fix landed that did not."""
    fake_run.answers["pip show pytest"] = (0, "Version: 8.3.2\n")
    fake_run.answers["index versions"] = (0, "LATEST: 8.3.2\n")
    fake_run.answers["install --upgrade"] = (0, "Requirement already satisfied\n")

    out = maintenance.update(db, "pytest", "user:admin")

    assert out["status"] == "unchanged"
    assert out["restart_required"] is False


def test_failed_update_keeps_the_log_and_says_failed(db, fake_run):
    fake_run.answers["pip show pytest"] = (0, "Version: 8.1.0\n")
    fake_run.answers["index versions"] = (0, "LATEST: 8.3.2\n")
    fake_run.answers["install --upgrade"] = (1, "ERROR: No matching distribution")

    out = maintenance.update(db, "pytest", "user:admin")

    assert out["status"] == "failed"
    assert "No matching distribution" in out["log"]
    assert db.one("SELECT 1 AS ok FROM audit_log "
                  "WHERE action='maintenance.update.failed'")["ok"] == 1


def test_updating_bastet_itself_asks_for_a_restart(db, monkeypatch):
    versions = iter(["0.16.0", "0.17.0"])

    def run(command, timeout):
        text = command if isinstance(command, str) else " ".join(command)
        if "pip show" in text:
            return 0, f"Version: {next(versions)}\n"
        if "install --upgrade" in text:
            assert "bastet-agent-os" in text          # the released package
            return 0, "Successfully installed bastet-agent-os-0.17.0\n"
        return 0, "LATEST: 0.17.0\n"

    monkeypatch.setattr(maintenance, "_run", run)

    out = maintenance.update(db, "bastet-agent-os", "user:admin")

    assert out["status"] == "updated"
    assert out["restart_required"] is True


def test_check_all_covers_every_declared_component(fake_run, monkeypatch):
    monkeypatch.setattr(maintenance.shutil, "which", lambda program: None)

    rows = maintenance.check_all()

    assert [r["id"] for r in rows] == [c["id"] for c in maintenance.COMPONENTS]
    assert {"bastet-agent-os", "agent-memory-os", "claude"} <= {r["id"] for r in rows}


def test_amos_web_reports_the_command_when_it_is_not_installed(monkeypatch):
    monkeypatch.setattr(maintenance.shutil, "which", lambda program: None)
    monkeypatch.delenv("AMOS_WEB_URL", raising=False)

    info = maintenance.amos_web({})

    assert info["installed"] is False
    assert "agent-memory-web" in info["command"]
    assert info["url"] == ""              # no dead link offered


def test_standard_tooling_is_tracked():
    """pytest (gates), pillow (media), playwright (browser E2E + previews):
    the tools the shipped workflows assume must be visible to check/update —
    an invisible dependency is the one that rots."""
    ids = [c["id"] for c in maintenance.PIP_COMPONENTS]
    for tool in ("pytest", "pillow", "playwright"):
        assert tool in ids, tool
