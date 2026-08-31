"""Executing a project: PM decomposition, then dependency-aware dispatch.

Two halves of the same idea. First a **project-manager agent** turns the agreed
plan into concrete tasks — it proposes, a human confirms, nothing runs before
that. Then the **runner** walks the confirmed list: dispatch a task, let the
project's workflow drive it through its stages and role-assigned agents, wait
for it to settle, take the next one.

Legacy plans remain sequential. Graph-native plans declare stable task ids and
``needs`` edges; every ready node may run concurrently up to
``config_json.max_parallel``. Pause stops new claims and leaves current tasks to
finish; stop cancels in-flight jobs. A blocked task waits for a human and blocks
only its descendants, never an independent branch.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from . import project_lifecycle as lifecycle
from .db import now
from .orchestrator import DispatchRequest
from .project_budget import ProjectBudgetExceeded, sync_pause

log = logging.getLogger("bastet.project")

POLL_S = 3.0
DECOMPOSE_TIMEOUT_S = 300
MAX_TASKS = 40
TERMINAL = ("done", "cancelled")

DECOMPOSE_INSTRUCTIONS = """\
你是這個專案的專案經理。根據以下專案資料與規劃討論，把專案拆分成可獨立派工的任務。

規則：
- 每個任務要能由一個 agent 在一次工作流中完成，有明確的完成定義
- 每個任務必須有穩定、簡短的 id，並用 needs 宣告直接前置任務 id
- 沒有相依關係的任務 needs 為 []，讓它們可以並行；不要用陣列順序假裝依賴
- 任務數量控制在 3～12 個之間，不要拆到無法驗收的碎片
- spec 要寫得讓執行者不必回頭問人：範圍、驗收條件、要動到哪些部分
- 每個任務必須宣告 delivery：純分析用 none、非開發交付可用 branch、一般程式工作
  用 integration 合併並核驗遠端目標分支、真正上線用 production；production 必須填
  一個不同於目前正式版的新 version
- 只有最後一張整合/發布卡可以用 production，不要讓每張功能卡各自部署
- 不要發明專案沒提到的需求；資訊不足就在 spec 裡寫明「需確認：…」

重要：拆分需要的資訊全部在下方，**不要使用任何工具**（不要讀檔、不要搜尋、
不要執行指令）。headless 模式無法詢問權限，工具呼叫會被拒絕而讓這次拆分失敗。

