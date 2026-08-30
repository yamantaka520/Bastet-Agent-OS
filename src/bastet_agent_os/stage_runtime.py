"""Durable workspace primitives for workflow-v2 stage graphs.

The scheduler owns state in ``job_stage_nodes``. Writable parallel nodes never
share a checkout: each branch starts from one explicit commit and is joined into
the job worktree only after its gate passed. These helpers do no implicit retry
or conflict resolution; ambiguity becomes a durable blocked node for a person or
an integration agent to resolve.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .db import now

SCRATCH_RELPATH = "._bastet"


@dataclass(frozen=True)
class StageWorkspace:
    path: str
    branch: str
    base_commit: str


@dataclass(frozen=True)
class JoinResult:
    passed: bool
    head_commit: str
    merged: tuple[str, ...]
    failed_commit: str = ""
    detail: str = ""


def _git(path: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)


def _stage_token(stage: str) -> str:
    readable = re.sub(r"[^a-zA-Z0-9._-]+", "-", stage).strip("-.")[:32] or "stage"
    digest = hashlib.sha256(stage.encode()).hexdigest()[:8]
    return f"{readable}-{digest}"


def create_isolated_workspace(*, repo: str, worktrees_root: str | Path,
                              job_id: str, stage: str,
                              base_commit: str) -> StageWorkspace:
    """Create or recover a stage branch at an explicit immutable base."""
    if not base_commit.strip():
        raise ValueError("isolated stage workspace requires a base commit")
    if _git(repo, "rev-parse", "--verify", "--quiet", f"{base_commit}^{{commit}}").returncode:
        raise ValueError(f"unknown stage base commit {base_commit!r}")
    token = _stage_token(stage)
    # The parent job already owns ``bastet/<job_id>``. Git refs are files, so
    # ``bastet/<job_id>/stage-x`` cannot coexist beneath it as a directory.
    branch = f"bastet/{job_id}-stage-{token}"
    root = Path(worktrees_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{job_id}--{token}"

    listed = _git(repo, "worktree", "list", "--porcelain")
    if listed.returncode == 0:
        for block in listed.stdout.split("\n\n"):
            fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
            if fields.get("branch") == f"refs/heads/{branch}":
                existing = Path(fields.get("worktree", ""))
                if existing.is_dir() and existing.resolve() == path.resolve():
                    head = _git(existing, "rev-parse", "HEAD")
                    return StageWorkspace(str(existing), branch,
                                          head.stdout.strip() if head.returncode == 0
                                          else base_commit)
                raise ValueError(f"stage branch {branch!r} is checked out elsewhere")

    branch_exists = _git(repo, "rev-parse", "--verify", "--quiet",
                         f"refs/heads/{branch}").returncode == 0
    command = ["worktree", "add"]
    if branch_exists:
        command.extend([str(path), branch])
    else:
        command.extend(["-b", branch, str(path), base_commit])
    created = _git(repo, *command)
    if created.returncode:
        raise RuntimeError(created.stderr.strip() or "git worktree add failed")
    return StageWorkspace(str(path), branch, base_commit)


def commit_stage_output(workdir: str, *, job_id: str, stage: str,
                        title: str = "") -> str:
    """Commit one isolated node's output and return its immutable full SHA."""
    status = _git(workdir, "status", "--porcelain", "--", ".",
                  f":(exclude){SCRATCH_RELPATH}")
    tracked_scratch = _git(workdir, "status", "--porcelain", "--untracked-files=no",
                           "--", SCRATCH_RELPATH)
    if status.returncode or tracked_scratch.returncode:
        raise RuntimeError((status.stderr or tracked_scratch.stderr).strip()
                           or "git status failed")
    if status.stdout.strip() or tracked_scratch.stdout.strip():
        added = _git(workdir, "add", "-A", "--", ".",
                     f":(exclude){SCRATCH_RELPATH}")
        if added.returncode:
            raise RuntimeError(added.stderr.strip() or "git add failed")
        if tracked_scratch.stdout.strip():
            added = _git(workdir, "add", "-u", "--", SCRATCH_RELPATH)
            if added.returncode:
                raise RuntimeError(added.stderr.strip() or "git add failed")
        message = f"bastet({stage}): {(title or job_id)[:60]}\n\njob {job_id}\nstage {stage}"
        committed = _git(workdir, "-c", "user.name=Bastet Agent OS", "-c",
                         "user.email=bastet@localhost", "commit", "--no-verify", "-q",
                         "-m", message)
        if committed.returncode:
            raise RuntimeError(committed.stderr.strip() or "git commit failed")
    head = _git(workdir, "rev-parse", "HEAD")
    if head.returncode:
        raise RuntimeError(head.stderr.strip() or "git rev-parse failed")
    return head.stdout.strip()


