import pytest
from fake_executor import SCRIPT  # registers the "fake" executor builtin

from bastet_agent_os.config import Home
from bastet_agent_os.db import Db, now
from bastet_agent_os.orchestrator import Orchestrator
from bastet_agent_os.pricing import PriceBook


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "test.db")
    yield d
    d.close()


@pytest.fixture
def repo(tmp_path):
    """A real git repo — dispatch refuses a path that is not one, because
    running an agent in a non-repo is how a first dispatch fails confusingly."""
    import subprocess
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("# test repo\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c",
                    "user.name=t", "commit", "-qm", "init"], check=True)
    return path


@pytest.fixture
def seeded(db, repo):
    """A project + agent + llm resource + grant + job + active run."""
    ts = now()
    db.write_many([
        ("INSERT INTO projects(id, team_id, repo_path, created_at, updated_at) "
         "VALUES('proj1','team1',?,?,?)", (str(repo), ts, ts)),
        ("INSERT INTO agents(id, amos_agent_id, name, executor_type, created_at, updated_at) "
         "VALUES('ag1','ag1','Agent One','claude-code',?,?)", (ts, ts)),
        ("INSERT INTO resources(id, kind, name, endpoint, api_flavor, secret_ref, "
         "created_at, updated_at) "
         "VALUES('res1','llm','anthropic-main','https://upstream.example','anthropic',"
         "'env:TEST_UPSTREAM_KEY',?,?)", (ts, ts)),
        ("INSERT INTO grants(id, resource_id, scope_type, scope_id, budget_usd, "
         "max_concurrency, created_at) VALUES('grt1','res1','project','proj1',10.0,2,?)",
         (ts,)),
        ("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, stage, status, "
         "created_at, updated_at) VALUES('job1','proj1','[]','t','work','in_progress',?,?)",
         (ts, ts)),
        ("INSERT INTO runs(id, job_id, stage, agent_id, executor_type, resource_id, status) "
         "VALUES('run1','job1','work','ag1','claude-code','res1','running')", ()),
    ])
    return db


@pytest.fixture
def orch(seeded, tmp_path):
    """Orchestrator wired to the seeded DB + the scripted fake executor."""
    SCRIPT.clear()
    home = Home(tmp_path / "home")
    home.ensure()
    seeded.write("INSERT INTO agents(id, amos_agent_id, name, executor_type, created_at, "
                 "updated_at) VALUES('fakebot','fakebot','Fake','fake',datetime('now'),"
                 "datetime('now'))")
    return Orchestrator(seeded, home, PriceBook(), "http://127.0.0.1:0")
