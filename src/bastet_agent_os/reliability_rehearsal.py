"""Destructive-to-temporary-data multiprocess rehearsal for engine ownership.

This is deliberately separate from ordinary unit mocks.  Every contender is a
fresh OS process with its own SQLite connection and Orchestrator instance.  The
rehearsal never opens the operator's Bastet home or a real project repository.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import subprocess
from pathlib import Path
from queue import Empty
from typing import Any

from .config import Home
from .db import Db, now


def _discard_driver(coro) -> None:
    coro.close()


def _dispatch_worker(db_path: str, home_root: str, plan_key: str, barrier, queue) -> None:
    from .orchestrator import DispatchRequest, Orchestrator
    from .pricing import PriceBook

    db = Db(db_path)
    orch = Orchestrator(db, Home(Path(home_root)), PriceBook(), "http://127.0.0.1:0")
    orch._spawn = _discard_driver
    try:
        barrier.wait(timeout=10)
        job_id = orch.dispatch(DispatchRequest(
            project_id="rehearsal", prompt="prove one dispatch", title="race task",
            agent_id="worker", origin="runner", use_worktree=False,
            plan_key=plan_key, task_id="race-task"), actor="rehearsal")
        queue.put({"ok": True, "job_id": job_id})
    except Exception as exc:
        queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        db.close()


def _stage_worker(db_path: str, job_id: str, wait_after_claim: bool,
                  barrier, queue) -> None:
    from .stage_runtime import claim_ready_node

    db = Db(db_path)
    try:
        barrier.wait(timeout=10)
        claimed = claim_ready_node(db, job_id, "work")
        queue.put({"ok": True, "claimed": claimed})
        if claimed and wait_after_claim:
            # The parent terminates this process to create the exact crash
            # window: durable running node, no run row, no cleanup callback.
            barrier.wait(timeout=60)
    except Exception as exc:
        queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        db.close()


def _pm_worker(db_path: str, home_root: str, job_id: str, barrier, queue) -> None:
    from .orchestrator import Orchestrator
    from .pricing import PriceBook

    db = Db(db_path)
    orch = Orchestrator(db, Home(Path(home_root)), PriceBook(), "http://127.0.0.1:0")
    spawned: list[bool] = []

    def discard(coro) -> None:
        spawned.append(True)
        coro.close()

    orch._spawn = discard
    try:
        barrier.wait(timeout=10)
        job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        orch._maybe_pm_diagnose(job, "business acceptance stalled")
        queue.put({"ok": True, "diagnosis_started": bool(spawned)})
    except Exception as exc:
        queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        db.close()


def _collect(processes: list[mp.Process], queue, count: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for _ in range(count):
            rows.append(queue.get(timeout=15))
    except Empty as exc:
        raise RuntimeError("multiprocess rehearsal worker did not report") from exc
    finally:
        for process in processes:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    failures = [row for row in rows if not row.get("ok")]
    if failures:
        raise RuntimeError(f"multiprocess rehearsal failed: {failures}")
    return rows


def _pair(ctx, target, args: tuple) -> tuple[list[mp.Process], Any, Any]:
    barrier = ctx.Barrier(3)
    queue = ctx.Queue()
    processes = [ctx.Process(target=target, args=(*args, barrier, queue)) for _ in range(2)]
    for process in processes:
        process.start()
    barrier.wait(timeout=10)
    return processes, barrier, queue


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("# reliability rehearsal\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=Bastet Rehearsal",
                    "-c", "user.email=rehearsal@localhost", "commit", "-qm", "seed"],
                   check=True)


def run(root: str | Path) -> dict[str, Any]:
    """Run all ownership races in an isolated directory and return receipts."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    home = Home(root / "home")
    home.ensure()
    repo = root / "repo"
    _git_repo(repo)
    db = Db(home.db_path)
    stamp = now()
    db.write_many([
        ("INSERT INTO projects(id,team_id,repo_path,status,config_json,created_at,updated_at) "
         "VALUES('rehearsal','team',?,'running','{}',?,?)", (str(repo), stamp, stamp)),
        ("INSERT INTO agents(id,amos_agent_id,name,executor_type,config_json,created_at,"
         "updated_at) VALUES('worker','worker','Worker','codex','{}',?,?)", (stamp, stamp)),
    ])
    from . import project_lifecycle
    project_lifecycle.save_task_plan(
        db, "rehearsal",
        [{"id": "race-task", "title": "race task", "spec": "prove one dispatch",
          "needs": []}], by="rehearsal", confirmed=True)
    plan_key = project_lifecycle.task_plan(db, "rehearsal")["plan_key"]
    db.close()

    ctx = mp.get_context("spawn")
    processes, _, queue = _pair(
        ctx, _dispatch_worker, (str(home.db_path), str(home.root), plan_key))
    dispatches = _collect(processes, queue, 2)

    db = Db(home.db_path)
    jobs = db.query("SELECT id FROM jobs WHERE project_id='rehearsal'")
    receipts = db.query("SELECT job_id FROM project_task_dispatches")
    nodes = db.query("SELECT job_id,stage,status FROM job_stage_nodes")
    if len(jobs) != 1 or len(receipts) != 1 or len(nodes) != 1:
        raise RuntimeError("dispatch transaction was not exactly one complete graph")
    if {row["job_id"] for row in dispatches} != {jobs[0]["id"]}:
        raise RuntimeError("dispatch contenders did not receive the same job")
    job_id = jobs[0]["id"]
    db.close()

    processes, _, queue = _pair(
        ctx, _stage_worker, (str(home.db_path), job_id, False))
    claims = _collect(processes, queue, 2)
    if sorted(row["claimed"] for row in claims) != [False, True]:
        raise RuntimeError(f"stage CAS had the wrong winners: {claims}")

    db = Db(home.db_path)
    db.write("UPDATE job_stage_nodes SET status='ready' WHERE job_id=? AND stage='work'",
             (job_id,))
    db.close()

    # Claim in a child, kill it before a run row exists, then exercise the same
    # startup recovery entrypoint used by the server lifespan.
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    crashed = ctx.Process(target=_stage_worker,
                          args=(str(home.db_path), job_id, True, barrier, queue))
    crashed.start()
    barrier.wait(timeout=10)
    crash_claim = queue.get(timeout=10)
    if not crash_claim.get("claimed"):
        raise RuntimeError(f"crash worker did not acquire stage: {crash_claim}")
    crashed.terminate()
    crashed.join(timeout=5)

    from .orchestrator import Orchestrator
    from .pricing import PriceBook
    from .stage_runtime import claim_ready_node
    db = Db(home.db_path)
    if db.one("SELECT status FROM job_stage_nodes WHERE job_id=? AND stage='work'",
              (job_id,))["status"] != "running":
        raise RuntimeError("killed owner did not leave the expected running receipt")
    restarted = Orchestrator(db, home, PriceBook(), "http://127.0.0.1:0")
    restarted._spawn = _discard_driver
    recovery = restarted.resume_interrupted_jobs(actor="rehearsal-restart")
    if recovery["resumed"] != [job_id]:
        raise RuntimeError(f"startup did not resume killed job: {recovery}")
    if not claim_ready_node(db, job_id, "work"):
        raise RuntimeError("startup recovery did not make the orphan claimable")
    db.write("UPDATE job_stage_nodes SET status='blocked' WHERE job_id=? AND stage='work'",
             (job_id,))
    db.write("UPDATE jobs SET status='blocked',rework_note='business acceptance stalled' "
             "WHERE id=?", (job_id,))
    db.close()

    processes, _, queue = _pair(
        ctx, _pm_worker, (str(home.db_path), str(home.root), job_id))
    diagnoses = _collect(processes, queue, 2)
    if sorted(row["diagnosis_started"] for row in diagnoses) != [False, True]:
        raise RuntimeError(f"PM diagnosis lease had the wrong winners: {diagnoses}")

    from . import execution_leases
    db = Db(home.db_path)
    lease = db.one("SELECT * FROM execution_leases WHERE kind='pm-diagnosis' "
                   "AND target_id=?", (job_id,))
    if lease is None:
        raise RuntimeError("PM winner left no durable ownership receipt")
    db.write("UPDATE execution_leases SET expires_at='2000-01-01T00:00:00+00:00' "
             "WHERE kind='pm-diagnosis' AND target_id=?", (job_id,))
    if not execution_leases.acquire(
            db, kind="pm-diagnosis", target_id=job_id,
            owner_id="restart-owner", ttl_s=30):
        raise RuntimeError("expired PM lease was not reclaimable after owner death")
    audit = db.one("SELECT COUNT(*) AS n FROM audit_log WHERE "
                   "action='stage.graph_nodes_recovered' AND target_id=?", (job_id,))
    db.close()

    return {
        "ok": True,
        "process_start_method": "spawn",
        "dispatch": {"contenders": 2, "jobs": 1, "receipts": 1,
                     "job_id": job_id},
        "stage_claim": {"contenders": 2, "winners": 1},
        "kill_restart": {"terminated_exitcode": crashed.exitcode,
                         "recovered_nodes": int(audit["n"] if audit else 0)},
        "pm_diagnosis": {"contenders": 2, "winners": 1,
                         "expired_lease_reclaimed": True},
    }


def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="bastet-reliability-") as directory:
        print(json.dumps(run(directory), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
