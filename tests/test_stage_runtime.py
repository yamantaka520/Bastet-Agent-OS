"""Workflow-v2 isolated workspace and join invariants."""

import subprocess
from pathlib import Path

from bastet_agent_os.stage_runtime import (
    claim_ready_node,
    cleanup_isolated_workspaces,
    commit_stage_output,
    create_isolated_workspace,
    join_stage_heads,
    recover_orphaned_nodes,
    reset_failed_subgraph,
)
from bastet_agent_os.workflow import parse_stages, seed_stage_nodes


def _run(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_parallel_stage_workspaces_start_at_same_base_and_join(repo, tmp_path):
    base = _run(repo, "rev-parse", "HEAD")
    ui = create_isolated_workspace(
        repo=str(repo), worktrees_root=tmp_path / "worktrees", job_id="job1",
        stage="ui/ux", base_commit=base)
    core = create_isolated_workspace(
        repo=str(repo), worktrees_root=tmp_path / "worktrees", job_id="job1",
        stage="core", base_commit=base)
    assert ui.path != core.path and ui.branch != core.branch
    assert _run(ui.path, "rev-parse", "HEAD") == base
    assert _run(core.path, "rev-parse", "HEAD") == base

    with open(f"{ui.path}/ui.txt", "w", encoding="utf-8") as handle:
        handle.write("accessible layout\n")
    with open(f"{core.path}/core.txt", "w", encoding="utf-8") as handle:
        handle.write("stable api\n")
    ui_head = commit_stage_output(ui.path, job_id="job1", stage="ui/ux")
    core_head = commit_stage_output(core.path, job_id="job1", stage="core")

    joined = join_stage_heads(str(repo), [ui_head, core_head])
    assert joined.passed is True
    assert (repo / "ui.txt").read_text() == "accessible layout\n"
    assert (repo / "core.txt").read_text() == "stable api\n"
    assert set(joined.merged) == {ui_head, core_head}


def test_join_conflict_is_aborted_without_leaving_merge_state(repo, tmp_path):
    base = _run(repo, "rev-parse", "HEAD")
    left = create_isolated_workspace(
        repo=str(repo), worktrees_root=tmp_path / "worktrees", job_id="job2",
        stage="left", base_commit=base)
    right = create_isolated_workspace(
        repo=str(repo), worktrees_root=tmp_path / "worktrees", job_id="job2",
        stage="right", base_commit=base)
    for workspace, text in ((left, "left\n"), (right, "right\n")):
        with open(f"{workspace.path}/README.md", "w", encoding="utf-8") as handle:
            handle.write(text)
    left_head = commit_stage_output(left.path, job_id="job2", stage="left")
    right_head = commit_stage_output(right.path, job_id="job2", stage="right")

    joined = join_stage_heads(str(repo), [left_head, right_head])
    assert joined.passed is False and joined.failed_commit == right_head
    assert _run(repo, "status", "--porcelain") == ""
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_node_recovery_and_retry_only_invalidate_the_failed_descendants(seeded):
    stages = parse_stages([
        {"name": "plan", "needs": [], "read_only": True},
        {"name": "ui", "needs": ["plan"], "workspace": "isolated"},
        {"name": "core", "needs": ["plan"], "workspace": "isolated"},
        {"name": "join", "needs": ["ui", "core"]},
    ])
    seed_stage_nodes(seeded, "job1", stages)
    seeded.write("UPDATE job_stage_nodes SET status='running' WHERE job_id='job1' "
                 "AND stage='plan'")
    assert recover_orphaned_nodes(seeded, "job1") == ["plan"]
    seeded.write("UPDATE job_stage_nodes SET status='passed' WHERE job_id='job1' "
                 "AND stage IN ('plan','core')")
    seeded.write("UPDATE job_stage_nodes SET status='failed' WHERE job_id='job1' "
                 "AND stage='ui'")
    seeded.write("UPDATE job_stage_nodes SET status='blocked' WHERE job_id='job1' "
                 "AND stage='join'")
    assert reset_failed_subgraph(seeded, "job1", stages, "ui") == ["join"]
    states = {row["stage"]: row["status"] for row in seeded.query(
        "SELECT stage,status FROM job_stage_nodes WHERE job_id='job1'")}
    assert states == {"plan": "passed", "ui": "ready",
                      "core": "passed", "join": "pending"}


def test_ready_node_has_one_database_owner_across_connections(seeded):
    """Two server processes may see ready; only one may cross into side effects."""
    from bastet_agent_os.db import Db

    seeded.write("INSERT INTO job_stage_nodes(job_id,stage,status,needs_json,workspace,"
                 "updated_at) VALUES('job1','claim-me','ready','[]','shared',"
                 "datetime('now'))")
    other = Db(seeded.path)
    try:
        assert claim_ready_node(seeded, "job1", "claim-me") is True
        assert claim_ready_node(other, "job1", "claim-me") is False
    finally:
        other.close()


def test_isolated_workspace_cleanup_keeps_branch_and_commit(seeded, repo, tmp_path):
    workspace = create_isolated_workspace(
        repo=str(repo), worktrees_root=tmp_path / "worktrees", job_id="job1",
        stage="visual", base_commit=_run(repo, "rev-parse", "HEAD"))
    seeded.write("INSERT INTO job_stage_nodes(job_id,stage,status,worktree_path,"
                 "head_commit,updated_at) VALUES('job1','visual','passed',?,?,datetime('now'))",
                 (workspace.path, workspace.base_commit))
    removed = cleanup_isolated_workspaces(seeded, repo=str(repo), job_id="job1")
    assert removed == [workspace.path]
    assert not Path(workspace.path).exists()
    assert _run(repo, "rev-parse", "--verify", workspace.branch)


def test_untracked_engine_scratch_is_not_committed(repo, tmp_path):
    workspace = create_isolated_workspace(
        repo=str(repo), worktrees_root=tmp_path / "worktrees", job_id="job3",
        stage="review", base_commit=_run(repo, "rev-parse", "HEAD"))
    scratch = Path(workspace.path) / "._bastet"
    scratch.mkdir()
    (scratch / "verdict.json").write_text('{"verdict":"approve"}')
    (Path(workspace.path) / "result.txt").write_text("real output\n")
    head = commit_stage_output(workspace.path, job_id="job3", stage="review")
    tracked = _run(workspace.path, "ls-tree", "-r", "--name-only", head).splitlines()
    assert "result.txt" in tracked
    assert "._bastet/verdict.json" not in tracked
