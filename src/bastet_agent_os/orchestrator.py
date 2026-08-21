"""Orchestrator (SPEC §2, §5.4): stage-driven job execution.

A job walks its stage pipeline: each stage picks an agent (by role, falling
back to the job's default agent), executes a run in the job's worktree, then
the stage's gate decides — pass advances, pending waits for human approval
(resumed via approve()), and fail goes BACK to a stage that can fix it.

That last part is the engine's reason to exist. A failing test or a rejected
review is an ordinary event in a development loop: the agents that write the
code are the ones equipped to answer it, so the card returns to the writing
stage with the failure output attached and the pipeline continues on its own.
Bastet stops for a human only when it genuinely cannot proceed — the stage is
declared `on_fail: block`, there is no earlier stage that can write, or the
loop has spent its cycles without converging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import run_memory, run_tokens
from .config import Home
from .context_engine import build_context
from .db import Db, new_id, now
from .executors.base import RunResult, TaskSpec, get_executor
from .governance import GrantView, QuotaError, dispatch_check, resolve_grant
from .pricing import PriceBook
from .role_prompts import prompt_for as role_prompt_for
from .workflow import (
    REVIEW_INSTRUCTIONS,
    GateOutcome,
    StageDef,
    clear_verdict,
    evaluate_gate,
    parse_stages,
    rework_brief,
    rework_target_for,
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
    origin: str = "dispatch"          # chat|runner|dispatch — shown on the plan


def _failure_reason(result: RunResult, workdir: str) -> str:
    """A failed run must say why. An executor that reports nothing still leaves
    us the status and where it ran — infinitely better than an empty string,
    which is what the first real dispatch failure looked like."""
    if result.summary.strip():
        return result.summary[:500]
    return (f"executor reported {result.status} with no output "
            f"(workdir: {workdir})")[:500]


class Orchestrator:
    def __init__(self, db: Db, home: Home, prices: PriceBook, gateway_url: str,
                 bus=None):
        self.db = db
        self.home = home
        self.prices = prices
        self.gateway_url = gateway_url
        self.bus = bus  # events.EventBus | None
        self._grant_slots: dict[str, asyncio.Semaphore] = {}
        self._tasks: set[asyncio.Task] = set()
        self._live: dict[str, tuple] = {}  # run_id -> (executor, handle) while streaming
        self._driving_jobs: set[str] = set()
        self._pm_diagnosing: set[str] = set()   # jobs with a PM diagnosis in flight

    def _emit(self, event_type: str, project_id: str | None, **payload) -> None:
        if self.bus is not None:
            self.bus.emit(event_type, project_id=project_id, **payload)

    def _sync_project(self, project_id: str | None) -> None:
        """Keep the project's light truthful whenever a job's status moves.
        A project reading 規劃中 while a job executes is worse than no light."""
        if not project_id:
            return
        from . import project_lifecycle as lifecycle
        try:
            moved = lifecycle.sync_from_jobs(self.db, project_id, actor="orchestrator")
        except Exception as exc:            # never let bookkeeping kill a run
            log.warning("project %s status sync failed: %r", project_id, exc)
            return
        if moved:
            self._emit("project.status", project_id, status=moved)

    # -- dispatch -------------------------------------------------------------

    def dispatch(self, req: DispatchRequest, actor: str = "user") -> str:
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
            try:
                dispatch_check(self.db, grant)
            except QuotaError as exc:
                if exc.policy != "queue":
                    raise
                # queue policy: accept the job; the stage runner waits its turn

        # explicit template > the project's assigned workflow > single stage
        template_id = req.template_id or project["default_template_id"]
        if template_id:
            row = self.db.one("SELECT * FROM workflow_templates WHERE id=?",
                              (template_id,))
            if row is None:
                raise ValueError(f"unknown template {template_id!r}")
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
            (job_id, req.project_id, template_id or "single-stage",
             json.dumps(stages_raw), req.title, req.prompt, stages[0].name,
             "in_progress", req.agent_id, req.resource_id, ts, ts),
        )
        self.db.audit(actor, "job.dispatch", "job", job_id,
                      {"project": req.project_id, "agent": req.agent_id,
                       "resource": req.resource_id, "template": req.template_id,
                       "title": req.title})
        self._emit("job.created", req.project_id, job_id=job_id, title=req.title,
                   stage=stages[0].name)
        # the plan and the board must show the same work, and the light must move
        from . import project_lifecycle as lifecycle
        try:
            lifecycle.link_job(self.db, req.project_id, job_id, req.title,
                               req.prompt, origin=req.origin)
        except Exception as exc:
            log.warning("could not link job %s to the plan: %r", job_id, exc)
        self._sync_project(req.project_id)
        self._spawn(self._drive_job(job_id, req))
        return job_id

    async def cancel_job(self, job_id: str, actor: str = "user") -> dict:
        """Stop a job now: kill whatever is streaming, mark the job cancelled.

        Used by the project stop control — a run left streaming after its job is
        cancelled would keep spending tokens on work nobody wants."""
        job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            raise ValueError("job not found")
        killed = []
        for row in self.db.query(
                "SELECT id FROM runs WHERE job_id=? AND status IN "
                "('queued','running','waiting_input')", (job_id,)):
            pair = self._live.get(row["id"])
            if pair is not None:
                executor, handle = pair
                try:
                    await executor.cancel(handle)
                except Exception as exc:            # a dead process is fine here
                    log.info("cancel run %s: %s", row["id"], type(exc).__name__)
            self.db.write("UPDATE runs SET status='cancelled', finished_at=? "
                          "WHERE id=?", (now(), row["id"]))
            run_tokens.revoke_for_run(self.db, row["id"])
            killed.append(row["id"])
        if job["status"] not in ("done", "cancelled"):
            self.db.write("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=?",
                          (now(), job_id))
        self.db.audit(actor, "job.cancel", "job", job_id, {"runs": killed})
        self._emit("job.cancelled", job["project_id"], job_id=job_id)
        self._sync_project(job["project_id"])
        return {"job_id": job_id, "runs_cancelled": killed}

    def archive_job(self, job_id: str, archived: bool, actor: str = "user") -> dict:
        """Hide a finished card without destroying anything. This is the default
        way to clear the board: every run, gate and usage row stays queryable."""
        job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            raise ValueError("job not found")
        if archived and job["status"] not in ("done", "cancelled"):
            raise ValueError(f"只能封存已結束的任務（目前 {job['status']}）— "
                             f"進行中的請先停止")
        self.db.write("UPDATE jobs SET archived=?, updated_at=? WHERE id=?",
                      (1 if archived else 0, now(), job_id))
        self.db.audit(actor, "job.archive" if archived else "job.unarchive",
                      "job", job_id, {"title": job["title"]})
        self._emit("job.archived", job["project_id"], job_id=job_id,
                   archived=archived)
        return {"job_id": job_id, "archived": archived}

    def delete_job(self, job_id: str, actor: str = "user") -> dict:
        """Remove a card and its runs for good.

        Refused when the job spent anything: usage rows hang off its runs, and
        deleting them would quietly reduce reported spend in a system whose whole
        point is honest accounting. Archive those instead. The audit log keeps a
        record of the deletion either way."""
        job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            raise ValueError("job not found")
        if job["status"] not in ("done", "cancelled"):
            raise ValueError(f"只能刪除已結束的任務（目前 {job['status']}）— "
                             f"進行中的請先停止")
        spend = self.db.one(
            "SELECT COUNT(*) AS rows, COALESCE(SUM(cost_usd), 0) AS cost "
            "FROM usage_ledger WHERE run_id IN (SELECT id FROM runs WHERE job_id=?)",
            (job_id,))
        if spend["rows"]:
            raise ValueError(
                f"這個任務有 {spend['rows']} 筆用量紀錄（${spend['cost']:.4f}），"
                f"刪除會讓帳目對不上 — 請改用封存")
        runs = [r["id"] for r in
                self.db.query("SELECT id FROM runs WHERE job_id=?", (job_id,))]
        for table in ("run_interactions", "gate_results", "run_tokens"):
            for run_id in runs:
                self.db.write(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
        self.db.write("DELETE FROM runs WHERE job_id=?", (job_id,))
        self.db.write("DELETE FROM job_deps WHERE job_id=? OR depends_on_job_id=?",
                      (job_id, job_id))
        self.cleanup_worktree(job_id)
        self.db.write("DELETE FROM jobs WHERE id=?", (job_id,))
        from . import project_lifecycle as lifecycle
        unlinked = lifecycle.unlink_job(self.db, job["project_id"], job_id)
        self.db.audit(actor, "job.delete", "job", job_id,
                      {"title": job["title"], "status": job["status"],
                       "stage": job["stage"], "runs": len(runs),
                       "plan": unlinked})
        self._emit("job.deleted", job["project_id"], job_id=job_id)
        self._sync_project(job["project_id"])
        return {"deleted": job_id, "runs": len(runs), "plan": unlinked}

    def resume_interrupted_jobs(self, actor: str = "server") -> dict:
        """Re-drive jobs whose driver died with the process.

        Startup marks runs left non-terminal as `orphaned`, but the *job* was
        being left at `in_progress` with nobody driving it: the project runner
        only resumes projects that still have undispatched plan tasks, and
        `retry` refuses anything that is not blocked. A card interrupted by a
        service restart therefore sat on the board looking alive, forever, and
        there was no button that would touch it.

        Found on the validation host: restarting the service to deploy killed a
        live CatsWalker run mid-stage; the card stayed `in_progress` for half an
        hour with no process behind it.

        Called from the lifespan, where a fresh process means `self._live` is
        empty by definition — anything still `in_progress` has no driver. A
        paused or closed project is not restarted; its card is blocked with the
        real reason instead, so the board stops claiming work is happening."""
        resumed, parked = [], []
        # read the project states ONCE: blocking the first job runs a lifecycle
        # sync that can move its project out of `paused`, and then the second job
        # of the same project would be judged against a status this loop itself
        # just changed
        states = {r["id"]: r["status"] for r in
                  self.db.query("SELECT id, status FROM projects")}
        for job in self.db.query(
                "SELECT * FROM jobs WHERE status='in_progress' AND archived=0"):
            live_runs = [r["id"] for r in self.db.query(
                "SELECT id FROM runs WHERE job_id=?", (job["id"],))
                if r["id"] in self._live]
            if live_runs:
                continue                       # a driver in this process owns it
            state = states.get(job["project_id"], "")
            if state in ("paused", "closed"):
                self._block(job["id"],
                            f"服務重啟時中斷；專案目前是 {state}，沒有自動接手。"
                            f"要繼續請先恢復專案，或用重試。",
                            stage=job["stage"])
                parked.append(job["id"])
                continue
            try:
                stages = parse_stages(json.loads(job["stages_snapshot_json"]))
                names = [s.name for s in stages]
                if job["stage"] not in names:
                    raise ValueError(f"stage {job['stage']!r} is not in the snapshot")
            except Exception as exc:
                # a job we cannot drive must say why, rather than being fed to a
                # driver that will crash on it and report `driver_crashed`
                self._block(job["id"],
                            f"服務重啟時中斷，但這張卡的工作流快照無法接續"
                            f"（{type(exc).__name__}: {exc}）。請重新派工。",
                            stage=job["stage"])
                parked.append(job["id"])
                continue
            self.db.audit(actor, "job.resumed", "job", job["id"],
                          {"stage": job["stage"], "reason": "driver lost on restart"})
            self._emit("job.resumed", job["project_id"], job_id=job["id"],
                       title=job["title"], stage=job["stage"])
            req = DispatchRequest(
                project_id=job["project_id"], prompt=job["spec_md"],
                title=job["title"], agent_id=job["default_agent_id"],
                resource_id=job["resource_id"], template_id=job["template_id"])
            self._spawn(self._drive_job(job["id"], req))
            resumed.append(job["id"])
            log.info("job %s: driver lost on restart, resuming at stage %s",
                     job["id"], job["stage"])
        return {"resumed": resumed, "parked": parked}

    async def quota_resume_loop(self) -> None:
        """Retry quota-parked jobs when their timer passes. Runs for the life of
        the server; each pass is cheap (one indexed-ish query a minute), and a
        retry failure parks the job again rather than killing the loop."""
        while True:
            try:
                due = [r["id"] for r in self.db.query(
                    "SELECT id FROM jobs WHERE status='blocked' AND resume_at "
                    "IS NOT NULL AND resume_at <= ?", (now(),))]
                for job_id in due:
                    try:
                        self.retry(job_id, user="server:quota-reset")
                        log.info("job %s: quota window passed, resumed", job_id)
                    except Exception as exc:
                        # e.g. someone retried it manually a moment ago
                        log.warning("quota resume of %s failed: %r", job_id, exc)
                        self.db.write("UPDATE jobs SET resume_at=NULL WHERE id=?",
                                      (job_id,))
            except Exception:
                log.exception("quota resume sweep failed")
            await asyncio.sleep(60)

    def purge_project_jobs(self, project_id: str, actor: str = "user") -> dict:
        """Delete every job of a project, including ones that spent money.

        `delete_job` refuses a job with usage rows, because removing them
        quietly lowers reported spend. Deleting the whole project is the one
        place that refusal has to give way — otherwise a trial project can never
        be removed. So the spend is not silently dropped: the total is returned
        and written into the audit row, which is what "honest accounting" has to
        mean when the records really are going away."""
        jobs = [dict(r) for r in self.db.query(
            "SELECT id, title, status FROM jobs WHERE project_id=?", (project_id,))]
        spend = self.db.one(
            "SELECT COUNT(*) AS rows, COALESCE(SUM(cost_usd), 0) AS cost "
            "FROM usage_ledger WHERE run_id IN (SELECT id FROM runs WHERE job_id IN "
            "(SELECT id FROM jobs WHERE project_id=?))", (project_id,))
        runs = 0
        for job in jobs:
            run_ids = [r["id"] for r in self.db.query(
                "SELECT id FROM runs WHERE job_id=?", (job["id"],))]
            runs += len(run_ids)
            for run_id in run_ids:
                for table in ("run_interactions", "gate_results", "run_tokens",
                              "usage_ledger"):
                    self.db.write(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
            self.cleanup_worktree(job["id"])
            self.db.write("DELETE FROM runs WHERE job_id=?", (job["id"],))
            self.db.write("DELETE FROM job_deps WHERE job_id=? OR depends_on_job_id=?",
                          (job["id"], job["id"]))
            self.db.write("DELETE FROM jobs WHERE id=?", (job["id"],))
        return {"jobs": len(jobs), "runs": runs,
                "usage_rows": spend["rows"], "usage_usd": round(spend["cost"], 4)}

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    SUPERVISOR_PERIOD_S = 30.0
    STALLED_PROGRESS_S = 15 * 60
    MAX_SUPERVISOR_RETRIES = 2

    def _supervisor_retry_count(self, job_id: str) -> int:
        row = self.db.one(
            "SELECT COUNT(*) AS n FROM audit_log WHERE action='job.supervisor_retry' "
            "AND target_type='job' AND target_id=?", (job_id,))
        return int(row["n"] if row else 0)

    def _recoverable_block(self, job) -> tuple[bool, str]:
        """Classify engine/executor failures, never business or human gates."""
        latest_gate = self.db.one(
            "SELECT g.gate_type, g.verdict FROM gate_results g JOIN runs r "
            "ON r.id=g.run_id WHERE r.job_id=? ORDER BY g.at DESC, g.rowid DESC LIMIT 1",
            (job["id"],))
        if latest_gate and latest_gate["gate_type"] == "human-approve" and \
                latest_gate["verdict"] == "pending":
            return False, "human approval"
        run = self.db.one(
            "SELECT error, status FROM runs WHERE job_id=? ORDER BY rowid DESC LIMIT 1",
            (job["id"],))
        text = " ".join(filter(None, [job["rework_note"] or "",
                                      run["error"] if run else ""]))
        needles = ("max turns reached", "executor reported failed with no output",
                   "driver_crashed", "driver lost", "orphaned")
        hit = next((n for n in needles if n in text.lower()), "")
        if not hit:
            from . import quota_wait
            # a depleted agent is recoverable by ROUTING, not by waiting: the
            # agent is already out of rotation, so one controlled retry lands on
            # a funded stand-in. Handling it here keeps the PM's intervention
            # budget for problems that actually need judgement.
            if quota_wait.is_credit_exhausted(text) and \
                    self._alternate_agent(job, self._last_agent(job["id"])):
                return True, "agent balance exhausted"
        return bool(hit), hit

    def _last_agent(self, job_id: str) -> str:
        row = self.db.one("SELECT agent_id FROM runs WHERE job_id=? "
                          "ORDER BY rowid DESC LIMIT 1", (job_id,))
        return row["agent_id"] if row else ""

    def _mark_depleted(self, agent_id: str, job, detail: str) -> None:
        """Take an agent out of rotation until a human tops it up.

        Only money clears this, so the engine must not retry into it and must
        say so out loud — the alternative (what actually happened) is a rework
        loop that re-dispatches a dead agent forever, burning the PM's whole
        intervention budget on a decision the router then undoes."""
        if not agent_id:
            return
        row = self.db.one("SELECT depleted_at FROM agents WHERE id=?", (agent_id,))
        if row is None or row["depleted_at"]:
            return                       # unknown, or already known to be out
        self.db.write("UPDATE agents SET depleted_at=?, depleted_reason=?, "
                      "updated_at=? WHERE id=?", (now(), detail, now(), agent_id))
        self.db.audit("orchestrator", "agent.depleted", "agent", agent_id,
                      {"job_id": job["id"], "detail": detail})
        self._emit("agent.depleted", job["project_id"], agent_id=agent_id,
                   job_id=job["id"], title=job["title"], detail=detail)
        run_memory.remember(
            self.db, job["project_id"],
            f"Agent {agent_id} 的付費額度耗盡（供應商回應：{detail[:160]}），"
            f"已暫停對它派工，直到有人充值並解除。派工會自動改由同角色的其他 agent 接手。",
            kind="warning", importance=0.85)

    def clear_depleted(self, agent_id: str, user: str = "user") -> bool:
        """A human says the balance is topped up. Only a human can."""
        row = self.db.one("SELECT depleted_at FROM agents WHERE id=?", (agent_id,))
        if row is None or not row["depleted_at"]:
            return False
        self.db.write("UPDATE agents SET depleted_at=NULL, depleted_reason=NULL, "
                      "updated_at=? WHERE id=?", (now(), agent_id))
        self.db.audit(f"user:{user}", "agent.undepleted", "agent", agent_id, {})
        return True

    def _alternate_agent(self, job, last_agent: str) -> str:
        """Prefer another enabled, funded agent for the current stage's role."""
        try:
            stages = parse_stages(json.loads(job["stages_snapshot_json"]))
            role = next(s.role for s in stages if s.name == job["stage"])
        except Exception:
            role = None
        if role:
            row = self.db.one(
                "SELECT par.agent_id FROM project_agent_roles par JOIN agents a "
                "ON a.id=par.agent_id WHERE par.project_id=? AND par.role=? "
                "AND a.enabled=1 AND a.depleted_at IS NULL AND par.agent_id<>? ORDER BY par.preference DESC LIMIT 1",
                (job["project_id"], role, last_agent or ""))
            if row:
                return row["agent_id"]
        row = self.db.one(
            "SELECT par.agent_id FROM project_agent_roles par JOIN agents a "
            "ON a.id=par.agent_id WHERE par.project_id=? AND a.enabled=1 AND a.depleted_at IS NULL "
            "AND par.agent_id<>? ORDER BY par.preference DESC LIMIT 1",
            (job["project_id"], last_agent or ""))
        if row:
            return row["agent_id"]
        return ""

    async def supervise_once(self) -> dict[str, list[str]]:
        """Act on liveness incidents instead of merely painting them orange.

        The supervisor is deliberately conservative: it interrupts only a live
        run whose *semantic progress* has been silent for fifteen minutes, and
        automatically retries only infrastructure/executor failures. Human
        gates and failed acceptance criteria remain human decisions.
        """
        interrupted, retried, resumed = [], [], []
        cutoff = (datetime.now(UTC) -
                  timedelta(seconds=self.STALLED_PROGRESS_S)).isoformat()
        for run_id, (executor, handle) in list(self._live.items()):
            row = self.db.one(
                "SELECT r.*, j.project_id, j.title FROM runs r JOIN jobs j "
                "ON j.id=r.job_id WHERE r.id=?", (run_id,))
            if not row or row["status"] != "running" or not row["started_at"]:
                continue
            semantic = row["progress_at"] or row["started_at"]
            if semantic > cutoff:
                continue
            try:
                await executor.cancel(handle)
                interrupted.append(run_id)
                self.db.audit("supervisor", "run.stalled_interrupted", "run", run_id,
                              {"job_id": row["job_id"], "last_progress": semantic})
                self._emit("run.stalled_interrupted", row["project_id"],
                           job_id=row["job_id"], run_id=run_id,
                           title=row["title"], last_progress=semantic)
                run_memory.remember(
                    self.db, row["project_id"],
                    f"專案監督器中斷假活 run {run_id}（任務「{row['title']}」）："
                    f"語意進度自 {semantic} 起未更新；保留 worktree，交由受控恢復。",
                    kind="warning", importance=0.8)
            except Exception:
                log.exception("supervisor could not interrupt %s", run_id)

        for job in self.db.query("SELECT * FROM jobs WHERE status='blocked' AND archived=0"):
            recoverable, reason = self._recoverable_block(job)
            count = self._supervisor_retry_count(job["id"])
            if not recoverable or count >= self.MAX_SUPERVISOR_RETRIES:
                if not recoverable:
                    self._maybe_pm_diagnose(job, reason)
                continue
            latest = self.db.one(
                "SELECT agent_id FROM runs WHERE job_id=? ORDER BY rowid DESC LIMIT 1",
                (job["id"],))
            alternate = self._alternate_agent(job, latest["agent_id"] if latest else "")
            self.db.audit("supervisor", "job.supervisor_retry", "job", job["id"],
                          {"reason": reason, "cycle": count + 1,
                           "agent": alternate or "role-default"})
            run_memory.remember(
                self.db, job["project_id"],
                f"專案監督器自動恢復任務「{job['title']}」：偵測到 {reason}，"
                f"第 {count + 1}/{self.MAX_SUPERVISOR_RETRIES} 次受控重試，"
                f"接手者 {alternate or '角色預設代理'}。",
                kind="procedure", importance=0.8)
            self.retry(job["id"], agent_id=alternate, user="supervisor")
            retried.append(job["id"])

        # Same-process driver loss is possible too; startup recovery alone is
        # not enough. A succeeded run without a gate is re-driven, not rerun.
        for job in self.db.query("SELECT * FROM jobs WHERE status='in_progress' AND archived=0"):
            if job["id"] in self._driving_jobs:
                continue
            live = any(self.db.one("SELECT job_id FROM runs WHERE id=?", (rid,))["job_id"]
                       == job["id"] for rid in self._live)
            if live:
                continue
            latest = self.db.one(
                "SELECT id,status FROM runs WHERE job_id=? ORDER BY rowid DESC LIMIT 1",
                (job["id"],))
            if not latest or latest["status"] not in ("succeeded", "failed", "orphaned"):
                continue
            gated = self.db.one("SELECT 1 AS x FROM gate_results WHERE run_id=?",
                                (latest["id"],))
            if latest["status"] == "succeeded" and not gated:
                req = DispatchRequest(project_id=job["project_id"], prompt=job["spec_md"],
                                      title=job["title"], agent_id=job["default_agent_id"],
                                      resource_id=job["resource_id"], template_id=job["template_id"])
                self._spawn(self._drive_job(job["id"], req))
                self.db.audit("supervisor", "job.driver_resumed", "job", job["id"],
                              {"stage": job["stage"], "after_run": latest["id"]})
                resumed.append(job["id"])
        return {"interrupted": interrupted, "retried": retried, "resumed": resumed}

    def _maybe_pm_diagnose(self, job, reason: str) -> None:
        """Hand a business stall to the project's PM — bounded, non-blocking.

        Infra failures are retried mechanically above. Everything else that
        blocks (rework budget spent, criteria disputes, missing rulings) used to
        wait for a human by construction; now the PM that planned the card gets
        first responsibility. Human-approve gates and quota waits stay out —
        one is a designed stop, the other resumes itself."""
        from . import pm_supervisor
        if reason == "human approval" or job["resume_at"]:
            return
        if job["id"] in self._pm_diagnosing:
            return
        if pm_supervisor.intervention_count(self.db, job["id"]) >= \
                pm_supervisor.MAX_INTERVENTIONS:
            return
        # an escalation is a terminal PM answer for this stall: "a human must
        # look". Re-diagnosing the same unchanged card every sweep would burn
        # tokens restating it — the latch clears when a human retries (their
        # retry makes a new blocked episode with a fresh audit trail below it).
        last = self.db.one(
            "SELECT detail_json FROM audit_log WHERE action='job.pm_intervention' "
            "AND target_id=? AND id > COALESCE((SELECT MAX(id) FROM audit_log "
            "WHERE action='job.retry' AND target_id=?), 0) "
            "ORDER BY id DESC LIMIT 1", (job["id"], job["id"]))
        if last:
            try:
                if json.loads(last["detail_json"] or "{}").get(
                        "decision", {}).get("action") == "escalate":
                    return
            except json.JSONDecodeError:
                pass
        self._pm_diagnosing.add(job["id"])

        async def _run() -> None:
            try:
                outcome = await pm_supervisor.diagnose(self, job)
                log.info("pm supervision for %s: %s", job["id"], outcome)
            except Exception:
                log.exception("pm supervision failed for %s", job["id"])
            finally:
                self._pm_diagnosing.discard(job["id"])

        self._spawn(_run())

    async def supervision_loop(self) -> None:
        while True:
            try:
                await self.supervise_once()
            except Exception:
                log.exception("project supervision sweep failed")
            await asyncio.sleep(self.SUPERVISOR_PERIOD_S)

    async def wait_idle(self) -> None:
        """Await all in-flight job drivers (used by tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- job driver -------------------------------------------------------------

    async def _drive_job(self, job_id: str, req: DispatchRequest) -> None:
        if job_id in self._driving_jobs:
            return
        self._driving_jobs.add(job_id)
        try:
            await self._advance_until_blocked(job_id, req)
        except Exception:
            log.exception("job %s driver crashed", job_id)
            self.db.write("UPDATE jobs SET status='blocked', updated_at=? WHERE id=?",
                          (now(), job_id))
            self.db.audit("orchestrator", "job.driver_crashed", "job", job_id, {})
        finally:
            self._driving_jobs.discard(job_id)

    async def _advance_until_blocked(self, job_id: str, req: DispatchRequest) -> None:
        while True:
            job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            stages = parse_stages(json.loads(job["stages_snapshot_json"]))
            names = [s.name for s in stages]
            idx = names.index(job["stage"])
            stage = stages[idx]

            result, run_id = await self._run_stage_with_retries(job, stage, req)
            if result.status in EXEC_FAILURES:
                # a quota failure is a timer, not an error: the vendor's message
                # usually states when it lifts (live case: "resets 1:30am
                # (Asia/Taipei)" blocked a card for hours until a human noticed)
                from . import quota_wait
                # a depleted balance is NOT a timer: mark the agent unusable so
                # the next dispatch routes around it instead of re-earning the
                # same instant 402 on every rework cycle
                if quota_wait.is_credit_exhausted(result.summary or ""):
                    self._mark_depleted(self._run_agent(run_id), job,
                                        (result.summary or "")[:300])
                resume_at = quota_wait.parse_reset(result.summary or "")
                if resume_at:
                    self.db.write("UPDATE jobs SET resume_at=? WHERE id=?",
                                  (resume_at, job_id))
                    self.db.audit("orchestrator", "job.quota_wait", "job", job_id,
                                  {"stage": stage.name, "resume_at": resume_at,
                                   "detail": (result.summary or "")[:200]})
                    self._emit("job.quota_wait", job["project_id"], job_id=job_id,
                               title=job["title"], stage=stage.name,
                               resume_at=resume_at,
                               detail=(result.summary or "")[:200])
                self._block(job_id, f"stage {stage.name}: execution {result.status}"
                            + (f"（額度用盡，{resume_at} 自動續跑）" if resume_at
                               else ""))
                return

            # re-read: the run may have just CREATED the worktree, and the row in
            # hand predates it. Judging the gate against the stale row meant a
            # first-stage tests-pass gate ran in the project repo instead of the
            # worktree the agent had just edited — and preview collection looked
            # for files in a directory the stage never wrote to.
            job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            workdir = job["worktree_path"] or self._project_repo(job)
            outcome = self._judge(stage, workdir, result)
            self._record_gate(run_id, stage, outcome,
                              reviewer_kind="agent",
                              reviewer_id="workflow-engine" if stage.gate == "tests-pass"
                              else (result and self._run_agent(run_id)) or "unknown")
            self.db.audit("orchestrator", f"gate.{outcome.verdict}", "job", job_id,
                          {"stage": stage.name, "gate": stage.gate,
                           "config_error": outcome.config_error,
                           "detail": outcome.detail[:300]})
            # previews ride on the ONE gate.pending event — a second emit here
            # meant every approval arrived on Telegram twice (review finding)
            previews = (self._collect_previews(job, workdir)
                        if outcome.verdict == "pending" else [])
            self._emit(f"gate.{outcome.verdict}", job["project_id"], job_id=job_id,
                       stage=stage.name, gate=stage.gate, detail=outcome.detail[:200],
                       previews=previews)

            if outcome.verdict == "pending":
                self._block(job_id, f"stage {stage.name}: waiting for human approval")
                return
            if outcome.verdict == "failed":
                if self._rework(job, stages, idx, outcome):
                    continue          # sent back to be fixed; keep driving
                return

            if idx + 1 >= len(stages):
                self.db.write("UPDATE jobs SET status='done', updated_at=? WHERE id=?",
                              (now(), job_id))
                self.db.audit("orchestrator", "job.done", "job", job_id, {})
                run_memory.job_finished(self.db, job, "done")
                self._emit("job.done", job["project_id"], job_id=job_id)
                self._sync_project(job["project_id"])
                self.cleanup_worktree(job_id)   # commits the work to bastet/<job>
                # deliver the finished branch to the project's remote — a push
                # failure is a delivery problem, never a reason to un-finish
                try:
                    from . import git_push
                    git_push.push_job_branch(self.db, job, emit=self._emit)
                except Exception as exc:
                    log.warning("job %s: auto-push crashed: %r", job_id, exc)
                return
            # the note has served its purpose: the gate it described just passed
            self.db.write("UPDATE jobs SET stage=?, rework_note=NULL, "
                          "agent_override=NULL, updated_at=? WHERE id=?",
                          (stages[idx + 1].name, now(), job_id))
            self._emit("job.stage_changed", job["project_id"], job_id=job_id,
                       stage=stages[idx + 1].name)

    def _rework(self, job, stages: list[StageDef], idx: int,
                outcome: GateOutcome) -> bool:
        """A failed gate goes back to whoever can fix it. Returns True if the
        job is moving again, False if it is now blocked.

        This is the whole point of the engine: a failing test is a normal event
        in a development loop, not an outage. The old behaviour — stop the card
        and post a one-line notification — put a human in a position to do
        something the writing agent is better placed to do. What still stops:
        `on_fail: block`, a pipeline with no earlier stage that can write, and
        running out of cycles (an agent that has failed three times is not
        converging, and by then a person genuinely needs to look)."""
        stage = stages[idx]
        job_id = job["id"]
        target_idx = rework_target_for(stages, idx)
        cycle = int(job["rework_count"] or 0) + 1
        blocked_reason = ""
        if stage.on_fail == "block":
            blocked_reason = "這一關設定為失敗即停（on_fail: block）"
        elif target_idx is None:
            blocked_reason = ("這條工作流裡沒有任何前面的可寫階段能修這個問題"
                              "（前面都是唯讀階段）")
        elif cycle > stage.max_cycles:
            blocked_reason = (f"已經返工 {stage.max_cycles} 次仍未通過，"
                              f"停下來等人看")
        if blocked_reason:
            kind = "設定問題" if outcome.config_error else "關卡未通過"
            self._block(job_id,
                        f"{stage.name} {kind}（{blocked_reason}）：{outcome.detail[:1200]}",
                        stage=stage.name, gate=stage.gate,
                        config_error=outcome.config_error,
                        detail=outcome.detail, cycles=cycle - 1)
            return False

        target = stages[target_idx]
        note = rework_brief(failed_stage=stage.name, gate=stage.gate, cycle=cycle,
                            max_cycles=stage.max_cycles, detail=outcome.detail,
                            config_error=outcome.config_error)
        self.db.write(
            "UPDATE jobs SET stage=?, status='in_progress', rework_count=?, "
            "rework_note=?, agent_override=NULL, updated_at=? WHERE id=?",
            (target.name, cycle, note, now(), job_id))
        self.db.audit("orchestrator", "job.rework", "job", job_id,
                      {"failed_stage": stage.name, "gate": stage.gate,
                       "back_to": target.name, "cycle": cycle,
                       "max_cycles": stage.max_cycles,
                       "config_error": outcome.config_error,
                       "detail": outcome.detail[:1200]})
        self._emit("job.rework", job["project_id"], job_id=job_id,
                   title=job["title"], failed_stage=stage.name, gate=stage.gate,
                   back_to=target.name, role=target.role or "", cycle=cycle,
                   max_cycles=stage.max_cycles,
                   config_error=outcome.config_error,
                   detail=outcome.detail[:2000])
        run_memory.gate_failed(self.db, job, stage.name, stage.gate,
                               outcome.detail, target.name, cycle)
        log.info("job %s: gate %s failed, sent back to %s (cycle %d/%d)",
                 job_id, stage.name, target.name, cycle, stage.max_cycles)
        return True

    def _judge(self, stage: StageDef, workdir: str, result: RunResult) -> GateOutcome:
        if stage.gate == "human-approve":
            return GateOutcome("pending", "waiting for human approval")
        return evaluate_gate(stage, workdir, result.structured_verdict,
                             reviewer_output=result.summary)

    # -- in-run interactions (SPEC §5.1.1 interaction_request) ---------------------

    LIVENESS_PERIOD_S = 20

    async def _liveness_beat(self, run_id: str, handle) -> None:
        """Beat while the run is alive, even when it says nothing.

        A one-shot executor (agy's `json` mode, every read-only reviewer) prints
        nothing until it exits, so a stream-only heartbeat left those cards
        looking dead for their entire life. This beat claims only what it can
        check — the process has not exited — and never touches progress_text,
        so "alive" and "said something" stay separable on the board."""
        process = getattr(handle, "process", None)
        while True:
            await asyncio.sleep(self.LIVENESS_PERIOD_S)
            if process is not None and process.returncode is not None:
                return                      # exited: result() owns it from here
            try:
                self.db.write("UPDATE runs SET heartbeat_at=? WHERE id=? "
                              "AND status='running'", (now(), run_id))
            except Exception:               # a beat must never break a run
                log.warning("run %s: liveness beat failed", run_id, exc_info=True)
                return

    def _record_interaction(self, job, run_id: str, data: dict) -> None:
        request_id = str(data.get("request_id") or new_id("itx"))
        self.db.write(
            "INSERT INTO run_interactions(id, run_id, request_id, kind, payload_json, "
            "created_at) VALUES(?,?,?,?,?,?)",
            (new_id("itx"), run_id, request_id, data.get("kind"),
             json.dumps(data.get("payload") or {}), now()))
        self.db.write("UPDATE runs SET status='waiting_input' WHERE id=? AND status='running'",
                      (run_id,))
        self.db.audit("orchestrator", "run.interaction_request", "run", run_id,
                      {"request_id": request_id, "kind": data.get("kind")})
        self._emit("run.waiting_input", job["project_id"], job_id=job["id"], run_id=run_id,
                   request_id=request_id, kind=data.get("kind"),
                   summary=str(data.get("payload") or {})[:200])

    async def respond(self, run_id: str, request_id: str, reply: dict,
                      user: str = "user") -> dict:
        """Answer a pending in-run interaction (permission request etc.)."""
        pair = self._live.get(run_id)
        if pair is None:
            raise ValueError(f"run {run_id} is not live (finished or not interactive)")
        executor, handle = pair
        await executor.respond(handle, request_id, reply)
        self.db.write(
            "UPDATE run_interactions SET status='answered', reply_json=?, answered_at=? "
            "WHERE run_id=? AND request_id=? AND status='pending'",
            (json.dumps(reply), now(), run_id, request_id))
        self.db.write("UPDATE runs SET status='running' WHERE id=? AND status='waiting_input'",
                      (run_id,))
        self.db.audit(f"user:{user}", "run.interaction_reply", "run", run_id,
                      {"request_id": request_id, "reply": reply})
        return {"run_id": run_id, "request_id": request_id, "status": "answered"}

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
            self.db.audit("orchestrator", "job.done", "job", job_id,
                          {"via": "approve"})
            run_memory.job_finished(self.db, job, "done")
            self._emit("job.done", job["project_id"], job_id=job_id)
            self.cleanup_worktree(job_id)
            # a job approved into done deserves the same delivery as one that
            # finishes in the driver loop — the live art card completed via this
            # path and never pushed, silently (no audit row of any kind)
            try:
                from . import git_push
                git_push.push_job_branch(self.db, job, emit=self._emit)
            except Exception as exc:
                log.warning("job %s: auto-push crashed: %r", job_id, exc)
            self._sync_project(job["project_id"])
            return {"job_id": job_id, "status": "done"}
        self.db.write("UPDATE jobs SET stage=?, status='in_progress', updated_at=? WHERE id=?",
                      (names[idx + 1], now(), job_id))
        req = DispatchRequest(
            project_id=job["project_id"], prompt=job["spec_md"], title=job["title"],
            agent_id=job["default_agent_id"], resource_id=job["resource_id"])
        self._spawn(self._drive_job(job_id, req))
        return {"job_id": job_id, "status": "in_progress", "stage": names[idx + 1]}

    def retry(self, job_id: str, agent_id: str = "", user: str = "user",
              spec: str = "", refresh_workflow: bool = True) -> dict:
        """Run the current stage again after a failure.

        A blocked card with no way forward is a dead end: the operator fixed the
        repo path, logged the agent in, or freed the budget, and wants the same
        stage attempted again — with a different agent if the first one is the
        problem. Only jobs that are actually stuck may be retried; a running job
        would end up with two drivers."""
        job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            raise ValueError(f"unknown job {job_id!r}")
        if job["status"] not in ("blocked", "cancelled"):
            raise ValueError(f"job {job_id} is {job['status']}, not stuck "
                             f"(only blocked/cancelled jobs can be retried)")
        # Re-read the project as it is NOW. Retrying with the state that already
        # failed just fails again: the operator has fixed the repo path, changed
        # the workflow, or corrected the spec — that is the whole point.
        stages = parse_stages(json.loads(job["stages_snapshot_json"]))
        refreshed_from = None
        if refresh_workflow:
            project = self.db.one("SELECT default_template_id FROM projects WHERE id=?",
                                  (job["project_id"],))
            template_id = project["default_template_id"] if project else None
            if template_id:
                template = self.db.one(
                    "SELECT stages_json, version FROM workflow_templates WHERE id=?",
                    (template_id,))
                # compare the STAGES, not the template id: fixing a stage's test
                # command edits the same template in place, and that is the most
                # common reason to retry at all
                changed = template is not None and (
                    template_id != job["template_id"]
                    or json.loads(template["stages_json"])
                    != json.loads(job["stages_snapshot_json"]))
                if template is not None and changed:
                    fresh = parse_stages(json.loads(template["stages_json"]))
                    names = [st.name for st in fresh]
                    if job["stage"] in names:      # keep our place in the pipeline
                        stages = fresh
                        refreshed_from = f"{template_id} v{template['version']}"
                        self.db.write(
                            "UPDATE jobs SET template_id=?, stages_snapshot_json=? "
                            "WHERE id=?",
                            (template_id, template["stages_json"], job_id))
                    else:
                        log.info("job %s stays on its snapshot: stage %r is not in "
                                 "template %s", job_id, job["stage"], template_id)
        if spec.strip() and spec.strip() != (job["spec_md"] or "").strip():
            self.db.write("UPDATE jobs SET spec_md=? WHERE id=?",
                          (spec.strip(), job_id))
            job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job["stage"] not in [s.name for s in stages]:
            raise ValueError(f"job {job_id} is at unknown stage {job['stage']!r}")
        # a HUMAN retry is a fresh lease for the rework loop (the live case: a
        # card spent its cycles on a transient DNS failure and three manual
        # retries produced three instant re-blocks). The automatic quota-reset
        # retry is not a human judgement, so it must NOT refill the budget — a
        # vendor limit interleaving with a rework loop would otherwise disable
        # the cycles cap entirely.
        if user.startswith("server:"):
            self.db.write("UPDATE jobs SET resume_at=NULL WHERE id=?", (job_id,))
        else:
            self.db.write("UPDATE jobs SET rework_count=0, rework_note=NULL, "
                          "resume_at=NULL WHERE id=?", (job_id,))
        agent = agent_id or job["default_agent_id"]
        if agent_id:
            # the human picked WHO runs this retry. Role assignment normally
            # outranks the job default, so without an explicit override the
            # picked agent silently lost to the role mapping — the live case
            # retried with Claude1 and watched Codex1 fail again identically.
            self.db.write("UPDATE jobs SET default_agent_id=?, agent_override=? "
                          "WHERE id=?", (agent_id, agent_id, job_id))
            # naming a depleted agent IS the human saying it has funds again —
            # otherwise the override would silently lose to the routing filter
            # and they would watch a different agent run instead
            if not user.startswith(("server:", "pm-supervisor:", "supervisor")):
                self.clear_depleted(agent_id, user=user)
        self.db.write("UPDATE jobs SET status='in_progress', updated_at=? WHERE id=?",
                      (now(), job_id))
        self.db.audit(f"user:{user}", "job.retry", "job", job_id,
                      {"stage": job["stage"], "agent": agent,
                       "workflow_refreshed": refreshed_from,
                       "spec_edited": bool(spec.strip())})
        self._emit("job.retried", job["project_id"], job_id=job_id, stage=job["stage"])
        req = DispatchRequest(
            project_id=job["project_id"], prompt=job["spec_md"], title=job["title"],
            agent_id=agent, resource_id=job["resource_id"])
        self._spawn(self._drive_job(job_id, req))
        return {"job_id": job_id, "status": "in_progress", "stage": job["stage"],
                "workflow_refreshed": refreshed_from}

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
        resource = None
        if job["resource_id"]:
            resource = self.db.one("SELECT * FROM resources WHERE id=?", (job["resource_id"],))
            grant = resolve_grant(self.db, job["resource_id"], job["project_id"], agent["id"])
            if grant is None:
                raise QuotaError(f"no grant covers resource {job['resource_id']}")
            await self._await_grant(grant, timeout_s=req.timeout_s)

        run_id = new_id("run")
        self.db.write(
            "INSERT INTO runs(id, job_id, stage, attempt, agent_id, executor_type, "
            "resource_id, isolation, status) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, job["id"], stage.name, attempt, agent["id"], agent["executor_type"],
             job["resource_id"], stage.isolation, "queued"),
        )

        async def _go() -> RunResult:
            run_memory.ensure_org(self.db, job["project_id"], agent["id"])
            workdir = self._ensure_workdir(job, req.use_worktree)
            clear_verdict(workdir)
            token = run_tokens.issue(
                self.db, run_id,
                ttl_seconds=(stage.timeout_s or req.timeout_s) + 300)
            self.db.write("UPDATE runs SET status='running', workdir=?, started_at=? WHERE id=?",
                          (workdir, now(), run_id))
            self._emit("run.started", job["project_id"], job_id=job["id"], run_id=run_id,
                       stage=stage.name, agent_id=agent["id"])
            context_text, report = build_context(
                self.db, job, stage.name, skip=frozenset({"spec"}),
                recall=run_memory.recall_kwargs(self.db, job, agent["id"]))
            self.db.audit("orchestrator", "context.assembled", "run", run_id,
                          report.to_dict())
            # model: agent config > resource routing default > official default
            agent_cfg = json.loads(agent["config_json"] or "{}")
            model = agent_cfg.get("model")
            flavor = None
            if resource is not None:
                routing = json.loads(resource["routing_json"] or "{}")
                model = model or routing.get("default_model")
                flavor = resource["api_flavor"]
            llm = {"flavor": flavor, "model": model} if (model or flavor) else None
            extra_env: dict[str, str] = self._project_secrets(job, run_id)
            # granted pool resources: env vars + an MCP config the agent can use
            from . import resource_access
            access = resource_access.build(
                self.db, self.home.root, job["project_id"],
                self._project_team(job["project_id"]), run_id,
                audit_actor=f"run:{run_id}")
            extra_env.update(access.env)
            account_id = agent["account_id"] if "account_id" in agent.keys() else None
            if account_id:
                from .executors.accounts import account_env
                account = self.db.one("SELECT * FROM executor_accounts WHERE id=?",
                                      (account_id,))
                if account is not None:
                    # merge, never replace: the account picks the executor profile,
                    # it must not drop the project's credentials and resources
                    extra_env.update(account_env(agent["executor_type"],
                                                 account["home_dir"]))
            spec = TaskSpec(
                run_id=run_id,
                prompt=self._stage_prompt(job, stage, access.notes),
                workdir=workdir,
                # a stage that declares its own budget wins over the dispatch
                # default — heavy stages were losing an hour of work to 3600s
                timeout_s=stage.timeout_s or req.timeout_s,
                # WebFetch/WebSearch included by default: an agent implementing
                # against a vendor API needs the vendor's docs, and "no
                # permission" was the live complaint
                allowed_tools=req.allowed_tools or ["Read", "Edit", "Write", "Bash",
                                                    "WebFetch", "WebSearch"],
                read_only=stage.read_only,
                # the verdict schema binds the agent's entire answer — it must
                # reach review gates only, never other read-only runs
                expect_verdict=(stage.gate == "agent-review"),
                context_text=context_text,
                gateway_url=self.gateway_url if job["resource_id"] else None,
                run_token=token if job["resource_id"] else None,
                llm=llm,
                extra_env=extra_env,
                mcp_config=access.mcp_config_path,
                isolation=stage.isolation,
                container_image=json.loads(
                    self.db.one("SELECT config_json FROM projects WHERE id=?",
                                (job["project_id"],))["config_json"] or "{}"
                ).get("container_image"),
            )
            executor = get_executor(agent["executor_type"])
            handle = await executor.start(spec)
            self.db.write("UPDATE runs SET executor_handle_json=? WHERE id=?",
                          (json.dumps(handle.state()), run_id))
            self._live[run_id] = (executor, handle)
            last_beat = 0.0
            watchdog = asyncio.create_task(self._liveness_beat(run_id, handle))
            try:
                async for event in executor.stream(handle):
                    if event.type == "progress":
                        text = event.data.get("text", "")
                        log.info("run %s: %s", run_id, text[:120])
                        # liveness for the board: what the run last said, and
                        # when. Throttled — an agent can emit hundreds of lines
                        # a minute and each one is a write plus a WS fanout.
                        clock = asyncio.get_event_loop().time()
                        if clock - last_beat >= 2.0:
                            last_beat = clock
                            stamp = now()
                            self.db.write(
                                "UPDATE runs SET heartbeat_at=?, progress_at=?, "
                                "progress_text=? WHERE id=?",
                                (stamp, stamp, text[:300], run_id))
                            self._emit("run.progress", job["project_id"],
                                       job_id=job["id"], run_id=run_id,
                                       stage=stage.name, text=text[:200])
                    elif event.type == "interaction_request":
                        self._record_interaction(job, run_id, event.data)
            finally:
                watchdog.cancel()
                self._live.pop(run_id, None)
            result = await executor.result(handle)
            self._finalize_run(job["id"], run_id, workdir, result)
            # every executor contributes to team memory, not just bastet-lite:
            # this is the write side the memory bucket was missing
            if result.status not in EXEC_FAILURES:
                run_memory.stage_done(self.db, job, stage.name, agent["id"],
                                      result.summary)
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
            from . import resource_access
            resource_access.cleanup(self.home.root, run_id)  # MCP file holds secrets

    def _slot(self, grant: GrantView) -> asyncio.Semaphore:
        if grant.id not in self._grant_slots:
            self._grant_slots[grant.id] = asyncio.Semaphore(grant.max_concurrency or 1_000)
        return self._grant_slots[grant.id]

    # polling interval for queued grants; tests shrink this
    queue_poll_s: float = 5.0

    async def _await_grant(self, grant: GrantView, timeout_s: int) -> None:
        """Phase-1 check with queue semantics: `queue` waits FIFO-ish for
        budget/concurrency to free up; `block`/`degrade` raise immediately."""
        waited = 0.0
        while True:
            try:
                dispatch_check(self.db, grant)
                return
            except QuotaError as exc:
                if exc.policy != "queue":
                    raise
                if waited >= timeout_s:
                    raise QuotaError(f"grant {grant.id}: queued past timeout "
                                     f"({timeout_s}s)", policy="queue") from exc
                await asyncio.sleep(self.queue_poll_s)
                waited += self.queue_poll_s

    # -- prompt assembly (task-layer context, SPEC §5.6 minimal) --------------------

    def _stage_prompt(self, job, stage: StageDef, resource_notes: str = "") -> str:
        # pipeline history / deps / memory travel via TaskSpec.context_text
        # (context engine, §5.6); the prompt carries only the spec + scaffold
        parts = []
        role_prompt = role_prompt_for(self.db, stage.role)
        if role_prompt:
            # the role definition frames HOW this stage's agent should behave
            parts.append(f"## 你的角色（{stage.role}）\n{role_prompt}")
        parts += [f"# Task: {job['title']}", job["spec_md"]]
        if self._has_media_resources(job):
            # a vendor's download URL expires; only a file in the worktree
            # survives to the bastet/<job> branch and the remote
            parts.append(
                "## 生成資產的保存（重要）\n"
                "用媒體資源（圖片/影片/音樂/語音）生成的產物，必須在這個階段結束前"
                "**下載成 worktree 裡的實體檔案**，放到專案慣用的資產目錄"
                "（assets/、public/、或任務指定的路徑）。廠商回傳的下載 URL 有"
                "時效，過期就什麼都不剩；工作流會把 worktree 的檔案 commit 到任務"
                "分支並推到遠端 —— 只有真的存在的檔案會被保存。\n"
                "**絕對不要把生成丟到背景然後結束回合**：這是一次性的 headless "
                "執行，你結束的瞬間所有子行程一併回收，沒有任何『完成通知』會送達"
                "—— 曾有任務因此空轉三輪（每輪都啟動管線、退場、什麼都沒留下）。"
                "正確做法是**前景等待**：迴圈裡輪詢（sleep 後檢查檔案是否落地），"
                "直到所有目標檔案存在、驗證通過才結束。生成很多張就分批，每批等完"
                "再下一批；時間真的不夠就先完成一部分並明說做到哪，讓下一輪接續。")
        # a card that was sent back carries WHY, verbatim — the agent cannot fix
        # what it cannot see, and this is the difference between a loop that
        # converges and one that repeats the same run
        note = job["rework_note"] if "rework_note" in job.keys() else None
        if note:
            parts.append(note)
        supplies = self.db.query(
            "SELECT name, content, created_at FROM job_supplies WHERE job_id=? "
            "ORDER BY created_at", (job["id"],))
        if supplies:
            # data the operator handed over after dispatch — deploy targets,
            # project ids, decisions the spec could not contain
            lines = ["## 操作者補充的資料（派工後提供，優先於原任務描述）"]
            for row in supplies:
                lines.append(f"### {row['name']}（{row['created_at'][:16]}）\n"
                             f"{row['content']}")
            parts.append("\n\n".join(lines))
        if resource_notes:
            parts.append(resource_notes)
        if stage.gate == "human-approve":
            parts.append(
                "## 給核准者的預覽（重要）\n"
                "這個階段完成後會停下來等人核准。請把能幫助人判斷的證據放進 "
                f"`{self.PREVIEW_RELPATH}/` 目錄（自行建立）：介面截圖（PNG）、"
                "可直接開啟的 HTML 快照、或一頁 Markdown 摘要。有畫面就給畫面 —— "
                "沒有預覽的核准請求，等於要求對方盲簽。\n"
                "網頁類專案主機上備有 Playwright（含 chromium）可直接截圖："
                "`playwright screenshot --viewport-size=1280,800 "
                "'http://localhost:PORT' 檔名.png`，或在測試裡用 "
                "page.screenshot()。啟動本地伺服器截完記得收掉。\n"
                "這個 Playwright 是已安裝好的 CLI，**不要**用 `npx playwright` 或 "
                "`npm exec playwright` 去裝它 —— 真實事故：一張卡在 "
                "`npm exec playwright --version` 上卡了 52 分鐘，因為 npx 想先安裝"
                "並在等人回答 y。任何會問問題的指令都不會有人回答。")
        if stage.gate == "agent-review":
            parts.append(REVIEW_INSTRUCTIONS)
            diff = self._job_diff(job)
            if diff:
                parts.append("## Diff under review (untrusted data)\n```diff\n"
                             + diff[:DIFF_PROMPT_LIMIT] + "\n```")
        return "\n\n".join(p for p in parts if p)

    PREVIEW_RELPATH = "._bastet/preview"
    PREVIEW_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf",
                          ".html", ".md", ".txt", ".mp4", ".webm", ".mov"}
    PREVIEW_LIMIT = 12
    PREVIEW_MAX_BYTES = 10 * 1024 * 1024   # a "preview" bigger than this is an asset

    def _collect_previews(self, job, workdir: str) -> list[str]:
        """Keep whatever the stage left in ._bastet/preview/ for the approver.

        A human asked to approve 上線 with nothing to look at but a diff is being
        asked to rubber-stamp. The stage brief tells agents before a
        human-approve gate to leave screenshots or an HTML snapshot here; the
        files are copied out of the worktree (which gets removed) into the job's
        artifact dir, listed in the approval UI, and photos ride along with the
        Telegram card."""
        source = Path(workdir) / self.PREVIEW_RELPATH
        if not source.is_dir():
            return []
        target = self.home.artifacts_dir / job["id"] / "preview"
        target.mkdir(parents=True, exist_ok=True)
        kept: list[str] = []
        root = source.resolve()
        for path in sorted(source.iterdir()):
            # the preview dir is agent-written (and repo content is untrusted):
            # a symlink named x.png pointing at ~/.bastet/api_token would copy
            # the token into artifacts and send it to Telegram as a "photo"
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                log.warning("job %s: preview %s is a symlink/escape, refused",
                            job["id"], path.name)
                continue
            if not path.is_file() or path.suffix.lower() not in self.PREVIEW_EXTENSIONS:
                continue
            if len(kept) >= self.PREVIEW_LIMIT:
                log.info("job %s: preview limit reached, skipping %s",
                         job["id"], path.name)
                break
            if path.stat().st_size > self.PREVIEW_MAX_BYTES:
                log.info("job %s: preview %s over %d bytes, skipped",
                         job["id"], path.name, self.PREVIEW_MAX_BYTES)
                continue
            safe = Path(path.name).name          # no traversal via crafted names
            (target / safe).write_bytes(path.read_bytes())
            kept.append(safe)
        if kept:
            # A directory of opaque filenames is not a review package. Generate
            # the index ourselves so WebUI and Telegram always carry the same
            # concrete checklist, even when the agent forgot to write one.
            rows = ["# 核准附件清單", "", f"任務：{job['title']}（{job['id']}）", "",
                    "| 檔案 | 類型 | 大小 | 檢核方式 |", "|---|---|---:|---|"]
            for name in kept:
                path = target / name
                ext = path.suffix.lower().lstrip(".") or "file"
                size = path.stat().st_size
                method = ("直接檢視畫面" if ext in {"png", "jpg", "jpeg", "gif", "webp"}
                          else "播放並檢查動態" if ext in {"mp4", "webm", "mov"}
                          else "開啟附件核對內容")
                rows.append(f"| `{name}` | {ext} | {size:,} B | {method} |")
            manifest = "_review-manifest.md"
            (target / manifest).write_text("\n".join(rows) + "\n", encoding="utf-8")
            kept.insert(0, manifest)
            self.db.audit("orchestrator", "job.previews", "job", job["id"],
                          {"files": kept, "manifest": manifest})
        return kept

    MEDIA_KINDS = ("image", "video", "music", "tts", "stt", "model3d")

    def _has_media_resources(self, job) -> bool:
        project = self.db.one("SELECT team_id FROM projects WHERE id=?",
                              (job["project_id"],))
        team = project["team_id"] if project else ""
        row = self.db.one(
            "SELECT 1 AS x FROM grants g JOIN resources r ON r.id=g.resource_id "
            "WHERE r.enabled=1 AND g.enabled=1 AND r.kind IN "
            "('image','video','music','tts','stt') AND "
            "(g.scope_type='global' OR (g.scope_type='project' AND g.scope_id=?) "
            " OR (g.scope_type='team' AND g.scope_id=?)) LIMIT 1",
            (job["project_id"], team))
        return row is not None

    def _job_diff(self, job) -> str | None:
        workdir = job["worktree_path"] or self._project_repo(job)
        proc = subprocess.run(["git", "-C", workdir, "diff", "HEAD"],
                              capture_output=True, text=True)
        return proc.stdout if proc.returncode == 0 and proc.stdout.strip() else None

    # -- agents / workdir -----------------------------------------------------------

    def _agent_for_stage(self, job, stage: StageDef):
        override = (job["agent_override"] if "agent_override" in job.keys()
                    else None)
        if override:
            row = self.db.one("SELECT * FROM agents WHERE id=? AND enabled=1 "
                              "AND depleted_at IS NULL", (override,))
            if row is not None:
                return row
            log.info("agent override %r unavailable; falling back", override)
        if stage.role:
            row = self.db.one(
                "SELECT a.* FROM project_agent_roles par JOIN agents a ON a.id = par.agent_id "
                "WHERE par.project_id=? AND par.role=? AND a.enabled=1 AND a.depleted_at IS NULL "
                "ORDER BY par.preference DESC LIMIT 1",
                (job["project_id"], stage.role))
            if row is not None:
                return row
            log.info("no agent for role %r in project %s; using job default",
                     stage.role, job["project_id"])
        agent = self.db.one("SELECT * FROM agents WHERE id=? AND enabled=1 "
                            "AND depleted_at IS NULL", (job["default_agent_id"],))
        if agent is not None:
            return agent
        # last resort: any funded agent on this project. Without it, a role with
        # exactly one agent (the live case: `tester` = Grok1 alone) dead-ends the
        # moment that agent's balance empties, even with capable stand-ins sitting
        # right there under other roles.
        stand_in = self.db.one(
            "SELECT a.* FROM project_agent_roles par JOIN agents a ON a.id=par.agent_id "
            "WHERE par.project_id=? AND a.enabled=1 AND a.depleted_at IS NULL "
            "ORDER BY par.preference DESC LIMIT 1", (job["project_id"],))
        if stand_in is not None:
            log.warning("job %s: %r has no funded agent; standing in with %s",
                        job["id"], stage.role or "default", stand_in["id"])
            return stand_in
        depleted = self.db.one(
            "SELECT id FROM agents WHERE id=? AND depleted_at IS NOT NULL",
            (job["default_agent_id"],))
        if depleted is not None:
            raise ValueError(
                f"job {job['id']}：所有可用 agent 的付費額度都用盡了（含 "
                f"{depleted['id']}）。充值後在「組織 → Agents」解除暫停，或指派"
                f"其他 agent 到這個角色。")
        raise ValueError(f"job {job['id']}: default agent unavailable")

    def _project_repo(self, job) -> str:
        """The repo, expanded and checked. A missing repo is a configuration
        error worth failing on: running the agent in some other directory
        produces a confusing failure instead of an obvious one."""
        from .config import expand_repo_path, is_git_repo

        project = self.db.one("SELECT * FROM projects WHERE id=?", (job["project_id"],))
        if not project or not project["repo_path"]:
            raise ValueError(f"project {job['project_id']} has no repo_path")
        repo = expand_repo_path(project["repo_path"])
        if not Path(repo).is_dir():
            raise ValueError(
                f"專案 {job['project_id']} 的 repo 路徑在 Bastet 主機上不存在："
                f"{repo}（設定值：{project['repo_path']}）")
        if not is_git_repo(repo):
            raise ValueError(
                f"專案 {job['project_id']} 的 repo 路徑不是 git repo：{repo}"
                f"（worktree 隔離需要 git；先在該目錄 git init 或改成正確路徑）")
        return repo

    def _worktree_base(self, job, repo: str) -> str | None:
        """Choose a stable start point for a new job worktree.

        A project's checked-out branch is operator state, not workflow input.
        Prefer an explicit project ``base_ref``, then the conventional local
        primary branches.  Returning ``None`` preserves Git's HEAD fallback for
        repositories that genuinely have neither.
        """
        project = self.db.one("SELECT config_json FROM projects WHERE id=?",
                              (job["project_id"],))
        config = json.loads(project["config_json"] or "{}") if project else {}
        candidates = [config.get("base_ref"), "main", "master"]
        for candidate in candidates:
            if not candidate:
                continue
            exists = subprocess.run(
                ["git", "-C", repo, "rev-parse", "--verify", "--quiet",
                 f"{candidate}^{{commit}}"],
                capture_output=True, text=True,
            )
            if exists.returncode == 0:
                return candidate
        return None

    def _ensure_workdir(self, job, use_worktree: bool) -> str:
        if job["worktree_path"]:
            return job["worktree_path"]
        repo = self._project_repo(job)
        if not use_worktree:
            return repo
        wt_path = str(self.home.worktrees_dir / job["id"])
        if Path(wt_path).exists():
            return wt_path
        # an explicit start point, never the ambient HEAD: `worktree add` with
        # no commit-ish branches from whatever the repo happens to have checked
        # out. Live case: the host repo sat on an old feature branch, so every
        # new card started from the 2D prototype while main carried the 3D
        # work — the implementer and reviewer then disagreed forever about a
        # baseline neither of them had chosen.
        base = self._worktree_base(job, repo)
        cmd = ["git", "-C", repo, "worktree", "add",
               "-b", f"bastet/{job['id']}", wt_path]
        if base:
            cmd.append(base)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            log.warning("worktree add failed (%s); running in repo directly",
                        proc.stderr.strip()[:200])
            return repo
        self.db.write("UPDATE jobs SET worktree_path=?, updated_at=? WHERE id=?",
                      (wt_path, now(), job["id"]))
        return wt_path

    def _project_secrets(self, job, run_id: str) -> dict[str, str]:
        """Resolve credentials whose scope covers this project (SPEC §5.8).

        Resolution happens at run start, is audited, and the value lands in the
        run's environment — anything injected must be assumed reachable by the
        agent, so scope these tightly and prefer short-lived tokens."""
        from . import secrets_store

        env: dict[str, str] = {}
        rows = self.db.query(
            "SELECT DISTINCT r.id, r.name, r.secret_ref, r.config_json FROM grants g "
            "JOIN resources r ON r.id = g.resource_id "
            "WHERE r.kind='secret' AND r.enabled=1 AND g.enabled=1 AND "
            "(g.scope_type='global' OR (g.scope_type='project' AND g.scope_id=?) "
            " OR (g.scope_type='team' AND g.scope_id=?))",
            (job["project_id"], self._project_team(job["project_id"])))
        for row in rows:
            config = json.loads(row["config_json"] or "{}")
            env_name = config.get("env_name")
            if not env_name:
                continue
            try:
                env[env_name] = secrets_store.resolve(row["secret_ref"])
            except secrets_store.SecretError as exc:
                log.warning("secret %s unresolved: %s", row["name"], exc)
                continue
            self.db.audit(f"run:{run_id}", "secret.resolve", "resource", row["id"],
                          {"env_name": env_name, "project": job["project_id"]})
        return env

    def _project_team(self, project_id: str) -> str:
        row = self.db.one("SELECT team_id FROM projects WHERE id=?", (project_id,))
        return row["team_id"] if row else ""

    # -- worktree lifecycle (SPEC §5.4.3) ----------------------------------------------

    def _commit_worktree(self, job, workdir: str) -> str | None:
        """Commit whatever the agents produced onto the job's own branch.

        Without this, cleanup threw the work away. `git worktree remove --force`
        deletes uncommitted changes, and no stage commits anything, so a job that
        ran a full rework loop and passed its tests left the branch pointing at
        the commit it started from — the fix recoverable only by hand-applying a
        diff file. Verified on a live host: the agent correctly changed
        `a - b` to `a + b`, the gate went green, and the edit was then deleted.

        This commits to `bastet/<job_id>`, never to the project's own branch:
        merging stays a deliberate step (the 合併發布 / deploy stages), which is
        the part that should keep asking a human."""
        status = subprocess.run(["git", "-C", workdir, "status", "--porcelain"],
                                capture_output=True, text=True)
        if status.returncode != 0 or not status.stdout.strip():
            return None                       # nothing to keep
        title = (job["title"] or job["id"])[:60]
        message = (f"bastet: {title}\n\njob {job['id']}\n"
                   f"stage {job['stage']} · status {job['status']}")
        for args in (["add", "-A"],
                     ["-c", "user.name=Bastet Agent OS",
                      "-c", "user.email=bastet@localhost",
                      "commit", "--no-verify", "-q", "-m", message]):
            proc = subprocess.run(["git", "-C", workdir, *args],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                log.warning("could not preserve job %s work (%s): %s", job["id"],
                            args[0], proc.stderr.strip()[:200])
                return None
        head = subprocess.run(["git", "-C", workdir, "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        sha = head.stdout.strip()[:12] if head.returncode == 0 else "?"
        self.db.audit("orchestrator", "worktree.committed", "job", job["id"],
                      {"branch": f"bastet/{job['id']}", "commit": sha})
        log.info("job %s: work committed to bastet/%s (%s)", job["id"],
                 job["id"], sha)
        return sha

    def cleanup_worktree(self, job_id: str) -> bool:
        """Remove a terminal job's worktree, keeping the work.

        Anything the agents changed is committed to the bastet/<job> branch
        first, so the branch and the diff artifact both survive and only the
        checkout directory goes. Projects can opt out of removal entirely with
        config_json {"keep_worktrees": true}."""
        job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None or not job["worktree_path"]:
            return False
        project = self.db.one("SELECT * FROM projects WHERE id=?", (job["project_id"],))
        if project is None:
            return False
        if json.loads(project["config_json"] or "{}").get("keep_worktrees"):
            return False
        self._commit_worktree(job, job["worktree_path"])
        proc = subprocess.run(
            ["git", "-C", project["repo_path"], "worktree", "remove", "--force",
             job["worktree_path"]],
            capture_output=True, text=True)
        if proc.returncode != 0:
            log.warning("worktree remove failed for %s: %s", job_id,
                        proc.stderr.strip()[:200])
            return False
        self.db.write("UPDATE jobs SET worktree_path=NULL, updated_at=? WHERE id=?",
                      (now(), job_id))
        self.db.audit("orchestrator", "worktree.removed", "job", job_id,
                      {"path": job["worktree_path"]})
        return True

    def gc_worktrees(self) -> int:
        """Sweep worktrees left behind by terminal jobs (crashes, old versions)."""
        removed = 0
        for row in self.db.query(
                "SELECT id FROM jobs WHERE status IN ('done','cancelled') "
                "AND worktree_path IS NOT NULL"):
            if self.cleanup_worktree(row["id"]):
                removed += 1
        return removed

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
            (result.status,
             None if result.status == "succeeded" else _failure_reason(result, workdir),
             *usage, precision, now(), json.dumps(artifacts), run_id),
        )
        self.db.audit("orchestrator", "run.finished", "run", run_id,
                      {"status": result.status, "precision": precision,
                       "cost_usd": round(float(usage[4]), 6)})
        row = self.db.one("SELECT project_id FROM jobs WHERE id=?", (job_id,))
        self._emit("run.finished", row["project_id"] if row else None, job_id=job_id,
                   run_id=run_id, status=result.status,
                   cost_usd=round(float(usage[4]), 6))

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
            "reviewer_id, detail_md, config_error, at) VALUES(?,?,?,?,?,?,?,?,?)",
            (new_id("gte"), run_id, stage.gate, outcome.verdict, reviewer_kind,
             reviewer_id, outcome.detail[:2000],
             1 if outcome.config_error else 0, now()),
        )

    def _run_agent(self, run_id: str) -> str:
        row = self.db.one("SELECT agent_id FROM runs WHERE id=?", (run_id,))
        return row["agent_id"] if row else "unknown"

    def _block(self, job_id: str, reason: str, **facts) -> None:
        """Stop the card — and say enough that a person can act on it.

        A blocked notification used to read `🟠 job.blocked: job_abc stage
        tests-pass`, which tells you a thing broke and nothing about what. The
        event now carries the title, the gate, how many rework cycles were spent,
        and the failing output, because the notification is usually the only
        place anyone reads it."""
        self.db.write("UPDATE jobs SET status='blocked', updated_at=? WHERE id=?",
                      (now(), job_id))
        self.db.audit("orchestrator", "job.blocked", "job", job_id,
                      {"reason": reason[:1500], **facts})
        job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is not None:
            run_memory.job_finished(self.db, job, "blocked", reason)
        row = self.db.one("SELECT project_id, stage, title FROM jobs WHERE id=?",
                          (job_id,))
        self._emit("job.blocked", row["project_id"] if row else None, job_id=job_id,
                   title=row["title"] if row else "",
                   stage=facts.get("stage") or (row["stage"] if row else None),
                   reason=reason[:1500],
                   gate=facts.get("gate", ""),
                   config_error=bool(facts.get("config_error")),
                   cycles=facts.get("cycles", 0),
                   detail=str(facts.get("detail", ""))[:2000])
        self._sync_project(row["project_id"] if row else None)
