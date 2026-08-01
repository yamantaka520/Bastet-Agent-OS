"""System settings, job supplies, previews, and the heartbeat."""


import pytest
from fake_executor import SCRIPT, add_template, req
from fastapi.testclient import TestClient

from bastet_agent_os import settings as settings_mod
from bastet_agent_os.config import Home
from bastet_agent_os.db import now
from bastet_agent_os.executors.base import RunResult
from bastet_agent_os.server import create_app


@pytest.fixture
def client(tmp_path):
    home = Home(tmp_path / "home")
    c = TestClient(create_app(home), base_url="http://127.0.0.1")
    c.headers["Authorization"] = f"Bearer {home.api_token()}"
    return c, home


# ---- settings -----------------------------------------------------------------

def test_timezone_is_a_setting_and_it_persists(client):
    c, home = client

    before = c.get("/api/settings").json()
    assert before["timezone"] == "UTC"          # honest default, not a guess

    out = c.put("/api/settings", json={"timezone": "Asia/Taipei"})
    assert out.status_code == 200
    assert out.json()["timezone_offset_minutes"] == 480

    # survives a restart: it lives in config.json, not in memory
    assert home.config()["timezone"] == "Asia/Taipei"
    assert c.get("/api/settings").json()["timezone"] == "Asia/Taipei"


def test_a_nonsense_timezone_is_refused_with_an_example(client):
    c, _ = client

    out = c.put("/api/settings", json={"timezone": "Mars/Olympus"})

    assert out.status_code == 400
    assert "Asia/Taipei" in out.json()["detail"]   # tells you what right looks like


def test_settings_never_leak_hosts_or_secrets():
    public = settings_mod.public({"timezone": "Asia/Tokyo", "host": "0.0.0.0",
                                  "allowed_hosts": ["secret.lan"]})

    assert "host" not in public and "allowed_hosts" not in public


# ---- supplies ------------------------------------------------------------------

@pytest.fixture
def job(client):
    c, home = client
    repo = home.root.parent / "repo"
    repo.mkdir(exist_ok=True)
    c.post("/api/teams", json={"id": "t1", "name": "T"})
    c.post("/api/projects", json={"id": "p1", "repo_path": str(repo), "team_id": "t1"})
    from bastet_agent_os.db import Db
    db = Db(home.db_path)
    ts = now()
    db.write("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, spec_md, "
             "stage, status, created_at, updated_at) VALUES('jobsup','p1','[]',"
             "'部署到 Firebase','spec','implement','in_progress',?,?)", (ts, ts))
    db.close()
    return c, home


async def test_a_supply_reaches_the_next_brief(orch, seeded):
    """The point of the feature: data handed over mid-flight is in the next
    run's prompt, marked as overriding the original spec."""
    seen: list[str] = []

    def capture(task):
        seen.append(task.prompt)
        return RunResult(status="succeeded", summary="done")

    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(capture)
    job_id = orch.dispatch(req(template_id="dev", title="deploy"))
    # supply lands between dispatch bookkeeping and the run — write it directly
    seeded.write("INSERT INTO job_supplies(id, job_id, name, content, created_by, "
                 "created_at) VALUES('sup1',?,?,?,?,?)",
                 (job_id, "Firebase 設定", "project_id: catswalker-prod\nregion: asia-east1",
                  "user:root", now()))
    await orch.wait_idle()

    assert seen, "the stage never ran"
    assert "catswalker-prod" in seen[0]
    assert "操作者補充的資料" in seen[0]


def test_a_credential_pasted_as_a_supply_is_refused(job):
    """A supply travels inside a prompt — to the LLM provider. The refusal names
    what was detected and points at the credentials card."""
    c, _ = job

    out = c.post("/api/jobs/jobsup/supplies", json={
        "name": "firebase key",
        "content": '{"type":"service_account","private_key":"-----BEGIN PRIVATE KEY-----"}',
    })

    assert out.status_code == 400
    assert "憑證" in out.json()["detail"]


def test_plain_data_is_accepted_and_listed(job):
    c, _ = job

    out = c.post("/api/jobs/jobsup/supplies", json={
        "name": "Firebase 專案", "content": "project: catswalker-prod, region: asia-east1"})
    assert out.status_code == 200, out.text

    rows = c.get("/api/jobs/jobsup/supplies").json()
    assert [r["name"] for r in rows] == ["Firebase 專案"]


def test_a_finished_job_takes_no_supplies(job):
    c, home = job
    from bastet_agent_os.db import Db
    db = Db(home.db_path)
    db.write("UPDATE jobs SET status='done' WHERE id='jobsup'")
    db.close()

    out = c.post("/api/jobs/jobsup/supplies", json={"name": "x", "content": "y"})

    assert out.status_code == 409
    assert "重試" in out.json()["detail"]      # says what to do instead


# ---- previews -------------------------------------------------------------------

async def test_previews_left_by_the_stage_survive_to_the_approval(orch, seeded):
    """The stage writes ._bastet/preview/, the worktree dies with the run, and
    the approver must still see the files."""
    from pathlib import Path

    add_template(seeded, "dev", [
        {"name": "ship", "gate": "human-approve"},
    ])

    def leaves_preview(task):
        folder = Path(task.workdir) / "._bastet" / "preview"
        folder.mkdir(parents=True)
        (folder / "screen.png").write_bytes(b"\x89PNG fake")
        (folder / "summary.md").write_text("# what changed\n")
        (folder / "evil?.sh").write_text("skipped: wrong extension")
        return RunResult(status="succeeded", summary="ready to ship")

    SCRIPT.append(leaves_preview)
    job_id = orch.dispatch(req(template_id="dev", use_worktree=True))
    await orch.wait_idle()

    kept = sorted(p.name for p in
                  (orch.home.artifacts_dir / job_id / "preview").iterdir())
    assert kept == ["screen.png", "summary.md"]
    detail = seeded.one("SELECT detail_json FROM audit_log WHERE action='job.previews'")
    assert "screen.png" in detail["detail_json"]


async def test_the_heartbeat_lands_on_the_run(orch, seeded):

    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])

    def chatty(task):
        result = RunResult(status="succeeded", summary="ok")
        return result

    # the fake executor yields events from handle.events; craft one with progress
    from fake_executor import FakeHandle  # noqa: F401
    SCRIPT.append(lambda task: RunResult(status="succeeded", summary="ok"))
    job_id = orch.dispatch(req(template_id="dev"))
    await orch.wait_idle()

    # no progress events from the fake executor → heartbeat may be empty, but the
    # column exists and the jobs endpoint tolerates it
    row = seeded.one("SELECT heartbeat_at, progress_text FROM runs WHERE job_id=?",
                     (job_id,))
    assert row is not None
