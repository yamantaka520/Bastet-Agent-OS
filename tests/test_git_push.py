"""Auto-push: a finished job's branch reaches the project's remote.

The trust boundary: only `bastet/<job_id>` is pushed — the project's own branch
is never touched, because parking work somewhere reviewable is automation and
merging it is a decision.
"""

import json
import subprocess

import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os import git_push
from bastet_agent_os.executors.base import RunResult

pytestmark = pytest.mark.asyncio


@pytest.fixture
def origin(repo, tmp_path):
    """A bare remote wired up as the project repo's origin."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(bare)],
                   check=True)
    return bare


def branch_tips(bare) -> dict:
    out = subprocess.run(["git", "--git-dir", str(bare), "for-each-ref",
                          "--format=%(refname:short)"], capture_output=True,
                         text=True)
    return {line for line in out.stdout.splitlines()}


def fixes(name):
    from pathlib import Path

    def run(task):
        (Path(task.workdir) / name).write_text("done\n")
        return RunResult(status="succeeded", summary="wrote it")
    return run


async def test_a_finished_job_lands_on_the_remote(orch, seeded, repo, origin):
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(fixes("feature.txt"))

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True,
                               title="送到遠端"))
    await orch.wait_idle()

    tips = branch_tips(origin)
    assert f"bastet/{job_id}" in tips              # the work arrived
    assert not any(t in ("master", "main") for t in tips), (
        "the project's own branch must never be pushed")
    detail = seeded.one("SELECT detail_json FROM audit_log WHERE action='job.pushed'")
    assert detail is not None
    assert f"bastet/{job_id}" in detail["detail_json"]


async def test_opting_out_per_project_is_respected(orch, seeded, repo, origin):
    seeded.write("UPDATE projects SET config_json=? WHERE id='proj1'",
                 (json.dumps({"git_auto_push": False}),))
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(fixes("feature.txt"))

    orch.dispatch(req(template_id="dev", use_worktree=True))
    await orch.wait_idle()

    assert branch_tips(origin) == set()
    assert seeded.one("SELECT 1 AS x FROM audit_log WHERE action='job.pushed'") is None


async def test_a_job_that_committed_nothing_pushes_nothing(orch, seeded, repo, origin):
    add_template(seeded, "dev", [{"name": "look", "gate": "auto", "read_only": True}])
    SCRIPT.append(RunResult(status="succeeded", summary="read only"))

    orch.dispatch(req(template_id="dev", use_worktree=True))
    await orch.wait_idle()

    assert branch_tips(origin) == set()


async def test_a_dead_remote_fails_loudly_but_not_fatally(orch, seeded, repo,
                                                          tmp_path):
    """The job is done and its work is safe locally; a push failure is a
    delivery problem and must say so, not un-finish the job."""
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    str(tmp_path / "gone.git")], check=True)
    add_template(seeded, "dev", [{"name": "implement", "gate": "auto"}])
    SCRIPT.append(fixes("feature.txt"))

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True))
    await orch.wait_idle()

    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] \
        == "done"                                   # still done
    failed = seeded.one(
        "SELECT detail_json FROM audit_log WHERE action='job.push_failed'")
    assert failed is not None
    assert json.loads(failed["detail_json"])["detail"]   # git's own words


def test_no_remote_and_no_resource_is_a_recorded_skip(seeded, repo):
    row = {"id": "job1", "project_id": "proj1", "title": "t"}

    out = git_push.push_job_branch(seeded, row)

    assert out is None or out.get("pushed") is False


def test_host_parsing_handles_both_url_shapes():
    assert git_push._host_of("git@gitlab.com:meow/catswalker.git") == "gitlab.com"
    assert git_push._host_of("https://github.com/yamantaka520/x.git") == "github.com"
    assert git_push._host_of("/local/path.git") == ""


async def test_a_job_approved_into_done_also_pushes(orch, seeded, repo, origin):
    """Live gap: the art card finished through approve() — the human gate was
    the last stage — and never pushed, with no audit row of any kind. Both
    completion paths must deliver."""
    add_template(seeded, "dev", [
        {"name": "implement", "gate": "auto"},
        {"name": "ship", "gate": "human-approve"},
    ])
    SCRIPT.append(fixes("feature.txt"))
    SCRIPT.append(RunResult(status="succeeded", summary="ready"))

    job_id = orch.dispatch(req(template_id="dev", use_worktree=True))
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] \
        == "blocked"                                # waiting at the human gate

    orch.approve(job_id, True, comment="looks good", user="manfred")

    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] \
        == "done"
    assert f"bastet/{job_id}" in branch_tips(origin)
    assert seeded.one("SELECT 1 AS x FROM audit_log WHERE action='job.pushed'") \
        is not None
