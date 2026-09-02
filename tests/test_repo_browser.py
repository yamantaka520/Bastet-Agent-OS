"""Repository browsing is commit-bound, text-only and path-safe."""

import base64
import json
import subprocess

from fastapi.testclient import TestClient

from bastet_agent_os.config import Home
from bastet_agent_os.db import Db, now
from bastet_agent_os.server import create_app


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_job_file_browser_reads_only_the_recorded_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('accepted')\n")
    (repo / "image.bin").write_bytes(b"\x00png")
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    (repo / "accepted.png").write_bytes(png)
    (repo / "spoof.png").write_text("<script>alert(1)</script>")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Bastet", "-c", "user.email=b@test",
         "commit", "-qm", "accepted")
    commit = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text("print('uncommitted secret')\n")

    home = Home(tmp_path / "home")
    client = TestClient(create_app(home), base_url="http://127.0.0.1")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"
    db = Db(home.db_path)
    stamp = now()
    db.write_many([
        ("INSERT INTO projects(id,team_id,repo_path,created_at,updated_at) "
         "VALUES('p','t',?,?,?)", (str(repo), stamp, stamp)),
        ("INSERT INTO jobs(id,project_id,stages_snapshot_json,title,stage,status,"
         "created_at,updated_at) VALUES('j','p','[]','J','done','done',?,?)",
         (stamp, stamp)),
        ("INSERT INTO job_stage_nodes(job_id,stage,status,needs_json,workspace,"
         "head_commit,updated_at) VALUES('j','done','passed','[]','shared',?,?)",
         (commit, stamp)),
    ])
    db.close()

    root = client.get("/api/jobs/j/files").json()
    assert [(item["name"], item["kind"]) for item in root["entries"]] == [
        ("accepted.png", "file"), ("image.bin", "file"),
        ("spoof.png", "file"), ("src", "directory")]
    source = client.get("/api/jobs/j/files", params={"path": "src/app.py"}).json()
    assert source["content"] == "print('accepted')\n"
    assert "secret" not in source["content"]
    assert client.get("/api/jobs/j/files", params={"path": "image.bin"}).json()["binary"]
    image_meta = client.get(
        "/api/jobs/j/files", params={"path": "accepted.png"}).json()
    assert image_meta["preview_mime"] == "image/png"
    image = client.get(
        "/api/jobs/j/files/image", params={"path": "accepted.png"})
    assert image.status_code == 200 and image.content == png
    assert image.headers["content-type"] == "image/png"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert client.get(
        "/api/jobs/j/files/image", params={"path": "spoof.png"}).status_code == 400
    assert client.get(
        "/api/jobs/j/files/image", params={"path": "../accepted.png"}).status_code == 400
    assert client.get("/api/jobs/j/files", params={"path": "../.env"}).status_code == 400


def test_job_file_browser_needs_an_evidence_commit(tmp_path):
    home = Home(tmp_path / "home")
    client = TestClient(create_app(home), base_url="http://127.0.0.1")
    client.headers["Authorization"] = f"Bearer {home.api_token()}"
    db = Db(home.db_path)
    stamp = now()
    db.write_many([
        ("INSERT INTO projects(id,team_id,repo_path,created_at,updated_at) "
         "VALUES('p','t',? ,?,?)", (str(tmp_path), stamp, stamp)),
        ("INSERT INTO jobs(id,project_id,stages_snapshot_json,title,stage,status,"
         "created_at,updated_at) VALUES('j','p',?,'J','x','done',?,?)",
         (json.dumps([]), stamp, stamp)),
    ])
    db.close()
    assert client.get("/api/jobs/j/files").status_code == 409
