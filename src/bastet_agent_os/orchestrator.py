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

    def _emit(self, event_type: str, project_id: str | None, **payload) -> None:
        if self.bus is not None:
            self.bus.emit(event_type, project_id=project_id, **payload)

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
        return {"job_id": job_id, "runs_cancelled": killed}

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
            self._emit(f"gate.{outcome.verdict}", job["project_id"], job_id=job_id,
                       stage=stage.name, gate=stage.gate, detail=outcome.detail[:200])

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
                self._emit("job.done", job["project_id"], job_id=job_id)
                self.cleanup_worktree(job_id)
                return
            self.db.write("UPDATE jobs SET stage=?, updated_at=? WHERE id=?",
                          (stages[idx + 1].name, now(), job_id))
            self._emit("job.stage_changed", job["project_id"], job_id=job_id,
                       stage=stages[idx + 1].name)

    def _judge(self, stage: StageDef, workdir: str, result: RunResult) -> GateOutcome:
        if stage.gate == "human-approve":
            return GateOutcome("pending", "waiting for human approval")
        return evaluate_gate(stage, workdir, result.structured_verdict)

    # -- in-run interactions (SPEC §5.1.1 interaction_request) ---------------------

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
            self.cleanup_worktree(job_id)
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
            if template_id and template_id != job["template_id"]:
                template = self.db.one(
                    "SELECT stages_json FROM workflow_templates WHERE id=?",
                    (template_id,))
                if template is not None:
                    fresh = parse_stages(json.loads(template["stages_json"]))
                    names = [st.name for st in fresh]
                    if job["stage"] in names:      # keep our place in the pipeline
                        stages = fresh
                        refreshed_from = template_id
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
        agent = agent_id or job["default_agent_id"]
        if agent_id:
            self.db.write("UPDATE jobs SET default_agent_id=? WHERE id=?",
                          (agent_id, job_id))
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
            workdir = self._ensure_workdir(job, req.use_worktree)
            clear_verdict(workdir)
            token = run_tokens.issue(self.db, run_id, ttl_seconds=req.timeout_s + 300)
            self.db.write("UPDATE runs SET status='running', workdir=?, started_at=? WHERE id=?",
                          (workdir, now(), run_id))
            self._emit("run.started", job["project_id"], job_id=job["id"], run_id=run_id,
                       stage=stage.name, agent_id=agent["id"])
            context_text, report = build_context(self.db, job, stage.name,
                                                 skip=frozenset({"spec"}))
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
                timeout_s=req.timeout_s,
                allowed_tools=req.allowed_tools or ["Read", "Edit", "Write", "Bash"],
                read_only=stage.read_only,
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
            try:
                async for event in executor.stream(handle):
                    if event.type == "progress":
                        log.info("run %s: %s", run_id, event.data.get("text", "")[:120])
                    elif event.type == "interaction_request":
                        self._record_interaction(job, run_id, event.data)
            finally:
                self._live.pop(run_id, None)
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
        if resource_notes:
            parts.append(resource_notes)
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

    def cleanup_worktree(self, job_id: str) -> bool:
        """Remove a terminal job's worktree. The bastet/<job> branch and the
        diff artifact survive — only the checkout directory goes. Projects can
        opt out with config_json {"keep_worktrees": true}."""
        job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None or not job["worktree_path"]:
            return False
        project = self.db.one("SELECT * FROM projects WHERE id=?", (job["project_id"],))
        if project is None:
            return False
        if json.loads(project["config_json"] or "{}").get("keep_worktrees"):
            return False
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
        row = self.db.one("SELECT project_id, stage FROM jobs WHERE id=?", (job_id,))
        self._emit("job.blocked", row["project_id"] if row else None, job_id=job_id,
                   stage=row["stage"] if row else None, reason=reason[:200])
