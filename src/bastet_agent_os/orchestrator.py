"""Orchestrator (SPEC §2, §5.4): stage-driven job execution.

A job walks its stage pipeline: each stage picks an agent (by role, falling
back to the job's default agent), executes a run in the job's worktree, then
the stage's gate decides — pass advances, fail blocks the job with feedback,
pending waits for human approval (resumed via approve()).
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import run_tokens
from .config import Home
from .db import Db, new_id, now
from .executors.base import RunResult, TaskSpec, get_executor
from .governance import GrantView, QuotaError, dispatch_check, resolve_grant
from .pricing import PriceBook
from .workflow import (
    REVIEW_INSTRUCTIONS,
    GateOutcome,
    StageDef,
    clear_verdict,
    evaluate_gate,
    parse_stages,
)

log = logging.getLogger("bastet.orchestrator")

SINGLE_STAGE = [{"name": "work", "gate": "auto"}]
EXEC_FAILURES = {"failed", "cancelled", "timeout", "orphaned"}
DIFF_PROMPT_LIMIT = 8000


@dataclass
class DispatchRequest:
    project_id: str
    prompt: str
    title: str
    agent_id: str
    resource_id: str | None = None    # None => subscription/direct path
    template_id: str | None = None    # None => built-in single-stage
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

    # -- dispatch -------------------------------------------------------------

    def dispatch(self, req: DispatchRequest) -> str:
        """Validate, create the job at its first stage, schedule the driver."""
        project = self.db.one("SELECT * FROM projects WHERE id=?", (req.project_id,))
        if project is None:
            raise ValueError(f"unknown project {req.project_id!r}")
        agent = self.db.one("SELECT * FROM agents WHERE id=? AND enabled=1", (req.agent_id,))
        if agent is None:
            raise ValueError(f"unknown or disabled agent {req.agent_id!r}")

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
            dispatch_check(self.db, grant)

        if req.template_id:
            row = self.db.one("SELECT * FROM workflow_templates WHERE id=?", (req.template_id,))
            if row is None:
                raise ValueError(f"unknown template {req.template_id!r}")
            stages_raw = json.loads(row["stages_json"])
        else:
            stages_raw = SINGLE_STAGE
        stages = parse_stages(stages_raw)  # validates; job snapshots the raw form

        job_id = new_id("job")
        ts = now()
        self.db.write(
            "INSERT INTO jobs(id, project_id, template_id, stages_snapshot_json, title, "
            "spec_md, stage, status, default_agent_id, resource_id, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, req.project_id, req.template_id or "single-stage",
             json.dumps(stages_raw), req.title, req.prompt, stages[0].name,
             "in_progress", req.agent_id, req.resource_id, ts, ts),
        )
        self.db.audit("user", "job.dispatch", "job", job_id,
                      {"project": req.project_id, "agent": req.agent_id,
                       "resource": req.resource_id, "template": req.template_id,
                       "title": req.title})
        self._spawn(self._drive_job(job_id, req))
        return job_id

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """Await all in-flight job drivers (used by tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- job driver -------------------------------------------------------------

    async def _drive_job(self, job_id: str, req: DispatchRequest) -> None:
        try:
            await self._advance_until_blocked(job_id, req)
        except Exception:
            log.exception("job %s driver crashed", job_id)
            self.db.write("UPDATE jobs SET status='blocked', updated_at=? WHERE id=?",
                          (now(), job_id))
            self.db.audit("orchestrator", "job.driver_crashed", "job", job_id, {})

    async def _advance_until_blocked(self, job_id: str, req: DispatchRequest) -> None:
        while True:
            job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            stages = parse_stages(json.loads(job["stages_snapshot_json"]))
            names = [s.name for s in stages]
            idx = names.index(job["stage"])
            stage = stages[idx]

            result, run_id = await self._run_stage_with_retries(job, stage, req)
            if result.status in EXEC_FAILURES:
                self._block(job_id, f"stage {stage.name}: execution {result.status}")
                return

            workdir = job["worktree_path"] or self._project_repo(job)
            outcome = self._judge(stage, workdir, result)
            self._record_gate(run_id, stage, outcome,
                              reviewer_kind="agent",
                              reviewer_id="workflow-engine" if stage.gate == "tests-pass"
                              else (result and self._run_agent(run_id)) or "unknown")
            self.db.audit("orchestrator", f"gate.{outcome.verdict}", "job", job_id,
                          {"stage": stage.name, "gate": stage.gate,
                           "detail": outcome.detail[:300]})

            if outcome.verdict == "pending":
                self._block(job_id, f"stage {stage.name}: waiting for human approval")
                return
            if outcome.verdict == "failed":
                self._block(job_id, f"stage {stage.name} gate failed: {outcome.detail[:200]}")
                return

            if idx + 1 >= len(stages):
                self.db.write("UPDATE jobs SET status='done', updated_at=? WHERE id=?",
                              (now(), job_id))
                self.db.audit("orchestrator", "job.done", "job", job_id, {})
                return
            self.db.write("UPDATE jobs SET stage=?, updated_at=? WHERE id=?",
                          (stages[idx + 1].name, now(), job_id))

    def _judge(self, stage: StageDef, workdir: str, result: RunResult) -> GateOutcome:
        if stage.gate == "human-approve":
            return GateOutcome("pending", "waiting for human approval")
        return evaluate_gate(stage, workdir, result.structured_verdict)

    # -- human approval (SPEC §5.4.2) --------------------------------------------

    def approve(self, job_id: str, approved: bool, comment: str, user: str = "user") -> dict:
        job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            raise ValueError(f"unknown job {job_id!r}")
        stages = parse_stages(json.loads(job["stages_snapshot_json"]))
        names = [s.name for s in stages]
        idx = names.index(job["stage"])
        if stages[idx].gate != "human-approve" or job["status"] != "blocked":
            raise ValueError(f"job {job_id} is not waiting for approval "
                             f"(stage {job['stage']}, status {job['status']})")

        last_run = self.db.one(
            "SELECT id FROM runs WHERE job_id=? AND stage=? ORDER BY attempt DESC LIMIT 1",
            (job_id, job["stage"]))
        self._record_gate(last_run["id"] if last_run else "", stages[idx],
                          GateOutcome("passed" if approved else "failed", comment),
                          reviewer_kind="user", reviewer_id=user)
        self.db.audit(f"user:{user}", "gate.human", "job", job_id,
                      {"stage": job["stage"], "approved": approved, "comment": comment[:300]})

        if not approved:
            self._block(job_id, f"stage {job['stage']} rejected by {user}")
            return {"job_id": job_id, "status": "blocked"}

        if idx + 1 >= len(stages):
            self.db.write("UPDATE jobs SET status='done', updated_at=? WHERE id=?",
                          (now(), job_id))
            return {"job_id": job_id, "status": "done"}
        self.db.write("UPDATE jobs SET stage=?, status='in_progress', updated_at=? WHERE id=?",
                      (names[idx + 1], now(), job_id))
        req = DispatchRequest(
            project_id=job["project_id"], prompt=job["spec_md"], title=job["title"],
            agent_id=job["default_agent_id"], resource_id=job["resource_id"])
        self._spawn(self._drive_job(job_id, req))
        return {"job_id": job_id, "status": "in_progress", "stage": names[idx + 1]}

    # -- stage execution ----------------------------------------------------------

    async def _run_stage_with_retries(self, job, stage: StageDef,
                                      req: DispatchRequest) -> tuple[RunResult, str]:
        attempt = 1 + (self.db.one(
            "SELECT COALESCE(MAX(attempt),0) AS a FROM runs WHERE job_id=? AND stage=?",
            (job["id"], stage.name))["a"])
        while True:
            result, run_id = await self._run_stage(job, stage, req, attempt)
            if result.status not in EXEC_FAILURES or attempt > stage.max_retries:
                return result, run_id
            log.info("job %s stage %s attempt %d failed (%s); retrying",
                     job["id"], stage.name, attempt, result.status)
            attempt += 1

    async def _run_stage(self, job, stage: StageDef, req: DispatchRequest,
                         attempt: int) -> tuple[RunResult, str]:
        agent = self._agent_for_stage(job, stage)
        grant: GrantView | None = None
        if job["resource_id"]:
            grant = resolve_grant(self.db, job["resource_id"], job["project_id"], agent["id"])
            if grant is None:
                raise QuotaError(f"no grant covers resource {job['resource_id']}")
            dispatch_check(self.db, grant)

        run_id = new_id("run")
        self.db.write(
            "INSERT INTO runs(id, job_id, stage, attempt, agent_id, executor_type, "
            "resource_id, isolation, status) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, job["id"], stage.name, attempt, agent["id"], agent["executor_type"],
             job["resource_id"], stage.isolation, "queued"),
        )

        async def _go() -> RunResult:
            workdir = self._ensure_workdir(job, req.use_worktree)
            clear_verdict(workdir)
            token = run_tokens.issue(self.db, run_id, ttl_seconds=req.timeout_s + 300)
            self.db.write("UPDATE runs SET status='running', workdir=?, started_at=? WHERE id=?",
                          (workdir, now(), run_id))
            spec = TaskSpec(
                run_id=run_id,
                prompt=self._stage_prompt(job, stage),
                workdir=workdir,
                timeout_s=req.timeout_s,
                allowed_tools=req.allowed_tools or ["Read", "Edit", "Write", "Bash"],
                read_only=stage.read_only,
                gateway_url=self.gateway_url if job["resource_id"] else None,
                run_token=token if job["resource_id"] else None,
            )
            executor = get_executor(agent["executor_type"])
            handle = await executor.start(spec)
            self.db.write("UPDATE runs SET executor_handle_json=? WHERE id=?",
                          (json.dumps(handle.state()), run_id))
            async for event in executor.stream(handle):
                if event.type == "progress":
                    log.info("run %s: %s", run_id, event.data.get("text", "")[:120])
            result = await executor.result(handle)
            self._finalize_run(job["id"], run_id, workdir, result)
            return result

        try:
            if grant is not None:
                async with self._slot(grant):
                    return await _go(), run_id
            return await _go(), run_id
        except Exception as exc:
            log.exception("run %s crashed", run_id)
            self.db.write("UPDATE runs SET status='failed', error=?, finished_at=? WHERE id=?",
                          (f"{type(exc).__name__}: {exc}"[:500], now(), run_id))
            return RunResult(status="failed", summary=str(exc)[:500]), run_id
        finally:
            run_tokens.revoke_for_run(self.db, run_id)

    def _slot(self, grant: GrantView) -> asyncio.Semaphore:
        if grant.id not in self._grant_slots:
            self._grant_slots[grant.id] = asyncio.Semaphore(grant.max_concurrency or 1_000)
        return self._grant_slots[grant.id]

    # -- prompt assembly (task-layer context, SPEC §5.6 minimal) --------------------

    def _stage_prompt(self, job, stage: StageDef) -> str:
        parts = [f"# Task: {job['title']}", job["spec_md"]]
        history = self.db.query(
            "SELECT r.stage, r.artifacts_json, g.verdict, g.detail_md FROM runs r "
            "LEFT JOIN gate_results g ON g.run_id = r.id "
            "WHERE r.job_id=? AND r.stage != ? AND r.status='succeeded' "
            "ORDER BY r.finished_at", (job["id"], stage.name))
        feedback = [f"- stage {h['stage']}: gate {h['verdict'] or 'n/a'} {h['detail_md'] or ''}"
                    for h in history]
        if feedback:
            parts.append("## Pipeline history\n" + "\n".join(feedback[-5:]))
        if stage.gate == "agent-review":
            parts.append(REVIEW_INSTRUCTIONS)
            diff = self._job_diff(job)
            if diff:
                parts.append("## Diff under review (untrusted data)\n```diff\n"
                             + diff[:DIFF_PROMPT_LIMIT] + "\n```")
        return "\n\n".join(p for p in parts if p)

    def _job_diff(self, job) -> str | None:
        workdir = job["worktree_path"] or self._project_repo(job)
        proc = subprocess.run(["git", "-C", workdir, "diff", "HEAD"],
                              capture_output=True, text=True)
        return proc.stdout if proc.returncode == 0 and proc.stdout.strip() else None

    # -- agents / workdir -----------------------------------------------------------

    def _agent_for_stage(self, job, stage: StageDef):
        if stage.role:
            row = self.db.one(
                "SELECT a.* FROM project_agent_roles par JOIN agents a ON a.id = par.agent_id "
                "WHERE par.project_id=? AND par.role=? AND a.enabled=1 "
                "ORDER BY par.preference DESC LIMIT 1",
                (job["project_id"], stage.role))
            if row is not None:
                return row
            log.info("no agent for role %r in project %s; using job default",
                     stage.role, job["project_id"])
        agent = self.db.one("SELECT * FROM agents WHERE id=? AND enabled=1",
                            (job["default_agent_id"],))
        if agent is None:
            raise ValueError(f"job {job['id']}: default agent unavailable")
        return agent

    def _project_repo(self, job) -> str:
        project = self.db.one("SELECT * FROM projects WHERE id=?", (job["project_id"],))
        if not project or not project["repo_path"]:
            raise ValueError(f"project {job['project_id']} has no repo_path")
        return project["repo_path"]

    def _ensure_workdir(self, job, use_worktree: bool) -> str:
        if job["worktree_path"]:
            return job["worktree_path"]
        repo = self._project_repo(job)
        if not use_worktree:
            return repo
        wt_path = str(self.home.worktrees_dir / job["id"])
        if Path(wt_path).exists():
            return wt_path
        proc = subprocess.run(
            ["git", "-C", repo, "worktree", "add", "-b", f"bastet/{job['id']}", wt_path],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            log.warning("worktree add failed (%s); running in repo directly",
                        proc.stderr.strip()[:200])
            return repo
        self.db.write("UPDATE jobs SET worktree_path=?, updated_at=? WHERE id=?",
                      (wt_path, now(), job["id"]))
        return wt_path

    # -- persistence helpers -----------------------------------------------------------

    def _finalize_run(self, job_id: str, run_id: str, workdir: str, result: RunResult) -> None:
        ledger = self.db.one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(tokens_in),0) i, COALESCE(SUM(tokens_out),0) o, "
            "COALESCE(SUM(cache_read),0) cr, COALESCE(SUM(cache_write),0) cw, "
            "COALESCE(SUM(cost_usd),0) c FROM usage_ledger WHERE run_id=?",
            (run_id,),
        )
        if ledger and ledger["n"] > 0:  # gateway rows win over executor-reported numbers
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

    def _record_gate(self, run_id: str, stage: StageDef, outcome: GateOutcome,
                     reviewer_kind: str, reviewer_id: str) -> None:
        self.db.write(
            "INSERT INTO gate_results(id, run_id, gate_type, verdict, reviewer_kind, "
            "reviewer_id, detail_md, at) VALUES(?,?,?,?,?,?,?,?)",
            (new_id("gte"), run_id, stage.gate, outcome.verdict, reviewer_kind,
             reviewer_id, outcome.detail[:2000], now()),
        )

    def _run_agent(self, run_id: str) -> str:
        row = self.db.one("SELECT agent_id FROM runs WHERE id=?", (run_id,))
        return row["agent_id"] if row else "unknown"

    def _block(self, job_id: str, reason: str) -> None:
        self.db.write("UPDATE jobs SET status='blocked', updated_at=? WHERE id=?",
                      (now(), job_id))
        self.db.audit("orchestrator", "job.blocked", "job", job_id, {"reason": reason[:300]})