def join_stage_heads(primary_workdir: str, heads: list[str]) -> JoinResult:
    """Merge passed stage heads into the job branch, aborting cleanly on conflict."""
    clean = _git(primary_workdir, "status", "--porcelain", "--untracked-files=all")
    if clean.returncode or clean.stdout.strip():
        return JoinResult(False, "", (), detail="join worktree is not clean")
    merged: list[str] = []
    for head in dict.fromkeys(heads):
        if not head:
            continue
        valid = _git(primary_workdir, "rev-parse", "--verify", "--quiet", f"{head}^{{commit}}")
        if valid.returncode:
            return JoinResult(False, _head(primary_workdir), tuple(merged), head,
                              f"unknown stage commit {head}")
        contained = _git(primary_workdir, "merge-base", "--is-ancestor", head, "HEAD")
        if contained.returncode == 0:
            merged.append(head)
            continue
        result = _git(primary_workdir, "merge", "--no-ff", "--no-edit", head)
        if result.returncode:
            _git(primary_workdir, "merge", "--abort")
            return JoinResult(False, _head(primary_workdir), tuple(merged), head,
                              (result.stderr or result.stdout).strip()[:2000])
        merged.append(head)
    return JoinResult(True, _head(primary_workdir), tuple(merged))


def _head(workdir: str) -> str:
    result = _git(workdir, "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else ""


def persist_node_workspace(db, job_id: str, stage: str, workspace: StageWorkspace) -> None:
    db.write("UPDATE job_stage_nodes SET worktree_path=?,head_commit=?,updated_at=? "
             "WHERE job_id=? AND stage=?",
             (workspace.path, workspace.base_commit, now(), job_id, stage))


def finish_node(db, job_id: str, stage: str, *, status: str,
                head_commit: str = "") -> None:
    if status not in ("passed", "failed", "blocked"):
        raise ValueError("terminal stage node status must be passed, failed, or blocked")
    db.write("UPDATE job_stage_nodes SET status=?,head_commit=?,finished_at=?,updated_at=? "
             "WHERE job_id=? AND stage=?",
             (status, head_commit, now(), now(), job_id, stage))


def claim_ready_node(db, job_id: str, stage: str) -> bool:
    """Atomically acquire one stage before workspace or executor side effects."""
    stamp = now()
    return bool(db.write(
        "UPDATE job_stage_nodes SET status='running',"
        "started_at=COALESCE(started_at,?),updated_at=? "
        "WHERE job_id=? AND stage=? AND status='ready'",
        (stamp, stamp, job_id, stage)).rowcount)


def recover_orphaned_nodes(db, job_id: str) -> list[str]:
    """Return running nodes without a live run to ready after process loss."""
    rows = db.query(
        "SELECT n.stage FROM job_stage_nodes n WHERE n.job_id=? AND n.status='running' "
        "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.job_id=n.job_id "
        "AND r.stage=n.stage AND r.status IN ('queued','running','waiting_input'))",
        (job_id,))
    stamp = now()
    for row in rows:
        db.write("UPDATE job_stage_nodes SET status='ready',updated_at=? "
                 "WHERE job_id=? AND stage=? AND status='running'",
                 (stamp, job_id, row["stage"]))
    return [row["stage"] for row in rows]


def reset_failed_subgraph(db, job_id: str, stages, root: str) -> list[str]:
    """Retry one failed node while invalidating every downstream node."""
    descendants: set[str] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        for stage in stages:
            if current in stage.needs and stage.name not in descendants:
                descendants.add(stage.name)
                pending.append(stage.name)
    stamp = now()
    db.write("UPDATE job_stage_nodes SET status='ready',finished_at=NULL,updated_at=? "
             "WHERE job_id=? AND stage=?", (stamp, job_id, root))
    for name in descendants:
        db.write("UPDATE job_stage_nodes SET status='pending',head_commit='',"
                 "started_at=NULL,finished_at=NULL,updated_at=? WHERE job_id=? AND stage=?",
                 (stamp, job_id, name))
    return sorted(descendants)


def cleanup_isolated_workspaces(db, *, repo: str, job_id: str) -> list[str]:
    """Remove graph checkouts but retain their committed branches and node heads."""
    removed: list[str] = []
    for row in db.query("SELECT stage,worktree_path FROM job_stage_nodes WHERE job_id=? "
                        "AND worktree_path IS NOT NULL", (job_id,)):
        path = row["worktree_path"]
        result = _git(repo, "worktree", "remove", "--force", path)
        if result.returncode == 0:
            removed.append(path)
            db.write("UPDATE job_stage_nodes SET worktree_path=NULL,updated_at=? "
                     "WHERE job_id=? AND stage=?", (now(), job_id, row["stage"]))
    return removed
