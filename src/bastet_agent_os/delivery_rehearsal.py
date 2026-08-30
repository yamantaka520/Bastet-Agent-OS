"""Isolated end-to-end DAG join and remote integration rehearsal."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Home
from .db import Db, now
from .executors.base import RunResult, TaskSpec, register_builtin

_REMOTE_PATH = ""
_REMOTE_ADVANCED = False


def _git(path: str | Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                            text=True, check=True)
    return result.stdout.strip()


def _advance_remote() -> None:
    global _REMOTE_ADVANCED
    if _REMOTE_ADVANCED:
        return
    remote = Path(_REMOTE_PATH)
    clone = remote.parent / "remote-writer"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(clone)],
                   check=True)
    (clone / "remote-only.txt").write_text("concurrent remote change\n")
    _git(clone, "add", "remote-only.txt")
    _git(clone, "-c", "user.name=Remote Writer",
         "-c", "user.email=remote@localhost", "commit", "-qm", "advance remote main")
    _git(clone, "push", "-q", "origin", "HEAD:main")
    _REMOTE_ADVANCED = True


@dataclass
class _Handle:
    task: TaskSpec

    def state(self) -> dict[str, str]:
        return {"kind": "delivery-rehearsal", "workdir": self.task.workdir}


@register_builtin
class _DeliveryRehearsalExecutor:
    kind = "delivery-rehearsal"
    capabilities = {"code", "review"}
    active = 0
    max_active = 0

    async def start(self, task: TaskSpec) -> _Handle:
        type(self).active += 1
        type(self).max_active = max(type(self).max_active, type(self).active)
        if task.read_only:
            return _Handle(task)
        workdir = Path(task.workdir)
        marker = workdir.name
        if "--ui-" in marker:
            (workdir / "ui.txt").write_text("ui branch\n")
        elif "--core-" in marker:
            (workdir / "core.txt").write_text("core branch\n")
        else:
            if not (workdir / "ui.txt").is_file() or not (workdir / "core.txt").is_file():
                raise RuntimeError("terminal join did not receive both dependency branches")
            (workdir / "joined.txt").write_text("terminal join\n")
            _advance_remote()
        return _Handle(task)

    async def stream(self, handle: _Handle):
        # Keep roots overlapped long enough to prove the graph really scheduled
        # both branches concurrently rather than merely creating two branches.
        await asyncio.sleep(0.05)
        if False:
            yield None

    async def respond(self, handle, request_id, reply) -> None:
        return None

    async def cancel(self, handle) -> None:
        return None

    async def result(self, handle: _Handle) -> RunResult:
        type(self).active -= 1
        if handle.task.read_only:
            return RunResult(
                status="succeeded",
                summary=json.dumps({
                    "verdict": "accept",
                    "response": "dependency handoffs contain committed branch evidence",
                }))
        return RunResult(status="succeeded", summary="delivery rehearsal stage passed")


def _seed_repo(root: Path) -> tuple[Path, Path, str]:
    remote = root / "origin.git"
    repo = root / "repo"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("# delivery rehearsal\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Bastet Rehearsal",
         "-c", "user.email=rehearsal@localhost", "commit", "-qm", "seed")
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo, remote, _git(repo, "rev-parse", "HEAD")


async def run(root: str | Path) -> dict[str, Any]:
    """Execute the real graph scheduler, Git join and integration delivery."""
    global _REMOTE_PATH, _REMOTE_ADVANCED
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    repo, remote, initial_main = _seed_repo(root)
    _REMOTE_PATH = str(remote)
    _REMOTE_ADVANCED = False
    _DeliveryRehearsalExecutor.active = 0
    _DeliveryRehearsalExecutor.max_active = 0

    home = Home(root / "home")
    home.ensure()
    db = Db(home.db_path)
    stamp = now()
    profile = {
        "target_branch": "main",
        "target": "origin/main",
        "predeploy_command": (
            "test -f ui.txt && test -f core.txt && test -f joined.txt "
            "&& test -f remote-only.txt"),
    }
    db.write_many([
        ("INSERT INTO projects(id,team_id,repo_path,status,config_json,created_at,updated_at) "
         "VALUES('delivery','team',?,'running',?,?,?)",
         (str(repo), json.dumps({"stage_max_parallel": 2,
                                 "delivery_profile": profile}), stamp, stamp)),
        ("INSERT INTO agents(id,amos_agent_id,name,executor_type,config_json,created_at,"
         "updated_at) VALUES('worker','worker','Worker','delivery-rehearsal',?,?,?)",
         (json.dumps({"max_concurrency": 2}), stamp, stamp)),
    ])
    stages = [
        {"name": "ui", "needs": [], "workspace": "isolated", "gate": "auto"},
        {"name": "core", "needs": [], "workspace": "isolated", "gate": "auto"},
        {"name": "join", "needs": ["ui", "core"], "workspace": "shared",
         "gate": "tests-pass",
         "gate_config": {"command": "test -f ui.txt && test -f core.txt "
                                    "&& test -f joined.txt"},
         "delivery_modes": ["integration", "production"]},
    ]
    db.write("INSERT INTO workflow_templates(id,name,version,stages_json) "
             "VALUES('delivery-dag','Delivery DAG',1,?)", (json.dumps(stages),))

    from .orchestrator import DispatchRequest, Orchestrator
    from .pricing import PriceBook
    orch = Orchestrator(db, home, PriceBook(), "http://127.0.0.1:0")
    job_id = orch.dispatch(DispatchRequest(
        project_id="delivery", prompt="join two branches and deliver safely",
        title="delivery rehearsal", agent_id="worker", template_id="delivery-dag",
        use_worktree=True, delivery={"mode": "integration"}), actor="rehearsal")
    await orch.wait_idle()

    job = db.one("SELECT status,delivery_status FROM jobs WHERE id=?", (job_id,))
    nodes = [dict(row) for row in db.query(
        "SELECT stage,status,head_commit FROM job_stage_nodes WHERE job_id=? "
        "ORDER BY rowid", (job_id,))]
    receipt = db.one("SELECT * FROM deliveries WHERE job_id=? ORDER BY started_at DESC",
                     (job_id,))
    if job is None or dict(job) != {"status": "done", "delivery_status": "succeeded"}:
        raise RuntimeError(f"job did not finish through delivery: {dict(job) if job else None}")
    if len(nodes) != 3 or any(node["status"] != "passed" for node in nodes):
        raise RuntimeError(f"stage graph did not converge: {nodes}")
    if _DeliveryRehearsalExecutor.max_active < 2:
        raise RuntimeError("UI and Core roots did not overlap")
    evidence = json.loads(receipt["evidence_json"] or "{}") if receipt else {}
    integration = evidence.get("integration") or {}
    remote_main = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True).stdout.strip()
    remote_job = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/bastet/{job_id}"],
        capture_output=True, text=True, check=True).stdout.strip()
    tree = subprocess.run(
        ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", remote_main],
        capture_output=True, text=True, check=True).stdout.splitlines()
    required = {"ui.txt", "core.txt", "joined.txt", "remote-only.txt"}
    if not required.issubset(tree):
        raise RuntimeError(f"remote main lost branch or concurrent remote work: {tree}")
    if not receipt or receipt["commit_sha"] != remote_main \
            or integration.get("remote_commit_sha") != remote_main:
        raise RuntimeError("delivery receipt does not match remote main")
    if remote_job == remote_main:
        raise RuntimeError("rehearsal did not create a fresh integration commit")
    branch_prefix = f"bastet/{job_id}-stage-"
    stage_branches = [
        branch
        for branch in _git(
            repo, "for-each-ref", "--format=%(refname:short)", "refs/heads"
        ).splitlines()
        if branch.startswith(branch_prefix)
    ]
    if len(stage_branches) != 2:
        raise RuntimeError(f"isolated stage branch receipts missing: {stage_branches}")
    db.close()

    return {
        "ok": True,
        "job_id": job_id,
        "parallel_roots": {"max_active": _DeliveryRehearsalExecutor.max_active,
                           "stage_branches": stage_branches},
        "join": {"nodes": nodes, "required_files": sorted(required)},
        "remote": {"initial_main": initial_main, "job_branch": remote_job,
                   "target_main": remote_main, "concurrent_change_preserved": True},
        "delivery": {"status": receipt["status"], "target": receipt["target"],
                     "commit_sha": receipt["commit_sha"], "receipt_matches": True},
    }


def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="bastet-delivery-") as directory:
        print(json.dumps(asyncio.run(run(directory)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
