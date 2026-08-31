"""The credentialed store canary is read-only, scoped and evidence-bound."""

import json

import pytest
from typer.testing import CliRunner

from bastet_agent_os.cli import app
from bastet_agent_os.db import Db, now
from bastet_agent_os.store_canary import StoreCanaryError, run


def _profile(package: str = "com.example.canary") -> dict:
    return {
        "provider": "google_play",
        "status_adapter": "official_api",
        "package_name": package,
        "track": "internal",
        "release_goal": "published",
        "predeploy_command": "check-build",
        "deploy_command": "upload-build",
        "target": f"google-play:{package}:internal",
    }


def _submission(package: str = "com.example.canary") -> dict:
    return {
        "provider": "google_play",
        "package_name": package,
        "track": "internal",
        "version_code": "10400",
        "commit_sha": "a" * 40,
        "version": "1.4.0",
        "target": f"google-play:{package}:internal",
    }


def _seed(home, monkeypatch, *, profile=None):
    profile = profile or _profile()
    home.mkdir(parents=True)
    db = Db(home / "bastet.db")
    stamp = now()
    monkeypatch.setenv("CANARY_GOOGLE_JSON", '{"private_key":"never logged"}')
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-resolve")
    db.write_many([
        ("INSERT INTO projects(id,team_id,config_json,created_at,updated_at) "
         "VALUES('mobile','team',?,?,?)", (json.dumps({"delivery_profile": profile}),
                                           stamp, stamp)),
        ("INSERT INTO resources(id,kind,name,secret_ref,config_json,created_at,updated_at) "
         "VALUES('google-secret','secret','Google Play','env:CANARY_GOOGLE_JSON',?,?,?)",
         (json.dumps({"env_name": "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"}), stamp, stamp)),
        ("INSERT INTO resources(id,kind,name,secret_ref,config_json,created_at,updated_at) "
         "VALUES('other-secret','secret','Other','env:UNRELATED_SECRET',?,?,?)",
         (json.dumps({"env_name": "UNRELATED_TOKEN"}), stamp, stamp)),
        ("INSERT INTO grants(id,resource_id,scope_type,scope_id,created_at) "
         "VALUES('google-grant','google-secret','project','mobile',?)", (stamp,)),
        ("INSERT INTO grants(id,resource_id,scope_type,scope_id,created_at) "
         "VALUES('other-grant','other-secret','project','mobile',?)", (stamp,)),
    ])
    return db


def _reader(calls, milestone="submitted", provider_status="inProgress"):
    def read(profile, submission, env):
        calls.append((profile, submission, env))
        return {
            "provider": "google_play",
            "package_name": profile["package_name"],
            "track": profile["track"],
            "commit_sha": submission["commit_sha"],
            "version": submission["version"],
            "target": submission["target"],
            "milestone": milestone,
            "provider_status": provider_status,
        }
    return read


def test_project_submission_canary_resolves_only_required_granted_secret(
        tmp_path, monkeypatch):
    home = tmp_path / "home"
    db = _seed(home, monkeypatch)
    db.close()
    receipt_file = tmp_path / "submission.json"
    receipt_file.write_text(json.dumps(_submission()))
    calls = []

    report = run(home, project_id="mobile", submission_file=receipt_file,
                 status_reader=_reader(calls))

    assert report["ok"] is report["read_only"] is True
    assert report["binding"] == "supplied_submission_receipt"
    assert report["meets_release_goal"] is False
    assert calls[0][2] == {
        "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON": '{"private_key":"never logged"}'}
    inspected = Db(home / "bastet.db")
    try:
        resolves = inspected.query(
            "SELECT target_id,detail_json FROM audit_log WHERE action='secret.resolve'")
        assert [row["target_id"] for row in resolves] == ["google-secret"]
        audit_text = "".join(row["detail_json"] for row in inspected.query(
            "SELECT detail_json FROM audit_log"))
        assert "never logged" not in audit_text
        assert inspected.one(
            "SELECT 1 FROM audit_log WHERE action='store.canary.checked'") is not None
    finally:
        inspected.close()