只輸出 JSON，格式如下，不要有其他文字：
{"tasks":[{"id":"stable-id","title":"任務標題","needs":[],"spec":"完整任務說明與驗收條件","role":"（可留空）建議角色 id","delivery":{"mode":"none|branch|integration|production","version":"production 時必填"}}]}
"""


class PlanError(Exception):
    pass


# ---- decomposition ---------------------------------------------------------------

def pm_agent(db, project_id: str) -> Any:
    """The agent assigned the `pm` role on this project (highest preference).

    Depleted agents are skipped: a PM with no balance cannot decompose or
    diagnose, and dispatching to it produces an instant 402 instead of a plan."""
    row = db.one(
        "SELECT a.* FROM project_agent_roles par JOIN agents a ON a.id = par.agent_id "
        "WHERE par.project_id=? AND par.role='pm' AND a.enabled=1 "
        "AND a.depleted_at IS NULL "
        "ORDER BY par.preference DESC LIMIT 1", (project_id,))
    return row


def _planning_context(db, project_id: str) -> str:
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if project is None:
        raise PlanError("project not found")
    config = json.loads(project["config_json"] or "{}")
    parts = [f"## 專案\n- id：{project_id}\n- team：{project['team_id']}\n"
             f"- repo：{project['repo_path'] or '未設定'}\n"
             f"- 說明：{config.get('description') or '（未填寫）'}"]

    if project["default_template_id"]:
        template = db.one("SELECT stages_json FROM workflow_templates WHERE id=?",
                          (project["default_template_id"],))
        if template is not None:
            stages = json.loads(template["stages_json"])
            parts.append("## 每個任務會走的工作流階段\n" + "\n".join(
                f"{i + 1}. {st.get('name')}（角色：{st.get('role') or '不指定'}，"
                f"關卡：{st.get('gate')}）" for i, st in enumerate(stages)))
    roles = db.query("SELECT par.role, a.name FROM project_agent_roles par "
                     "JOIN agents a ON a.id = par.agent_id WHERE par.project_id=?",
                     (project_id,))
    if roles:
        parts.append("## 可用的團隊\n" + "\n".join(
            f"- {r['role']}：{r['name']}" for r in roles))

    # the planning discussion is the actual input: this is what was agreed
    from . import chat as chat_mod
    said: list[str] = []
    for session in chat_mod.list_sessions(db, "project", project_id):
        for message in chat_mod.messages(db, session["id"], limit=40):
            if message["role"] in ("user", "assistant") and message["content"].strip():
                said.append(f"[{message['role']}] {message['content']}")
    if said:
        parts.append("## 規劃討論紀錄\n" + "\n\n".join(said[-40:]))
    return "\n\n".join(parts)


def parse_tasks(text: str) -> list[dict[str, Any]]:
    """Pull the task list out of the agent's answer.

    Real answers are messy: prose around the JSON, a preamble object, several
    objects in a row, or a bare array. So we scan every `{`/`[` and take the
    first value that actually carries tasks. A model that returns only prose is
    a failed decomposition — that we report rather than guess around."""
    decoder = json.JSONDecoder()
    raw: Any = None
    for index, char in enumerate(text or ""):
        if char not in "{[":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue                     # not the start of a JSON value
        candidate = payload.get("tasks") if isinstance(payload, dict) else payload
        if isinstance(candidate, list) and candidate:
            raw = candidate
            break
    if raw is None:
        # show what it actually said: "did not return JSON" with no evidence is
        # the least useful error message in the system
        excerpt = " ".join((text or "").split())[:400] or "(empty output)"
        raise PlanError(f"the PM agent did not return a JSON task list. "
                        f"It said: {excerpt}")
    tasks = []
    for item in raw[:MAX_TASKS]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        spec = str(item.get("spec") or item.get("description") or "").strip()
        if not title:
            continue
        delivery_value = item.get("delivery") or {"mode": "integration"}
        if isinstance(delivery_value, str):
            delivery_value = {"mode": delivery_value}
        try:
            from .delivery import normalize
            delivery_value = normalize(delivery_value)
        except (TypeError, ValueError) as exc:
            raise PlanError(f"task {title!r} has invalid delivery: {exc}") from exc
        needs = item.get("needs", [])
        if not isinstance(needs, list):
            raise PlanError(f"task {title!r} has invalid needs; expected a list")
        task = {"title": title[:120], "spec": spec or title,
                "role": str(item.get("role") or "").strip(),
                "delivery": delivery_value}
        if item.get("id"):
            task["id"] = str(item["id"]).strip()
        if "needs" in item:
            task["needs"] = needs
        tasks.append(task)
    if not tasks:
        raise PlanError("no usable tasks in the decomposition")
    return tasks


async def decompose(db, home_root, project_id: str, agent_id: str = "",
                    actor: str = "") -> list[dict[str, Any]]:
    """Ask the PM agent to split the project into tasks. Stores the proposal
    unconfirmed — a human still has to say go."""
    from .executors.base import TaskSpec, get_executor

    agent = (db.one("SELECT * FROM agents WHERE id=? AND enabled=1", (agent_id,))
             if agent_id else pm_agent(db, project_id))
    if agent is None:
        raise PlanError("這個專案沒有指派專案經理（pm 角色）的 agent，"
                        "請先在專案頁指派，或指定要用哪個 agent 拆分")
    from pathlib import Path

    from .config import expand_repo_path

    project = db.one("SELECT repo_path FROM projects WHERE id=?", (project_id,))
    workdir = expand_repo_path(project["repo_path"]) if project else ""
    if not workdir or not Path(workdir).is_dir():
        workdir = str(home_root)

    agent_cfg = json.loads(agent["config_json"] or "{}")
    extra_env: dict[str, str] = {}
    if "account_id" in agent.keys() and agent["account_id"]:
        from .executors.accounts import account_env
        account = db.one("SELECT * FROM executor_accounts WHERE id=?",
                         (agent["account_id"],))
        if account is not None:
            extra_env = account_env(agent["executor_type"], account["home_dir"])

    from .db import new_id
    spec = TaskSpec(
        run_id=new_id("plan"),
        prompt=f"{DECOMPOSE_INSTRUCTIONS}\n\n{_planning_context(db, project_id)}",
        workdir=workdir,
        timeout_s=DECOMPOSE_TIMEOUT_S,
        read_only=True,                 # planning reads the repo, never writes
        llm={"model": agent_cfg.get("model")} if agent_cfg.get("model") else None,
        extra_env=extra_env,
        isolation="plan",
    )
    executor = get_executor(agent["executor_type"])
    handle = await executor.start(spec)
    async for _ in executor.stream(handle):
        pass
    result = await executor.result(handle)
    db.audit(actor or "system", "project.decompose.raw", "project", project_id,
             {"agent": agent["id"], "status": result.status,
              "output": (result.summary or "")[:1500]})
    if not result.summary:
        raise PlanError(f"PM agent produced no output (status: {result.status})")
    try:
        fresh = lifecycle.normalize_task_graph(parse_tasks(result.summary))
    except lifecycle.LifecycleError as exc:
        raise PlanError(str(exc)) from exc
    # a re-run replaces the *proposal*, not the work already dispatched: losing
    # those rows would cut the plan's link to running jobs
    dispatched = [t for t in lifecycle.task_plan(db, project_id)["tasks"]
                  if t.get("job_id")]
    chat = lifecycle.chat_state(db, project_id)
    lifecycle.save_task_plan(db, project_id, [*dispatched, *fresh],
                             by=agent["id"],
                             source={"kind": "chat", "at": now(),
                                     "messages": chat["messages"],
                                     "chat_at": chat["last_at"]})
    db.audit(actor or "system", "project.decompose", "project", project_id,
             {"agent": agent["id"], "tasks": len(fresh),
              "kept_dispatched": len(dispatched),
              "from_chat_messages": chat["messages"]})
    return fresh


# ---- execution -------------------------------------------------------------------

class ProjectRunner:
    """One asyncio task per running project; the DB status is the source of
    truth so a restart cannot leave a project 'running' with nothing running."""

    def __init__(self, db, orch, bus=None):
        self.db = db
        self.orch = orch
        self.bus = bus
        self._tasks: dict[str, asyncio.Task] = {}

    def is_active(self, project_id: str) -> bool:
        task = self._tasks.get(project_id)
        return bool(task and not task.done())

    def active_projects(self) -> list[str]:
        return [pid for pid, task in self._tasks.items() if not task.done()]

    def ensure_running(self, project_id: str, actor: str = "server") -> bool:
        """Start the loop if this project should be progressing and none is alive.

        The loop only ever lived in memory, so a control-plane restart ended a
        project's run silently: its status still said 執行中 while nothing
        dispatched the next task. Idempotent, so it is safe to call on every job
        transition and at startup — automatic continuation is the whole promise."""
        if self.is_active(project_id):
            return False
        try:
            if lifecycle.status_of(self.db, project_id) != lifecycle.RUNNING:
                return False
        except lifecycle.LifecycleError:
            return False
        plan = lifecycle.task_plan(self.db, project_id)
        if not plan["confirmed"] or not plan["tasks"]:
            return False
        if not self._work_left(plan["tasks"]):
            return False
        try:
            self.admit(project_id, "", actor=actor)
        except PlanError:
            return False
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._loop(project_id, "", actor))
        self._tasks[project_id] = task
        task.add_done_callback(lambda t: self._finished(project_id, t))
        self.db.audit(actor, "project.runner.resumed", "project", project_id,
                      {"tasks": len(plan["tasks"])})
        log.info("project %s: runner resumed", project_id)
        return True

    def _work_left(self, tasks: list[dict[str, Any]]) -> bool:
        for task in tasks:
            job_id = task.get("job_id")
            if not job_id:
                return True                      # never dispatched
            row = self.db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
            if row is not None and row["status"] not in TERMINAL:
                return True                      # still moving (or awaiting a human)
        return False

    async def watch(self, bus) -> None:
        """Revive a project's runner whenever one of its jobs settles.

        A loop can die for reasons we did not foresee; every job transition is a
        chance to notice and carry on, instead of a project quietly stopping."""
        queue = bus.subscribe()
        try:
            while True:
                event = await queue.get()
                if event.get("type") not in ("job.done", "job.blocked",
                                             "job.cancelled", "gate.passed"):
                    continue
                project_id = event.get("project_id")
                if not project_id:
                    continue
                try:
                    self.ensure_running(project_id, actor="watcher")
                except Exception as exc:          # never let the watcher die
                    log.warning("runner watch on %s failed: %r", project_id, exc)
        finally:
            bus.unsubscribe(queue)

    def start(self, project_id: str, agent_id: str, actor: str = "") -> dict[str, Any]:
        plan = lifecycle.task_plan(self.db, project_id)
        if not plan["tasks"]:
            raise PlanError("還沒有任務拆分 — 先請專案經理 agent 拆分並確認")
        if not plan["confirmed"]:
            raise PlanError("任務拆分尚未經人工確認")
        try:
            self.admit(project_id, agent_id, actor=actor)
        except PlanError:
            # Direct callers historically moved the lifecycle before start().
            # Keep that path truthful too; the HTTP path admits before moving.
            if lifecycle.status_of(self.db, project_id) == lifecycle.RUNNING:
                lifecycle.apply(self.db, project_id, "stop", actor or "runner",
                                {"reason": "project admission blocked"})
            raise
        if self.is_active(project_id):
            return {"already_running": True}
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._loop(project_id, agent_id, actor))
        self._tasks[project_id] = task
        task.add_done_callback(lambda t: self._finished(project_id, t))
        return {"started": True, "tasks": len(plan["tasks"])}

    def admit(self, project_id: str, agent_id: str = "", actor: str = "") -> dict[str, Any]:
        """Require one viable route for every task and every workflow stage."""
        from . import admission
        plan = lifecycle.task_plan(self.db, project_id)
        report = admission.project_plan_report(
            self.db, project_id, lifecycle.normalize_task_graph(plan["tasks"]),
            fallback_agent_id=agent_id, require_default=True)
        action = ("project.admission.passed" if report["ok"]
                  else "project.admission.blocked")
        self.db.audit(actor or "runner", action, "project", project_id,
                      {"errors": report["errors"][:20],
                       "warnings": report["warnings"][:20]})
        try:
            admission.require(report)
        except admission.AdmissionError as exc:
            raise PlanError(str(exc)) from exc
        return report

    def _finished(self, project_id: str, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.warning("project runner %s crashed: %r", project_id, exc)
            self.db.audit("runner", "project.runner.error", "project", project_id,
                          {"error": f"{type(exc).__name__}: {exc}"[:300]})

    async def stop(self, project_id: str, actor: str = "") -> dict[str, Any]:
        """Cancel the loop and every job still in flight for this project."""
        task = self._tasks.pop(project_id, None)
        if task and not task.done():
            task.cancel()
        cancelled = []
        for row in self.db.query(
                "SELECT id FROM jobs WHERE project_id=? AND status NOT IN "
                "('done','cancelled')", (project_id,)):
            try:
                await self.orch.cancel_job(row["id"], actor=actor or "runner")
                cancelled.append(row["id"])
            except ValueError:
                continue
        return {"jobs_cancelled": cancelled}

    async def _loop(self, project_id: str, agent_id: str, actor: str) -> None:
        while lifecycle.status_of(self.db, project_id) == lifecycle.RUNNING:
            from .maintenance_mode import enabled as maintenance_enabled
            if maintenance_enabled(self.db):
                # A running project is durable intent, not permission to cross
                # the release fence. Keep its runner alive so leaving
                # maintenance resumes automatically without another UI action.
                await asyncio.sleep(POLL_S)
                continue
            budget, budget_event = sync_pause(
                self.db, project_id, actor=actor or "runner")
            if budget_event and self.bus is not None:
                self.bus.emit(budget_event, project_id, **budget)
            if budget["exceeded"]:
                # Keep durable RUNNING intent and the loop alive.  In-flight
                # jobs reach their own safe boundary; no new task is claimed.
                await asyncio.sleep(POLL_S)
                continue
            plan = lifecycle.task_plan(self.db, project_id)
            tasks = lifecycle.normalize_task_graph(plan["tasks"])
            states = self._task_states(tasks)
            if states and all(state in TERMINAL for state in states.values()):
                self._settle(project_id, actor or "runner")
                return

            max_parallel = self._max_parallel(project_id)
            active = sum(state in ("open", "in_progress") for state in states.values())
            capacity = max(0, max_parallel - active)
            dispatched = 0
            for task in tasks:
                if capacity <= 0:
                    break
                if task.get("job_id"):
                    continue
                dependency_states = [states.get(dep, "missing") for dep in task["needs"]]
                if any(state != "done" for state in dependency_states):
                    continue
                agent = self._agent_for(project_id, task, agent_id)
                if agent is None:
                    self.db.audit(actor or "runner", "project.task.skipped", "project",
                                  project_id, {"task": task.get("title"),
                                               "task_id": task["id"],
                                               "reason": "no agent available"})
                    continue
                try:
                    job_id = self.orch.dispatch(
                        actor=actor or "runner", req=DispatchRequest(
                            project_id=project_id,
                            prompt=task.get("spec") or task["title"],
                            title=task["title"], agent_id=agent, origin="runner",
                            delivery=task.get("delivery"),
                            plan_key=plan["plan_key"], task_id=task["id"]))
                except Exception as exc:
                    from .maintenance_mode import MaintenanceModeError
                    if isinstance(exc, MaintenanceModeError):
                        break
                    if isinstance(exc, ProjectBudgetExceeded):
                        break
                    raise
                for dependency in task["needs"]:
                    dependency_job = next((candidate.get("job_id") for candidate in tasks
                                           if candidate["id"] == dependency), None)
                    if dependency_job:
                        self.db.write(
                            "INSERT OR IGNORE INTO job_deps(job_id, depends_on_job_id, effect) "
                            "VALUES(?,?,'block')", (job_id, dependency_job))
                states[task["id"]] = "open"
                capacity -= 1
                dispatched += 1

            if dispatched:
                await asyncio.sleep(0)
                continue

            # No node was claimable. This is expected while jobs execute or a
            # human gate is blocked. If only cancelled/missing prerequisites
            # remain, stop honestly instead of spinning forever.
            undispatched = [task for task in tasks if not task.get("job_id")]
            permanently_blocked = [task for task in undispatched if any(
                states.get(dep) in ("cancelled", "missing") for dep in task["needs"])]
            ready_without_agent = [task for task in undispatched
                                   if all(states.get(dep) == "done"
                                          for dep in task["needs"])
                                   and self._agent_for(project_id, task, agent_id) is None]
            if ready_without_agent and not any(
                    state in ("open", "in_progress", "blocked")
                    for state in states.values()):
                self._settle(project_id, actor or "runner")
                return
            if undispatched and len(permanently_blocked) == len(undispatched) and not any(
                    state in ("open", "in_progress", "blocked")
                    for state in states.values()):
                self.db.audit(actor or "runner", "project.graph.blocked", "project",
                              project_id, {"tasks": [task["id"]
                                                     for task in permanently_blocked]})
                lifecycle.apply(self.db, project_id, "stop", actor or "runner",
                                {"reason": "task dependencies failed or disappeared",
                                 "tasks": [task["id"]
                                           for task in permanently_blocked]})
                return
            await asyncio.sleep(POLL_S)

    def _max_parallel(self, project_id: str) -> int:
        row = self.db.one("SELECT config_json FROM projects WHERE id=?", (project_id,))
        config = json.loads(row["config_json"] or "{}") if row else {}
        try:
            return max(1, min(32, int(config.get("max_parallel", 1))))
        except (TypeError, ValueError):
            return 1

    def _task_states(self, tasks: list[dict[str, Any]]) -> dict[str, str]:
        states: dict[str, str] = {}
        for task in tasks:
            job_id = task.get("job_id")
            if not job_id:
                states[task["id"]] = "pending"
                continue
            row = self.db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
            states[task["id"]] = row["status"] if row else "missing"
        return states

    def _settle(self, project_id: str, actor: str) -> None:
        """The loop ran out of tasks. Either everything finished (→ maintenance,
        awaiting acceptance) or nothing could be dispatched at all — in which
        case say so and go back to ready instead of sitting in `running` with no
        work and no runner."""
        if lifecycle.maybe_complete(self.db, project_id, actor):
            return
        if lifecycle.status_of(self.db, project_id) != lifecycle.RUNNING:
            return
        progress = lifecycle.job_progress(self.db, project_id)
        if progress["active"] or progress["blocked"]:
            return                      # something is still moving; leave it
        lifecycle.apply(self.db, project_id, "stop", actor,
                        {"reason": "runner had nothing it could dispatch",
                         "jobs": progress})
        self.db.audit(actor, "project.runner.idle", "project", project_id,
                      {"hint": "assign agents to the project's roles, or pick a "
                               "fallback agent when starting"})

    def _agent_for(self, project_id: str, task: dict, fallback: str) -> str | None:
        """A task may name a role; otherwise use the caller's agent, otherwise
        anyone assigned to this project."""
        role = task.get("role")
        if role:
            row = self.db.one(
                "SELECT agent_id FROM project_agent_roles par "
                "JOIN agents a ON a.id = par.agent_id WHERE par.project_id=? AND "
                "par.role=? AND a.enabled=1 AND a.depleted_at IS NULL "
                "ORDER BY par.preference DESC LIMIT 1",
                (project_id, role))
            if row is not None:
                return row["agent_id"]
            return None                    # declared roles never silently degrade
        if fallback:
            row = self.db.one("SELECT id FROM agents WHERE id=? AND enabled=1 "
                              "AND depleted_at IS NULL", (fallback,))
            return row["id"] if row is not None else None
        row = self.db.one(
            "SELECT agent_id FROM project_agent_roles par JOIN agents a "
            "ON a.id = par.agent_id WHERE par.project_id=? AND a.enabled=1 "
            "AND a.depleted_at IS NULL "
            "ORDER BY par.preference DESC LIMIT 1", (project_id,))
        return row["agent_id"] if row is not None else None

    async def _await_job(self, project_id: str, job_id: str) -> str | None:
        """Wait for a job to settle. Returns None when the project was paused or
        stopped meanwhile — a blocked job just means we keep waiting for the
        human at the gate."""
        while True:
            row = self.db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
            if row is None or row["status"] in TERMINAL:
                return row["status"] if row else "cancelled"
            if lifecycle.status_of(self.db, project_id) != lifecycle.RUNNING:
                return None
            await asyncio.sleep(POLL_S)


def reconcile(db, runner: ProjectRunner | None = None,
              actor: str = "server") -> dict[str, list[str]]:
    """Decide what happens to projects that were running when we stopped.

    Parking every one of them was wrong: a project with a confirmed plan and work
    left should simply carry on, which is what the runner exists for. Only a
    project that *cannot* be resumed is parked, and then the audit says why."""
    resumed, parked = [], []
    for row in db.query("SELECT id FROM projects WHERE status=?",
                        (lifecycle.RUNNING,)):
        project_id = row["id"]
        if runner is not None and runner.ensure_running(project_id, actor=actor):
            resumed.append(project_id)
            continue
        plan = lifecycle.task_plan(db, project_id)
        if plan["confirmed"] and runner is not None and \
                runner._work_left(plan["tasks"]):
            continue                     # a loop is already alive for it
        db.write("UPDATE projects SET status=?, updated_at=? WHERE id=?",
                 (lifecycle.PAUSED, now(), project_id))
        db.audit(actor, "project.parked", "project", project_id,
                 {"reason": "restarted with nothing the runner could continue",
                  "confirmed": plan["confirmed"], "tasks": len(plan["tasks"])})
        parked.append(project_id)
    return {"resumed": resumed, "parked": parked}
