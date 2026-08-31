"""The branch review API exposes evidence, never implicit merge authority."""

import json
import subprocess

from fastapi.testclient import TestClient

from bastet_agent_os.config import Home
from bastet_agent_os.db import Db, now
from bastet_agent_os.server import create_app


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True)


def test_branch_review_requires_a_successful_branch_receipt(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Bastet", "-c", "user.email=b@test",
         "commit", "-qm", "base")
    _git(repo, "branch", "-M", "main")
    _git(repo, "switch", "-qc", "bastet/review-me")
    (repo / "decision.md").write_text("approved content\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Bastet", "-c", "user.email=b@test",
         "commit", "-qm", "content")
    branch_commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "switch", "-q", "main")

    home = Home(tmp_path / "home")
    app = create_app(home)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"
    db = Db(home.db_path)
    stamp = now()
    try:
        db.write_many([
            ("INSERT INTO projects(id,team_id,repo_path,config_json,created_at,updated_at) "
             "VALUES('p','t',?,'{}',?,?)", (str(repo), stamp, stamp)),
            ("INSERT INTO jobs(id,project_id,stages_snapshot_json,title,stage,status,"
             "delivery_json,delivery_status,created_at,updated_at) VALUES(" 
             "'review-me','p','[]','Review','content','done',?,'succeeded',?,?)",
             (json.dumps({"mode": "branch"}), stamp, stamp)),
            ("INSERT INTO deliveries(id,job_id,mode,status,commit_sha,started_at) "
             "VALUES('d','review-me','branch','succeeded',?,?)",
             (branch_commit, stamp)),
        ])
    finally:
        db.close()

    response = client.get("/api/jobs/review-me/branch-review")
    assert response.status_code == 200
    review = response.json()
    assert review["target_branch"] == "main"
    assert review["comparison_scope"] == "local_target_snapshot"
    assert review["files"] == [{"status": "A", "path": "decision.md"}]
    assert "approved content" in review["patch"]

    db = Db(home.db_path)
    try:
        db.write("UPDATE deliveries SET status='failed' WHERE id='d'")
    finally:
        db.close()
    denied = client.get("/api/jobs/review-me/branch-review")
    assert denied.status_code == 409
    assert "completed branch delivery" in denied.json()["detail"]
