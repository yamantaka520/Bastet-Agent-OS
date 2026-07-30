"""What made the first real dispatch fail, pinned so it cannot come back.

The stored repo path was `~/Github/catswalker`. Nothing expanded it, something
created a literal `~` directory, git could not make a worktree there, the
orchestrator quietly ran the agent in that empty non-repo, and the executor
reported the failure with an empty string. Five separate holes.
"""

import subprocess

import pytest
from fake_executor import SCRIPT, req

from bastet_agent_os.config import check_repo_path, expand_repo_path, is_git_repo
from bastet_agent_os.executors.base import RunResult

# ---- path handling -----------------------------------------------------------------

def test_tilde_and_env_vars_are_expanded(monkeypatch, tmp_path):
    home = str(tmp_path / "home")
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("MYREPOS", str(tmp_path / "src"))
    assert expand_repo_path("~/Github/app") == f"{home}/Github/app"
    assert expand_repo_path("$MYREPOS/app") == f"{tmp_path / 'src'}/app"
    assert expand_repo_path("  /abs/path  ") == "/abs/path"
    assert expand_repo_path(None) == ""
    # the bug: an unexpanded path used verbatim creates a literal "~" directory
    assert "~" not in expand_repo_path("~/Github/app")


def test_relative_paths_are_refused_with_a_usable_message():
    with pytest.raises(ValueError, match="絕對路徑"):
        check_repo_path("Github/app")
    with pytest.raises(ValueError, match="絕對路徑"):
        check_repo_path("./app")
    with pytest.raises(ValueError):
        check_repo_path("")


def test_absoluteness_is_judged_on_this_platform(tmp_path):
    """A Windows path is absolute on Windows and meaningless on POSIX — judging
    it by the *server's* platform is the only correct answer, because that is
    where the path will be used."""
    import os
    if os.name == "nt":
        assert check_repo_path(r"C:\\Users\\you\\project")
    else:
        with pytest.raises(ValueError):
            check_repo_path(r"C:\\Users\\you\\project")
        assert check_repo_path(str(tmp_path)) == str(tmp_path)


def test_is_git_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not is_git_repo(plain)
    subprocess.run(["git", "init", "-q", str(plain)], check=True)
    assert is_git_repo(plain)


# ---- dispatch refuses to run in the wrong place -------------------------------------

async def test_missing_repo_fails_the_run_with_the_path_in_the_message(orch, seeded,
                                                                      tmp_path):
    seeded.write("UPDATE projects SET repo_path=? WHERE id='proj1'",
                 (str(tmp_path / "not-there"),))
    job_id = orch.dispatch(req())
    await orch.wait_idle()
    run = seeded.one("SELECT * FROM runs WHERE job_id=? ORDER BY started_at DESC "
                     "LIMIT 1", (job_id,))
    assert run["status"] == "failed"
    assert "not-there" in (run["error"] or "")        # says which path
    assert "不存在" in (run["error"] or "")


async def test_a_directory_that_is_not_a_git_repo_is_refused(orch, seeded, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    seeded.write("UPDATE projects SET repo_path=? WHERE id='proj1'", (str(plain),))
    job_id = orch.dispatch(req())
    await orch.wait_idle()
    run = seeded.one("SELECT * FROM runs WHERE job_id=? ORDER BY started_at DESC "
                     "LIMIT 1", (job_id,))
    assert run["status"] == "failed" and "git repo" in (run["error"] or "")


async def test_the_agent_runs_inside_the_expanded_repo(orch, seeded, repo, monkeypatch):
    """The whole point: with `~` in the DB the agent must still start in the real
    directory, not in a literal `~/…` that does not exist."""
    monkeypatch.setenv("HOME", str(repo.parent))
    seeded.write("UPDATE projects SET repo_path='~/repo' WHERE id='proj1'")
    captured = {}
    SCRIPT.append(lambda task: (captured.update(workdir=task.workdir)
                                or RunResult(status="succeeded")))
    orch.dispatch(req())
    await orch.wait_idle()
    assert "~" not in captured["workdir"]
    assert str(repo) in captured["workdir"]          # repo itself or its worktree


# ---- a failure always says something ------------------------------------------------

async def test_a_failed_run_never_records_an_empty_error(orch, seeded):
    """The first real failure showed `error: ""` — undebuggable."""
    SCRIPT.append(RunResult(status="failed", summary=""))
    job_id = orch.dispatch(req())
    await orch.wait_idle()
    run = seeded.one("SELECT * FROM runs WHERE job_id=?", (job_id,))
    assert run["status"] == "failed"
    assert (run["error"] or "").strip()
    assert "no output" in run["error"]


# ---- retry -------------------------------------------------------------------------

async def test_retry_reruns_the_stuck_stage(orch, seeded):
    SCRIPT.append(RunResult(status="failed", summary="boom"))
    job_id = orch.dispatch(req())
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] \
        == "blocked"

    SCRIPT.append(RunResult(status="succeeded", summary="fixed"))
    out = orch.retry(job_id, user="manfred")
    assert out["status"] == "in_progress"
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    attempts = seeded.query("SELECT attempt FROM runs WHERE job_id=? ORDER BY attempt",
                            (job_id,))
    assert len(attempts) >= 2                      # a second attempt really ran
    assert seeded.query("SELECT * FROM audit_log WHERE action='job.retry'")


async def test_retry_can_switch_agent_and_refuses_healthy_jobs(orch, seeded):
    seeded.write("INSERT INTO agents(id, amos_agent_id, name, executor_type, "
                 "created_at, updated_at) VALUES('other','other','Other','fake',"
                 "datetime('now'),datetime('now'))")
    SCRIPT.append(RunResult(status="failed", summary="boom"))
    job_id = orch.dispatch(req())
    await orch.wait_idle()

    SCRIPT.append(RunResult(status="succeeded"))
    orch.retry(job_id, agent_id="other")
    await orch.wait_idle()
    assert seeded.one("SELECT default_agent_id FROM jobs WHERE id=?",
                      (job_id,))["default_agent_id"] == "other"

    with pytest.raises(ValueError, match="not stuck"):
        orch.retry(job_id)                          # it is done now
    with pytest.raises(ValueError):
        orch.retry("job_ghost")