def test_job_canary_uses_frozen_profile_and_durable_submission(tmp_path, monkeypatch):
    home = tmp_path / "home"
    frozen = _profile("com.example.frozen")
    db = _seed(home, monkeypatch, profile=_profile("com.example.changed"))
    stamp = now()
    submission = _submission("com.example.frozen")
    db.write_many([
        ("INSERT INTO jobs(id,project_id,stages_snapshot_json,title,stage,status,"
         "delivery_json,created_at,updated_at) VALUES('release','mobile','[]','Release',"
         "'store','in_progress',?,?,?)",
         (json.dumps({"mode": "production", "version": "1.4.0", "profile": frozen}),
          stamp, stamp)),
        ("INSERT INTO deliveries(id,job_id,mode,status,target,version,commit_sha,"
         "evidence_json,provider,started_at) VALUES('delivery','release','production',"
         "'waiting_external',?,?,?,?, 'google_play',?)",
         (submission["target"], submission["version"], submission["commit_sha"],
          json.dumps({"submission_receipt": submission}), stamp)),
    ])
    db.close()
    calls = []

    report = run(home, job_id="release", status_reader=_reader(calls, "published", "completed"))

    assert report["binding"] == "frozen_job_delivery"
    assert report["job_id"] == "release"
    assert report["meets_release_goal"] is True
    assert calls[0][0]["package_name"] == "com.example.frozen"


def test_canary_rejects_duplicate_required_secret_before_api_call(tmp_path, monkeypatch):
    home = tmp_path / "home"
    db = _seed(home, monkeypatch)
    stamp = now()
    db.write_many([
        ("INSERT INTO resources(id,kind,name,secret_ref,config_json,created_at,updated_at) "
         "VALUES('duplicate','secret','Duplicate Google','env:CANARY_GOOGLE_JSON',?,?,?)",
         (json.dumps({"env_name": "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"}), stamp, stamp)),
        ("INSERT INTO grants(id,resource_id,scope_type,scope_id,created_at) "
         "VALUES('duplicate-grant','duplicate','team','team',?)", (stamp,)),
    ])
    db.close()
    receipt_file = tmp_path / "submission.json"
    receipt_file.write_text(json.dumps(_submission()))

    with pytest.raises(StoreCanaryError, match="multiple granted secrets"):
        run(home, project_id="mobile", submission_file=receipt_file,
            status_reader=lambda *_: pytest.fail("API must not be called"))


def test_rejected_provider_object_is_a_successful_observation_not_a_release(
        tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed(home, monkeypatch).close()
    receipt_file = tmp_path / "submission.json"
    receipt_file.write_text(json.dumps(_submission()))

    report = run(
        home, project_id="mobile", submission_file=receipt_file,
        status_reader=_reader([], "rejected", "rejected"))

    assert report["ok"] is True
    assert report["meets_release_goal"] is False
    assert report["receipt"]["milestone"] == "rejected"


def test_supplied_receipt_must_match_configured_target(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed(home, monkeypatch).close()
    supplied = _submission()
    supplied["target"] = "google-play:someone-else:internal"
    receipt_file = tmp_path / "submission.json"
    receipt_file.write_text(json.dumps(supplied))

    with pytest.raises(StoreCanaryError, match="target expected"):
        run(home, project_id="mobile", submission_file=receipt_file,
            status_reader=lambda *_: pytest.fail("API must not be called"))


def test_store_canary_is_exposed_by_cli_and_requires_one_receipt_source():
    help_result = CliRunner().invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "store-canary" in help_result.stdout

    result = CliRunner().invoke(app, ["store-canary", "--project", "mobile"])
    assert result.exit_code == 1
    assert "choose exactly one" in result.stderr
