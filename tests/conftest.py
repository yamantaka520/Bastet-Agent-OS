import pytest

from bastet_agent_os.db import Db, now


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "test.db")
    yield d
    d.close()


@pytest.fixture
def seeded(db):
    """A project + agent + llm resource + grant + job + active run."""
    ts = now()
    db.write_many([
        ("INSERT INTO projects(id, team_id, repo_path, created_at, updated_at) "
         "VALUES('proj1','team1','/tmp/repo',?,?)", (ts, ts)),
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
