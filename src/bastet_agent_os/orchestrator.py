"""Orchestrator (SPEC §2): dispatch, minimal FIFO queue, run lifecycle.

M1 scope: every dispatch creates a job on the built-in single-stage template
(gate: auto). Per-grant concurrency is enforced with asyncio semaphores —
excess dispatches queue FIFO in-process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass

from . import run_tokens
from .config import Home
from .db import Db, new_id, now
from .executors.base import RunResult, TaskSpec, get_executor
from .governance import GrantView, QuotaError, dispatch_check, resolve_grant
from .pricing import PriceBook

log = logging.getLogger("bastet.orchestrator")

SINGLE_STAGE = [{"name": "work", "gate": "auto"}]
TERMINAL = {"succeeded", "failed", "cancelled", "timeout", "orphaned"}


@dataclass
class DispatchRequest:
    project_id: str
    prompt: str
    title: str
    agent_id: str
    resource_id: str | None       # None => subscription/direct path
    timeout_s: int = 3600
    allowed_tools: list[str] | None = None
    use_worktree: bool = True


class Orchestrator:
    def __init__(self, db: Db, home: Home, prices: PriceBook, gateway_url: str):
        self.db = db
        self.home = home
        self.prices = prices
        self.gateway_url = gateway_url
        self._grant_slots: dict[str, asyncio.Semaphore] = {}
        self._tasks: set[asyncio.Task] = set()

    # -- dispatch -----------------------------------------------------------

    def dispatch(self, req: DispatchRequest) -> tuple[str, str]:
        """Validate, create job+run (queued), schedule execution. Returns ids."""
        project = self.db.one("SELECT * FROM projects WHERE id=?", (req.project_id,))
        if project is None:
            raise ValueError(f"unknown project {req.project_id!r}")
        agent = self.db.one("SELECT * FROM agents WHERE id=? AND enabled=1", (req.agent_id,))
        if agent is None:
            raise ValueError(f"unknown or disabled agent {req.agent_id!r}")

        grant: GrantView | None = None
        if req.resource_id:
            resource = self.db.one(
                "SELECT * FROM resources WHERE id=? AND kind='llm' AND enabled=1",
                (req.resource_id,),
            )
            if resource is None:
                raise ValueError(f"unknown or disabled llm resource {req.resource_id!r}")
            grant = resolve_grant(self.db, req.resource_id, req.project_id, req.agent_id)
            if grant is None:
                raise QuotaError(f"no grant covers resource {req.resource_id} for this run")
            dispatch_check(self.db, grant)  # phase-1: concurrency + budget estimate

        job_id = new_id("job")
        run_id = new_id("run")
        ts = now()
        self.db.write_many([
            ("INSERT INTO jobs(id, project_id, template_id, stages_snapshot_json, title, "
             "spec_md, stage, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
             (job_id, req.project_id, "single-stage", json.dumps(SINGLE_STAGE), req.title,
              req.prompt, "work", "in_progress", ts, ts)),
            ("INSERT INTO runs(id, job_id, stage, agent_id, executor_type, resource_id, "
             "isolation, status) VALUES(?,?,?,?,?,?,?,?)",
             (run_id, job_id, "work", req.agent_id, agent["executor_type"], req.resource_id,
              "worktree" if req.use_worktree else "none", "queued")),
        ])
        self.db.audit("user", "job.dispatch", "job", job_id,
                      {"project": req.project_id, "agent": req.agent_id,
                       "resource": req.resource_id, "title": req.title})

        task = asyncio.get_running_loop().create_task(
            self._execute(job_id, run_id, req, project, agent, grant)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job_id, run_id

    def _slot(self, grant: GrantView) -> asyncio.Semaphore:
        if grant.id not in self._grant_slots:
            self._grant_slots[grant.id] = asyncio.Semaphore(grant.max_concurrency or 1_000)
        return self._grant_slots[grant.id]

    # -- execution ----------------------------------------------------------

    async def _execute(self, job_id: str, run_id: str, req: DispatchRequest,
                       project, agent, grant: GrantView | None) -> None:
        try:
            if grant is not None:
                async with self._slot(grant):  # minimal FIFO queue per grant
                    await self._run(job_id, run_id, req, project, agent)
            else:
                await self._run(job_id, run_id, req, project, agent)
        except Exception:
            log.exception("run %s crashed", run_id)
            self._finish_run(run_id, status="failed", error="orchestrator exception")
            self._finish_job(job_id, "cancelled")
        finally:
            run_tokens.revoke_for_run(self.db, run_id)  # terminal state => token dies

    async def _run(self, job_id: str, run_id: str, req: DispatchRequest,
                   project, agent) -> None:
        workdir = self._prepare_workdir(job_id, project, req.use_worktree)
        token = run_tokens.issue(self.db, run_id, ttl_seconds=req.timeout_s + 300)
        self.db.write("UPDATE runs SET status='running', workdir=?, started_at=? WHERE id=?",
                      (workdir, now(), run_id))

        spec = TaskSpec(
            run_id=run_id,
            prompt=req.prompt,
            workdir=workdir,
            timeout_s=req.timeout_s,
            allowed_tools=req.allowed_tools or ["Read", "Edit", "Write", "Bash"],
            gateway_url=self.gateway_url if req.resource_id else None,
            run_token=token if req.resource_id else None,
        )
        executor = get_executor(agent["executor_type"])
        handle = await executor.start(spec)
        self.db.write("UPDATE runs SET executor_handle_json=? WHERE id=?",
                      (json.dumps(handle.state()), run_id))

        async for event in executor.stream(handle):
            if event.type == "progress":
                log.info("run %s: %s", run_id, event.data.get("text", "")[:120])

        result = await executor.result(handle)
        self._finalize(job_id, run_id, workdir, result)

    def _prepare_workdir(self, job_id: str, project, use_worktree: bool) -> str:
        repo = project["repo_path"]
        if not repo:
            raise ValueError(f"project {project['id']} has no repo_path")
        if not use_worktree:
            return repo
        wt_path = str(self.home.worktrees_dir / job_id)
        proc = subprocess.run(
            ["git", "-C", repo, "worktree", "add", "-b", f"bastet/{job_id}", wt_path],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            log.warning("worktree add failed (%s); running in repo directly",
                        proc.stderr.strip()[:200])
            return repo
        self.db.write("UPDATE jobs SET worktree_path=?, updated_at=? WHERE id=?",
                      (wt_path, now(), job_id))
        return wt_path

    # -- finalization -------------------------------------------------------

    def _finalize(self, job_id: str, run_id: str, workdir: str, result: RunResult) -> None:
        # gateway ledger rows win over executor-reported numbers
        ledger = self.db.one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(tokens_in),0) i, COALESCE(SUM(tokens_out),0) o, "
            "COALESCE(SUM(cache_read),0) cr, COALESCE(SUM(cache_write),0) cw, "
            "COALESCE(SUM(cost_usd),0) c FROM usage_ledger WHERE run_id=?",
            (run_id,),
        )
        if ledger and ledger["n"] > 0:
            usage = (ledger["i"], ledger["o"], ledger["cr"], ledger["cw"], ledger["c"])
            precision = "gateway"
        else:
            usage = (result.tokens_in, result.tokens_out, result.cache_read,
                     result.cache_write, result.cost_usd)
            precision = result.precision

        artifacts = dict(result.artifacts)
        diff_path = self._collect_diff(job_id, workdir)
        if diff_path:
            artifacts["diff"] = diff_path

        self.db.write(
            "UPDATE runs SET status=?, error=?, tokens_in=?, tokens_out=?, cache_read=?, "
            "cache_write=?, cost_usd=?, accounting_precision=?, finished_at=?, "
            "artifacts_json=? WHERE id=?",
            (result.status, None if result.status == "succeeded" else result.summary[:500],
             *usage, precision, now(), json.dumps(artifacts), run_id),
        )
        # single-stage template, gate: auto -> job completes with the run
        self._finish_job(job_id, "done" if result.status == "succeeded" else "blocked")
        self.db.audit("orchestrator", "run.finished", "run", run_id,
                      {"status": result.status, "precision": precision,
                       "cost_usd": round(float(usage[4]), 6)})

    def _collect_diff(self, job_id: str, workdir: str) -> str | None:
        proc = subprocess.run(["git", "-C", workdir, "diff", "HEAD"],
                              capture_output=True, text=True)
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        path = self.home.artifacts_dir / f"{job_id}.diff"
        path.write_text(proc.stdout)
        return str(path)

    def _finish_run(self, run_id: str, status: str, error: str = "") -> None:
        self.db.write("UPDATE runs SET status=?, error=?, finished_at=? WHERE id=?",
                      (status, error or None, now(), run_id))

    def _finish_job(self, job_id: str, status: str) -> None:
        self.db.write("UPDATE jobs SET status=?, updated_at=? WHERE id=?",
                      (status, now(), job_id))
