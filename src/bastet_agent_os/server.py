"""Control-plane FastAPI app (SPEC §4, §5.9).

Security posture (M1): bind 127.0.0.1 only; every request must pass Host and
Origin validation (DNS-rebinding defence — dispatch is shell execution, so a
rebound browser request would be RCE); /api/* requires the single-user token
via the Authorization header (no cookies, no CSRF surface); the gateway
endpoints authenticate with run tokens instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import secrets_store
from .config import Home
from .db import Db, new_id, now
from .gateway import GatewayContext, build_router
from .governance import QuotaError, Reservations
from .orchestrator import DispatchRequest, Orchestrator
from .pricing import PriceBook

log = logging.getLogger("bastet.server")

# host.docker.internal lets container runs reach the gateway (SPEC §5.4.3);
# it is safe against DNS rebinding — ".internal" is ICANN-reserved, so no
# attacker-controlled public domain can present that Host header.
BASE_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "host.docker.internal"}


def _local_addresses() -> set[str]:
    """This machine's own IPs — legitimate Host headers when binding 0.0.0.0.
    DNS rebinding stays blocked: a rebound request carries the attacker's
    DOMAIN in Host, never our literal IP."""
    import socket

    found: set[str] = set()
    try:
        hostname = socket.gethostname()
        found.add(hostname.lower())
        for info in socket.getaddrinfo(hostname, None):
            found.add(str(info[4][0]).lower())
    except OSError:
        pass
    try:  # primary outbound interface (no packets actually sent)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        found.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    return found


def _build_allowed_hosts(cfg: dict) -> set[str]:
    allowed = set(BASE_ALLOWED_HOSTS)
    allowed.update(h.lower() for h in cfg.get("allowed_hosts", []))
    if cfg.get("host") not in (None, "", "127.0.0.1", "localhost", "::1"):
        allowed.update(_local_addresses())  # LAN mode: our own addresses are valid
    return allowed


def _host_ok(value: str, allowed: set[str]) -> bool:
    host = value.split(":")[0].lower() if not value.startswith("[") else value.rsplit(":", 1)[0].lower()
    return host in allowed


# API models live at module level: `from __future__ import annotations` turns
# annotations into strings, and FastAPI can only resolve them against module
# globals — closure-local models degrade into query parameters.

class ProjectIn(BaseModel):
    id: str
    team_id: str | None = None
    repo_path: str
    config: dict[str, Any] = {}


class AgentIn(BaseModel):
    id: str
    name: str
    executor_type: str = "claude-code"
    amos_agent_id: str | None = None
    account_id: str | None = None
    model: str | None = None       # empty/None = the executor's official default


class AccountIn(BaseModel):
    executor_type: str
    name: str


class AgentUpdateIn(BaseModel):
    id: str | None = None            # operator-facing id; renames every reference atomically
    name: str | None = None
    executor_type: str | None = None
    account_id: str | None = None
    enabled: int | None = None
    model: str | None = None       # "" resets to the official default


class RenameIn(BaseModel):
    name: str


class ResourceIn(BaseModel):
    name: str
    kind: str = "llm"
    endpoint: str | None = None
    api_flavor: str | None = None
    secret_ref: str | None = None     # keyring:/file:/env: or secret:<id> from the pool
    config: dict[str, Any] = {}
    scope_type: str = ""              # global|team|project — creates the grant with it
    scope_id: str = ""


class ResourceUpdateIn(BaseModel):
    name: str | None = None
    # reclassification exists because categories arrive after the resources do:
    # Meshy 3D generation lived under "image" until model3d existed
    kind: str | None = None
    endpoint: str | None = None
    api_flavor: str | None = None
    secret_ref: str | None = None
    config: dict[str, Any] | None = None


class ScopeIn(BaseModel):
    scope_type: str = "project"
    scope_id: str = ""


class AttachResourceIn(BaseModel):
    resource_id: str
    budget_usd: float | None = None
    max_concurrency: int | None = None


class GrantIn(BaseModel):
    resource_id: str
    scope_type: str
    scope_id: str
    budget_usd: float | None = None
    budget_tokens: int | None = None
    period: str = "lifetime"
    max_concurrency: int | None = None
    on_exceed: str = "block"


class ConfigApplyIn(BaseModel):
    actions: list[dict]


class SettingsIn(BaseModel):
    # PEP 563 note: request models must live at module level — FastAPI resolves
    # the (stringified) annotation against module globals, and a class local to
    # create_app silently degrades into a required query parameter
    timezone: str


class SupplyIn(BaseModel):
    name: str
    content: str


class DispatchIn(BaseModel):
    project_id: str
    prompt: str
    title: str = ""
    agent_id: str
    resource_id: str | None = None
    template_id: str | None = None
    timeout_s: int = 3600
    use_worktree: bool = True
    delivery: dict[str, Any] | None = None


class TemplateIn(BaseModel):
    name: str
    stages: list[dict]


class RoleIn(BaseModel):
    project_id: str
    agent_id: str
    role: str
    preference: int = 0


class RetryIn(BaseModel):
    agent_id: str = ""             # blank = the same agent that failed
    spec: str = ""                 # blank = keep the current spec
    refresh_workflow: bool = True  # re-snapshot the project's current template
    # Retrying and reopening bounded recovery budgets are separate decisions.
    renew_recovery_lease: bool = False
    # A ruling normally tells the writer how to fix a rejected result.  Start
    # at that writable rework target instead of re-running the same reviewer.
    restart_from_rework_target: bool = False


class ApproveIn(BaseModel):
    approved: bool
    comment: str = ""
    stage: str = ""  # required only when multiple graph nodes await approval


class UserIn(BaseModel):
    name: str
    role: str = "operator"


class UserEnabledIn(BaseModel):
    enabled: bool


class ChannelIn(BaseModel):
    kind: str = "telegram"
    name: str = ""
    secret_ref: str
    config: dict[str, Any] = {}


class PairIn(BaseModel):
    user_id: str | None = None


class RespondIn(BaseModel):
    request_id: str
    reply: dict[str, Any]


class BindIn(BaseModel):
    project_id: str
    repo_path: str


class LoginStartIn(BaseModel):
    executor_type: str = ""
    account_id: str | None = None


class TeamIn(BaseModel):
    id: str
    name: str = ""


class ProjectTemplateIn(BaseModel):
    template_id: str | None = None   # None/"" clears the assignment


class ProjectUpdateIn(BaseModel):
    repo_path: str | None = None
    description: str | None = None
    delivery_profile: dict[str, Any] | None = None


class DeliveryIn(BaseModel):
    mode: str
    version: str = ""
    version_source: str = "package.json"
    profile: dict[str, Any] | None = None


class RolePromptIn(BaseModel):
    role: str
    label: str = ""
    prompt: str


class SecretIn(BaseModel):
    name: str
    value: str = ""                  # raw value (stored to <home>/secrets) …
    secret_ref: str = ""             # … or an existing ref (keyring:/file:/env:)
    env_name: str = ""               # env var injected into runs
    scope_type: str = "project"      # global|team|project
    scope_id: str = ""
    note: str = ""


class ArchiveIn(BaseModel):
    archived: bool = True


class ChatSessionIn(BaseModel):
    scope_type: str = "project"        # global|team|project
    scope_id: str = ""
    responder_kind: str = "resource"   # agent|resource
    responder_id: str
    title: str = ""


class ChatSessionUpdateIn(BaseModel):
    title: str | None = None
    responder_kind: str | None = None
    responder_id: str | None = None


class ChatMessageIn(BaseModel):
    content: str = ""
    attachment_ids: list[str] = []     # from POST …/files
    reply: bool = True                 # let the responder answer straight away


class RoomMessageIn(BaseModel):
    content: str
    kind: str = "message"             # message|assignment
    author_id: str = ""


class MaintenanceIn(BaseModel):
    reason: str = ""


class HandoffAckIn(BaseModel):
    agent_id: str
    acknowledgement: str = ""
    questions: list[str] = []


class HandoffChallengeIn(BaseModel):
    agent_id: str
    claim: str
    evidence_gap: str = ""
    requested_resolution: str = ""


class HandoffChallengeResponseIn(BaseModel):
    agent_id: str
    content: str
    resolution: str = ""


class ContextEvalIn(BaseModel):
    job_id: str
    stage: str
    role: str | None = None
    expected_buckets: list[str] = []
    expected_terms: list[str] = []
    forbidden_terms: list[str] = []
    budget_tokens: int = 6000


class UserRoleIn(BaseModel):
    role: str                      # viewer|operator|admin


class UserUpdateIn(BaseModel):
    name: str | None = None
    role: str | None = None


class DecomposeIn(BaseModel):
    agent_id: str = ""             # blank = the project's pm-role agent


class TaskPlanIn(BaseModel):
    tasks: list[dict[str, Any]]    # edited plan straight from the UI


class PlanningProposalIn(BaseModel):
    solution: str
    negotiation: list[dict[str, Any]] = []


class PlanningIntakeIn(BaseModel):
    kind: str = "idea"
    content: str


class ProjectRunIn(BaseModel):
    agent_id: str = ""             # fallback executor when a task names no role


class ChannelChatIn(BaseModel):
    """Which agent/LLM answers free-text messages on this channel, for which
    project. Clearing responder_id turns the channel back into notify-only."""
    responder_kind: str = ""      # agent|resource|"" (none)
    responder_id: str = ""
    project_id: str = ""


class ChatDispatchIn(BaseModel):
    agent_id: str
    title: str = ""
    spec: str = ""                     # blank = build it from the conversation
    template_id: str | None = None
    delivery: dict[str, Any] | None = None


class SecretUpdateIn(BaseModel):
    """Everything about a saved credential is editable. The value itself is
    write-only: send a new one to rotate it, leave both blank to keep it."""
    name: str | None = None
    value: str = ""
    secret_ref: str = ""
    env_name: str | None = None
    note: str | None = None
    scope_type: str | None = None
    scope_id: str | None = None


def create_app(home: Home) -> FastAPI:
    from .config import augment_path

    augment_path()  # services start with a minimal PATH; executors need theirs
    home.ensure()
    db = Db(home.db_path)
    prices = PriceBook(home.root / "model_prices.json")
    cfg = home.config()
    gateway_url = f"http://127.0.0.1:{cfg.get('port', 8890)}"
    from .events import EventBus
    from .events import dumps as event_dumps
    bus = EventBus()
    orch = Orchestrator(db, home, prices, gateway_url, bus=bus)
    api_token = home.api_token()

    from .role_prompts import seed as seed_role_prompts
    seed_role_prompts(db)   # built-ins once; user edits persist

    channels: list = []

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        from .channels.telegram import TelegramChannel
        tasks = []
        # automatic continuation must survive a restart: resume the runners of
        # projects that were mid-run, and keep watching job transitions so a loop
        # that dies for any reason is revived by the next settled job
        runner = app.state.project_runner
        outcome = runner_mod.reconcile(db, runner)
        for project_id in outcome["resumed"]:
            log.info("project %s: runner resumed after restart", project_id)
        for project_id in outcome["parked"]:
            log.warning("project %s parked: nothing to continue", project_id)
        tasks.append(asyncio.get_running_loop().create_task(runner.watch(bus)))
        tasks.append(asyncio.get_running_loop().create_task(orch.quota_resume_loop()))
        tasks.append(asyncio.get_running_loop().create_task(
            orch.external_delivery_loop()))
        tasks.append(asyncio.get_running_loop().create_task(orch.supervision_loop()))
        # a job whose driver died with the process is nobody's responsibility
        # otherwise: the runner only resumes projects with undispatched tasks,
        # and retry refuses anything that is not blocked
        jobs = orch.resume_interrupted_jobs()
        for job_id in jobs["resumed"]:
            log.info("job %s: resumed after restart", job_id)
        for job_id in jobs["parked"]:
            log.warning("job %s left blocked: project is not running", job_id)
        for row in db.query("SELECT * FROM channels WHERE enabled=1 AND kind='telegram'"):
            try:
                bot_token = secrets_store.resolve(row["secret_ref"])
            except secrets_store.SecretError as exc:
                log.warning("channel %s: credential error (%s); not started", row["id"], exc)
                continue
            channel = TelegramChannel(db, orch, bus, row["id"], bot_token,
                                      home_root=str(home.root))
            channels.append(channel)
            tasks.append(asyncio.get_running_loop().create_task(channel.run()))
            log.info("telegram channel %s started (long polling)", row["id"])
        yield
        for channel in channels:
            channel.stop()
        for task in tasks:
            task.cancel()
        # Cancelling without awaiting leaves Uvicorn's lifespan waiting on the
        # very background tasks we just asked to stop.  This used to run into
        # systemd's 90 second TimeoutStopSec even with no active jobs.
        await asyncio.gather(*tasks, return_exceptions=True)
        await orch.shutdown()

    allowed_hosts = _build_allowed_hosts(cfg)

    app = FastAPI(title="Bastet Agent OS", version="0.0.1.dev0", docs_url=None,
                  redoc_url=None, lifespan=lifespan)
    app.state.db = db
    app.state.orchestrator = orch
    app.state.channels = channels

    # crash recovery (SPEC §5.1.1): runs left non-terminal by a previous
    # process cannot be re-attached in M1 — mark them orphaned, kill tokens
    stale = db.query("SELECT id FROM runs WHERE status IN ('queued','running','waiting_input')")
    for row in stale:
        db.write("UPDATE runs SET status='orphaned', finished_at=? WHERE id=?",
                 (now(), row["id"]))
        from . import run_tokens as _rt
        _rt.revoke_for_run(db, row["id"])
        db.audit("server", "run.orphaned", "run", row["id"], {"reason": "found at startup"})

    @app.middleware("http")
    async def host_origin_guard(request: Request, call_next):
        if not _host_ok(request.headers.get("host", ""), allowed_hosts):
            return JSONResponse({"error": "bad host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            if not _host_ok(urlparse(origin).netloc, allowed_hosts):
                return JSONResponse({"error": "bad origin"}, status_code=403)
        return await call_next(request)

    from . import users as users_mod
    from .users import Auth

    def get_auth(request: Request) -> Auth:
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        resolved = users_mod.verify(db, token, api_token)
        if resolved is None:
            raise HTTPException(status_code=401, detail="invalid API token")
        return resolved

    def require_role(role: str):
        def dep(auth: Auth = Depends(get_auth)) -> Auth:
            if not auth.at_least(role):
                raise HTTPException(status_code=403, detail=f"requires {role} role")
            return auth
        return dep

    # ---- gateway (run-token auth, mounted at /v1/*) ------------------------

    app.include_router(build_router(GatewayContext(db, prices, Reservations())))

    # ---- AMOS org binding ---------------------------------------------------

    def amos_client():
        try:
            from agent_memory_os.client import MemoryClient
            return MemoryClient()
        except Exception as exc:  # AMOS optional at runtime; org sync degrades
            log.warning("AMOS unavailable (%s); org sync skipped", type(exc).__name__)
            return None

    def _sync_project_membership(project_id: str, agent_id: str) -> bool:
        """Make a role assignment real in AMOS too.

        Bastet's project_agent_roles says WHO plays a role; AMOS project
        membership is what gates that agent's access to the project's memory
        (and what the federation org view counts). Assigning a role without
        this leaves the agent outside the project's memory scope.
        AMOS invariant: project members must be team members first."""
        client = amos_client()
        if client is None:
            return False
        project = db.one("SELECT team_id FROM projects WHERE id=?", (project_id,))
        agent = db.one("SELECT amos_agent_id FROM agents WHERE id=?", (agent_id,))
        if project is None or agent is None:
            return False
        try:
            client.register_agent(agent["amos_agent_id"])
            client.add_team_member(project["team_id"], agent["amos_agent_id"])
            client.add_project_member(project_id, agent["amos_agent_id"])
            return True
        except Exception as exc:
            log.warning("AMOS membership sync failed (%s/%s): %s",
                        project_id, agent_id, exc)
            return False

    def _drop_project_membership(project_id: str, agent_id: str) -> None:
        """Called when an agent holds no more roles in the project — its
        project-scoped memory access goes with the last role. Team membership
        is left alone (other projects may rely on it)."""
        client = amos_client()
        agent = db.one("SELECT amos_agent_id FROM agents WHERE id=?", (agent_id,))
        if client is None or agent is None:
            return
        try:
            client.remove_project_member(project_id, agent["amos_agent_id"])
        except Exception as exc:
            log.warning("AMOS membership removal failed: %s", exc)

    def _reconcile_memberships() -> int:
        """Idempotent backfill: every existing role assignment gets its AMOS
        membership. Runs at startup so assignments made before this existed
        (or while AMOS was down) converge."""
        synced = 0
        for row in db.query("SELECT DISTINCT project_id, agent_id FROM "
                            "project_agent_roles"):
            if _sync_project_membership(row["project_id"], row["agent_id"]):
                synced += 1
        return synced

    # ---- endpoints ----------------------------------------------------------

    @app.post("/api/projects")
    def create_project(p: ProjectIn, auth: Auth = Depends(require_role("operator"))):
        from .config import check_repo_path
        try:                          # never store a bare "~/…" or a relative path
            p.repo_path = check_repo_path(p.repo_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        team_id = p.team_id or f"team-{p.id}"
        client = amos_client()
        if client is not None:
            try:
                client.create_team(team_id, name=team_id)
                client.create_project(p.id, team_id, name=p.id)
            except Exception as exc:
                log.warning("AMOS org sync failed: %s", exc)
        ts = now()
        db.write(
            "INSERT INTO projects(id, team_id, repo_path, config_json, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (p.id, team_id, p.repo_path, json.dumps(p.config), ts, ts),
        )
        from . import collaboration
        collaboration.ensure_room(db, p.id)
        db.audit(auth.actor, "project.create", "project", p.id, {"team": team_id})
        return {"id": p.id, "team_id": team_id}

    @app.get("/api/projects/{project_id}/room",
             dependencies=[Depends(require_role("viewer"))])
    def project_room(project_id: str, limit: int = 200):
        if db.one("SELECT id FROM projects WHERE id=?", (project_id,)) is None:
            raise HTTPException(status_code=404, detail="project not found")
        from . import collaboration
        return {"project_id": project_id,
                "members": collaboration.members(db, project_id),
                "messages": collaboration.messages(db, project_id, limit),
                "handoffs": collaboration.project_handoffs(db, project_id, limit)}

    @app.post("/api/projects/{project_id}/handoffs/{handoff_id}/ack")
    def acknowledge_project_handoff(
        project_id: str, handoff_id: str, body: HandoffAckIn,
        auth: Auth = Depends(require_role("operator")),
    ):
        from . import collaboration
        row = db.one("SELECT project_id FROM stage_handoffs WHERE id=?", (handoff_id,))
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="handoff not found")
        try:
            result = collaboration.acknowledge_handoff(
                db, handoff_id, agent_id=body.agent_id,
                acknowledgement=body.acknowledgement, questions=body.questions)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        db.audit(auth.actor, "handoff.acknowledged", "handoff", handoff_id,
                 {"agent_id": body.agent_id, "questions": body.questions})
        bus.emit("handoff.acknowledged", project_id, handoff_id=handoff_id,
                 agent_id=body.agent_id)
        return result

    @app.post("/api/projects/{project_id}/handoffs/{handoff_id}/challenges")
    def open_project_handoff_challenge(
        project_id: str, handoff_id: str, body: HandoffChallengeIn,
        auth: Auth = Depends(require_role("operator")),
    ):
        from . import collaboration
        row = db.one("SELECT project_id FROM stage_handoffs WHERE id=?", (handoff_id,))
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="handoff not found")
        try:
            result = collaboration.open_handoff_challenge(
                db, handoff_id, agent_id=body.agent_id, claim=body.claim,
                evidence_gap=body.evidence_gap,
                requested_resolution=body.requested_resolution)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        db.audit(auth.actor, "handoff.challenge_opened", "handoff_challenge",
                 result["id"], {"handoff_id": handoff_id, "agent_id": body.agent_id})
        bus.emit("handoff.challenge_opened", project_id, challenge_id=result["id"],
                 handoff_id=handoff_id, agent_id=body.agent_id)
        return result

    @app.post("/api/projects/{project_id}/handoff-challenges/{challenge_id}/exchanges")
    def respond_project_handoff_challenge(
        project_id: str, challenge_id: str, body: HandoffChallengeResponseIn,
        auth: Auth = Depends(require_role("operator")),
    ):
        from . import collaboration
        row = db.one("SELECT project_id FROM handoff_challenges WHERE id=?", (challenge_id,))
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="handoff challenge not found")
        try:
            result = collaboration.respond_handoff_challenge(
                db, challenge_id, agent_id=body.agent_id, content=body.content,
                resolution=body.resolution)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        event = ("handoff.challenge_resolved" if result["status"] != "open"
                 else "handoff.challenge_updated")
        db.audit(auth.actor, event, "handoff_challenge", challenge_id,
                 {"agent_id": body.agent_id, "status": result["status"]})
        bus.emit(event, project_id, challenge_id=challenge_id,
                 agent_id=body.agent_id, status=result["status"])
        return result

    @app.get("/api/jobs/{job_id}/handoff-challenges",
             dependencies=[Depends(require_role("viewer"))])
    def list_job_handoff_challenges(job_id: str):
        if db.one("SELECT id FROM jobs WHERE id=?", (job_id,)) is None:
            raise HTTPException(status_code=404, detail="job not found")
        from . import collaboration
        return collaboration.job_handoff_challenges(db, job_id)

    @app.post("/api/projects/{project_id}/room/messages")
    def post_project_room_message(project_id: str, body: RoomMessageIn,
                                  auth: Auth = Depends(require_role("operator"))):
        if db.one("SELECT id FROM projects WHERE id=?", (project_id,)) is None:
            raise HTTPException(status_code=404, detail="project not found")
        if not body.content.strip():
            raise HTTPException(status_code=400, detail="empty message")
        if body.kind not in ("message", "assignment"):
            raise HTTPException(status_code=400, detail="unsupported room message kind")
        from . import collaboration
        author_type = "pm" if body.kind == "assignment" else "user"
        message_id = collaboration.post(
            db, project_id, author_type=author_type,
            author_id=body.author_id or auth.actor, content=body.content,
            kind=body.kind)
        db.audit(auth.actor, f"room.{body.kind}", "project", project_id,
                 {"message_id": message_id, "author": body.author_id or auth.actor})
        bus.emit("room.message", project_id, message_id=message_id, kind=body.kind)
        return {"id": message_id}

    @app.get("/api/projects", dependencies=[Depends(require_role("viewer"))])
    def list_projects(status: str = "", q: str = "", since: str = "", until: str = ""):
        """Projects with their lifecycle state, filterable by status, keyword and
        time window — the project tab groups on these rather than guessing."""
        from . import project_lifecycle as lc
        lc.reconcile_all(db, actor="ui")     # the list must not show a stale light
        rows = []
        for row in db.query("SELECT * FROM projects ORDER BY updated_at DESC"):
            item = dict(row)
            config = json.loads(item.get("config_json") or "{}")
            item["description"] = config.get("description", "")
            item["status"] = item.get("status") or lc.PLANNING
            item["light"] = lc.LIGHTS.get(item["status"], "⚪")
            item["transitions"] = lc.allowed_transitions(item["status"])
            item["progress"] = lc.job_progress(db, item["id"])
            item["task_count"] = len(lc.task_plan(db, item["id"])["tasks"])
            item["running"] = app.state.project_runner.is_active(item["id"])
            if status and item["status"] != status:
                continue
            if q:
                haystack = " ".join([item["id"], item["team_id"] or "",
                                     item["description"],
                                     item["repo_path"] or ""]).lower()
                if q.lower() not in haystack:
                    continue
            if since and (item["updated_at"] or "") < since:
                continue
            if until and (item["created_at"] or "") > until:
                continue
            rows.append(item)
        return rows

    @app.post("/api/teams")
    def create_team(t: TeamIn, auth: Auth = Depends(require_role("operator"))):
        client = amos_client()
        if client is None:
            raise HTTPException(status_code=502, detail="AMOS unavailable")
        client.create_team(t.id, name=t.name or t.id)
        db.audit(auth.actor, "team.create", "team", t.id, {"name": t.name})
        return {"id": t.id}

    # ---- federation org view (M5) ---------------------------------------------
    # AMOS federation converges teams/projects/members across nodes; Bastet
    # surfaces that shared org and lets an operator BIND a synced project to a
    # local repo. Bastet-local state (resources/grants/jobs) stays per-node.

    @app.get("/api/org", dependencies=[Depends(require_role("viewer"))])
    def org_view():
        local = {r["id"] for r in db.query("SELECT id FROM projects")}
        client = amos_client()
        if client is None:
            return {"amos": False, "teams": [],
                    "local_only": sorted(local)}
        try:
            teams = client.list_teams()
            projects = {p["id"]: p for p in client.list_projects()}
        except Exception as exc:
            log.warning("AMOS org read failed: %s", exc)
            return {"amos": False, "teams": [], "local_only": sorted(local)}
        view = []
        for team in teams:
            view.append({
                "id": team["id"], "name": team.get("name") or team["id"],
                "members": team.get("members") or [],
                "projects": [{
                    "id": pid,
                    "members": (projects.get(pid) or {}).get("members") or [],
                    "bound": pid in local,
                } for pid in (team.get("projects") or [])],
            })
        amos_ids = set(projects)
        return {"amos": True, "teams": view,
                "local_only": sorted(local - amos_ids)}

    @app.post("/api/org/bind")
    def bind_project(b: BindIn, auth: Auth = Depends(require_role("operator"))):
        """Attach a federation-synced AMOS project to a local repo."""
        from .config import check_repo_path
        client = amos_client()
        if client is None:
            raise HTTPException(status_code=502, detail="AMOS unavailable")
        amos_project = next((p for p in client.list_projects()
                             if p["id"] == b.project_id), None)
        if amos_project is None:
            raise HTTPException(status_code=404, detail="AMOS project not found")
        if db.one("SELECT id FROM projects WHERE id=?", (b.project_id,)) is not None:
            raise HTTPException(status_code=409, detail="project already bound")
        ts = now()
        db.write(
            "INSERT INTO projects(id, team_id, repo_path, config_json, created_at, "
            "updated_at) VALUES(?,?,?,?,?,?)",
            (b.project_id, amos_project["team_id"],
             check_repo_path(b.repo_path), "{}", ts, ts))
        from . import collaboration
        collaboration.ensure_room(db, b.project_id)
        db.audit(auth.actor, "project.bind", "project", b.project_id,
                 {"team": amos_project["team_id"], "repo": b.repo_path})
        return {"id": b.project_id, "team_id": amos_project["team_id"]}

    @app.post("/api/agents")
    def create_agent(a: AgentIn, auth: Auth = Depends(require_role("operator"))):
        ts = now()
        amos_id = a.amos_agent_id or a.id
        client = amos_client()
        if client is not None:
            try:
                client.register_agent(amos_id)
            except Exception as exc:
                log.warning("AMOS agent register failed: %s", exc)
        config = {"model": a.model} if a.model else {}
        db.write(
            "INSERT INTO agents(id, amos_agent_id, name, executor_type, account_id, "
            "config_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (a.id, amos_id, a.name, a.executor_type, a.account_id,
             json.dumps(config), ts, ts),
        )
        db.audit(auth.actor, "agent.create", "agent", a.id, {"executor": a.executor_type})
        return {"id": a.id}

    @app.get("/api/agents", dependencies=[Depends(require_role("viewer"))])
    def list_agents():
        return [dict(r) for r in db.query("SELECT * FROM agents ORDER BY created_at")]

    @app.post("/api/agents/{agent_id}/undeplete")
    def undeplete_agent(agent_id: str,
                        auth: Auth = Depends(require_role("operator"))):
        """The human says the balance is topped up.

        Only a human can clear this: the engine took the agent out of rotation
        because a vendor said "payment required", and nothing the engine can do
        pays that bill."""
        if db.one("SELECT id FROM agents WHERE id=?", (agent_id,)) is None:
            raise HTTPException(status_code=404, detail="agent not found")
        cleared = orch.clear_depleted(agent_id, user=auth.actor)
        return {"agent_id": agent_id, "cleared": cleared}

    @app.put("/api/agents/{agent_id}")
    def update_agent(agent_id: str, a: AgentUpdateIn,
                     auth: Auth = Depends(require_role("operator"))):
        row = db.one("SELECT * FROM agents WHERE id=?", (agent_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="agent not found")
        fields = {k: v for k, v in a.model_dump().items() if v is not None}
        requested_id = str(fields.pop("id", agent_id)).strip()
        if not requested_id:
            raise HTTPException(status_code=400, detail="agent id cannot be empty")
        if requested_id != agent_id:
            try:
                db.rename_agent(agent_id, requested_id)
            except ValueError as exc:
                detail = str(exc)
                status = 409 if "already exists" in detail or "active run" in detail else 404
                raise HTTPException(status_code=status, detail=detail) from exc
            db.audit(auth.actor, "agent.rename", "agent", requested_id,
                     {"old_id": agent_id, "new_id": requested_id})
            agent_id = requested_id
            row = db.one("SELECT * FROM agents WHERE id=?", (agent_id,))
        if fields.get("account_id") == "":
            fields["account_id"] = None  # "" clears the binding (global login)
        if "model" in fields:            # model lives inside config_json
            config = json.loads(row["config_json"] or "{}")
            if fields.pop("model"):
                config["model"] = a.model
            else:
                config.pop("model", None)  # "" resets to official default
            fields["config_json"] = json.dumps(config)
        if not fields:
            return dict(row)
        sets = ", ".join(f"{k}=?" for k in fields)
        db.write(f"UPDATE agents SET {sets}, updated_at=? WHERE id=?",
                 (*fields.values(), now(), agent_id))
        db.audit(auth.actor, "agent.update", "agent", agent_id, fields)
        return dict(db.one("SELECT * FROM agents WHERE id=?", (agent_id,)))

    @app.delete("/api/agents/{agent_id}")
    def delete_agent(agent_id: str, auth: Auth = Depends(require_role("operator"))):
        runs = db.one("SELECT COUNT(*) AS n FROM runs WHERE agent_id=?", (agent_id,))
        if runs["n"] > 0:
            raise HTTPException(
                status_code=409,
                detail=f"agent has {runs['n']} runs of history — disable it instead "
                       "(PUT enabled=0) to keep the audit trail intact")
        db.write("DELETE FROM project_agent_roles WHERE agent_id=?", (agent_id,))
        cur = db.write("DELETE FROM agents WHERE id=?", (agent_id,))
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail="agent not found")
        db.audit(auth.actor, "agent.delete", "agent", agent_id, {})
        return {"deleted": agent_id}

    # ---- executors & accounts (multi-login per executor) -----------------------

    from .executors import accounts as accounts_mod

    @app.get("/api/executors", dependencies=[Depends(require_role("viewer"))])
    def list_executors():
        return accounts_mod.catalog_with_availability()

    @app.get("/api/executor-accounts", dependencies=[Depends(require_role("viewer"))])
    def list_executor_accounts():
        rows = []
        for r in db.query("SELECT * FROM executor_accounts ORDER BY created_at"):
            row = dict(r)
            row["status"] = accounts_mod.profile_status(r["executor_type"], r["home_dir"])
            row["login_instruction"] = accounts_mod.login_instruction(
                r["executor_type"], r["home_dir"])
            # Bastet-side usage attributed through the agents bound to this account
            for key, since in (("usage_today", "start of day"),
                               ("usage_7d", "-7 days")):
                agg = db.one(
                    "SELECT COUNT(*) runs, COALESCE(SUM(r.tokens_in+r.cache_read),0) tin, "
                    "COALESCE(SUM(r.tokens_out),0) tout, COALESCE(SUM(r.cost_usd),0) cost "
                    "FROM runs r JOIN agents a ON a.id = r.agent_id "
                    "WHERE a.account_id=? AND r.started_at >= datetime('now', ?)",
                    (r["id"], since))
                row[key] = {"runs": agg["runs"], "tokens_in": agg["tin"],
                            "tokens_out": agg["tout"],
                            "cost_usd": round(float(agg["cost"]), 4)}
            rows.append(row)
        return rows

    @app.post("/api/executor-accounts")
    def create_executor_account(a: AccountIn,
                                auth: Auth = Depends(require_role("operator"))):
        if a.executor_type not in accounts_mod.HOME_ENV:
            raise HTTPException(
                status_code=400,
                detail=f"{a.executor_type} does not support per-account profiles "
                       "(global login or resource-based)")
        account_id = new_id("acct")
        home_dir = accounts_mod.ensure_profile_dir(home.root, account_id)
        db.write("INSERT INTO executor_accounts(id, executor_type, name, home_dir, "
                 "created_at) VALUES(?,?,?,?,?)",
                 (account_id, a.executor_type, a.name, home_dir, now()))
        db.audit(auth.actor, "account.create", "account", account_id,
                 {"executor": a.executor_type, "name": a.name})
        return {"id": account_id, "home_dir": home_dir,
                "login_instruction": accounts_mod.login_instruction(a.executor_type,
                                                                    home_dir),
                "note": "在你自己的終端執行上面的指令完成登入（OAuth 需要瀏覽器）"}

    @app.get("/api/executor-accounts/{account_id}/quota",
             dependencies=[Depends(require_role("viewer"))])
    def account_quota(account_id: str):
        from .executors.quota import fetch_quota
        row = db.one("SELECT * FROM executor_accounts WHERE id=?", (account_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="account not found")
        return fetch_quota(row["executor_type"], row["home_dir"])

    @app.get("/api/executors/{kind}/quota",
             dependencies=[Depends(require_role("viewer"))])
    def global_quota(kind: str):
        """Quota for the executor's GLOBAL (default-profile) login."""
        from .executors.quota import fetch_quota
        return fetch_quota(kind, None)

    @app.put("/api/executor-accounts/{account_id}")
    def rename_executor_account(account_id: str, r: RenameIn,
                                auth: Auth = Depends(require_role("operator"))):
        cur = db.write("UPDATE executor_accounts SET name=? WHERE id=?",
                       (r.name, account_id))
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail="account not found")
        db.audit(auth.actor, "account.rename", "account", account_id, {"name": r.name})
        return {"id": account_id, "name": r.name}

    @app.delete("/api/executor-accounts/{account_id}")
    def delete_executor_account(account_id: str,
                                auth: Auth = Depends(require_role("operator"))):
        used_by = [r["id"] for r in db.query(
            "SELECT id FROM agents WHERE account_id=?", (account_id,))]
        if used_by:
            raise HTTPException(status_code=409,
                                detail=f"account is bound to agents: {used_by} — "
                                       "unbind or delete them first")
        row = db.one("SELECT * FROM executor_accounts WHERE id=?", (account_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="account not found")
        db.write("DELETE FROM executor_accounts WHERE id=?", (account_id,))
        db.audit(auth.actor, "account.delete", "account", account_id, {})
        return {"deleted": account_id,
                "note": f"profile 目錄保留（可能含登入憑證）：{row['home_dir']} — "
                        "確認不需要後自行刪除"}

    # ---- role definition prompts --------------------------------------------------

    @app.get("/api/role-prompts", dependencies=[Depends(require_role("viewer"))])
    def list_role_prompts():
        rows = []
        for r in db.query("SELECT * FROM role_prompts ORDER BY builtin DESC, role"):
            row = dict(r)
            used_by_templates = [t["id"] for t in db.query(
                "SELECT id FROM workflow_templates WHERE stages_json LIKE ?",
                (f'%"role": "{r["role"]}"%',))]
            used_by_projects = [p["project_id"] for p in db.query(
                "SELECT DISTINCT project_id FROM project_agent_roles WHERE role=?",
                (r["role"],))]
            row["used_by"] = {"templates": used_by_templates,
                              "projects": used_by_projects}
            row["in_use"] = bool(used_by_templates or used_by_projects)
            rows.append(row)
        return rows

    @app.post("/api/role-prompts")
    def upsert_role_prompt(r: RolePromptIn,
                           auth: Auth = Depends(require_role("operator"))):
        existing = db.one("SELECT * FROM role_prompts WHERE role=?", (r.role,))
        label = r.label or (existing["label"] if existing else r.role)
        db.write("INSERT INTO role_prompts(role, label, prompt, builtin, updated_at) "
                 "VALUES(?,?,?,0,?) ON CONFLICT(role) DO UPDATE SET "
                 "label=excluded.label, prompt=excluded.prompt, "
                 "updated_at=excluded.updated_at",
                 (r.role, label, r.prompt, now()))
        db.audit(auth.actor, "role_prompt.upsert", "role", r.role, {"label": label})
        return {"role": r.role, "label": label}

    @app.delete("/api/role-prompts/{role}")
    def delete_role_prompt(role: str, auth: Auth = Depends(require_role("operator"))):
        used = db.one("SELECT COUNT(*) AS n FROM project_agent_roles WHERE role=?",
                      (role,))["n"]
        in_templates = db.query("SELECT id FROM workflow_templates WHERE stages_json LIKE ?",
                                (f'%"role": "{role}"%',))
        if used or in_templates:
            raise HTTPException(status_code=409,
                                detail="此角色仍被範本或專案指派使用中，無法刪除")
        cur = db.write("DELETE FROM role_prompts WHERE role=?", (role,))
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail="role not found")
        db.audit(auth.actor, "role_prompt.delete", "role", role, {})
        return {"deleted": role}

    # ---- credentials (resources of kind=secret, scoped by grants) -------------------
    # Same tables as the resource pool — the 資源/管理/專案 views are three lenses
    # on one dataset, never separate stores.

    def _secret_rows(scope_filter: tuple[str, str] | None = None) -> list[dict]:
        rows = []
        for r in db.query("SELECT * FROM resources WHERE kind='secret' ORDER BY name"):
            config = json.loads(r["config_json"] or "{}")
            scopes = [{"scope_type": g["scope_type"], "scope_id": g["scope_id"]}
                      for g in db.query("SELECT scope_type, scope_id FROM grants "
                                        "WHERE resource_id=? AND enabled=1", (r["id"],))]
            if scope_filter:
                kind, ident = scope_filter
                visible = any(sc["scope_type"] == "global"
                              or (sc["scope_type"] == kind and sc["scope_id"] == ident)
                              for sc in scopes)
                if not visible:
                    continue
            rows.append({"id": r["id"], "name": r["name"], "enabled": r["enabled"],
                         "secret_scheme": (r["secret_ref"] or "").split(":", 1)[0],
                         "env_name": config.get("env_name"),
                         "note": config.get("note", ""), "scopes": scopes})
        return rows

    @app.get("/api/secrets", dependencies=[Depends(require_role("admin"))])
    def list_secrets():
        return _secret_rows()

    @app.post("/api/secrets")
    def create_secret(sec: SecretIn, auth: Auth = Depends(require_role("admin"))):
        if not (sec.value or sec.secret_ref):
            raise HTTPException(status_code=400, detail="需要憑證內容或既有 ref")
        if sec.scope_type not in ("global", "team", "project"):
            raise HTTPException(status_code=400, detail="scope 必須是 global/team/project")
        if sec.scope_type != "global" and not sec.scope_id:
            raise HTTPException(status_code=400, detail="team/project 範圍需要指定 id")
        rid = new_id("sec")
        ref = secrets_store.ensure_ref(sec.secret_ref or sec.value, home.root, rid)
        env_name = sec.env_name or "BASTET_" + "".join(
            ch.upper() if ch.isalnum() else "_" for ch in sec.name)[:48]
        ts = now()
        db.write("INSERT INTO resources(id, kind, name, secret_ref, config_json, "
                 "created_at, updated_at) VALUES(?,'secret',?,?,?,?,?)",
                 (rid, sec.name, ref,
                  json.dumps({"env_name": env_name, "note": sec.note}), ts, ts))
        db.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, created_at) "
                 "VALUES(?,?,?,?,?)",
                 (new_id("grt"), rid, sec.scope_type, sec.scope_id or "*", ts))
        db.audit(auth.actor, "secret.create", "resource", rid,
                 {"name": sec.name, "scope": f"{sec.scope_type}:{sec.scope_id or '*'}",
                  "env_name": env_name, "ref_scheme": ref.split(":", 1)[0]})
        return {"id": rid, "env_name": env_name,
                "note": "run 啟動時會以此環境變數注入可見範圍內的任務"}

    @app.put("/api/secrets/{secret_id}")
    def update_secret(secret_id: str, sec: SecretUpdateIn,
                      auth: Auth = Depends(require_role("admin"))):
        row = db.one("SELECT * FROM resources WHERE id=? AND kind='secret'", (secret_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="secret not found")
        config = json.loads(row["config_json"] or "{}")
        if sec.env_name is not None:
            config["env_name"] = sec.env_name or config.get("env_name")
        if sec.note is not None:
            config["note"] = sec.note
        secret_ref = row["secret_ref"]
        rotated = False
        if sec.value or sec.secret_ref:
            # rotation: a fresh file/ref replaces the old one (the previous file
            # stays on disk — deleting a key we might still need is worse)
            secret_ref = secrets_store.ensure_ref(sec.secret_ref or sec.value,
                                                  home.root, secret_id)
            rotated = True
        db.write("UPDATE resources SET name=?, secret_ref=?, config_json=?, updated_at=? "
                 "WHERE id=?", (sec.name or row["name"], secret_ref,
                                json.dumps(config), now(), secret_id))
        if sec.scope_type:
            if sec.scope_type not in ("global", "team", "project"):
                raise HTTPException(status_code=400,
                                    detail="scope 必須是 global/team/project")
            if sec.scope_type != "global" and not sec.scope_id:
                raise HTTPException(status_code=400, detail="team/project 範圍需要指定 id")
            db.write("DELETE FROM grants WHERE resource_id=?", (secret_id,))
            db.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, "
                     "created_at) VALUES(?,?,?,?,?)",
                     (new_id("grt"), secret_id, sec.scope_type,
                      sec.scope_id or "*", now()))
        db.audit(auth.actor, "secret.update", "resource", secret_id,
                 {"name": sec.name or row["name"], "rotated": rotated,
                  "env_name": config.get("env_name"),
                  "scope": f"{sec.scope_type}:{sec.scope_id}" if sec.scope_type else None})
        return next(s for s in _secret_rows() if s["id"] == secret_id)

    @app.delete("/api/secrets/{secret_id}")
    def delete_secret(secret_id: str, auth: Auth = Depends(require_role("admin"))):
        row = db.one("SELECT * FROM resources WHERE id=? AND kind='secret'", (secret_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="secret not found")
        db.write("DELETE FROM grants WHERE resource_id=?", (secret_id,))
        db.write("DELETE FROM resources WHERE id=?", (secret_id,))
        db.audit(auth.actor, "secret.delete", "resource", secret_id, {"name": row["name"]})
        return {"deleted": secret_id,
                "note": "憑證檔案本身保留在 <home>/secrets，確認後可自行刪除"}

    # ---- project overview: one place per project -----------------------------------

    @app.put("/api/projects/{project_id}")
    def update_project(project_id: str, p: ProjectUpdateIn,
                       auth: Auth = Depends(require_role("operator"))):
        row = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        from .config import check_repo_path
        config = json.loads(row["config_json"] or "{}")
        if p.description is not None:
            config["description"] = p.description
        if p.delivery_profile is not None:
            config["delivery_profile"] = p.delivery_profile
        if p.repo_path is not None and p.repo_path.strip():
            try:
                p.repo_path = check_repo_path(p.repo_path)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.write("UPDATE projects SET repo_path=?, config_json=?, updated_at=? WHERE id=?",
                 (p.repo_path if p.repo_path is not None else row["repo_path"],
                  json.dumps(config), now(), project_id))
        db.audit(auth.actor, "project.update", "project", project_id,
                 {"repo_path": p.repo_path})
        return {"id": project_id}

    @app.get("/api/projects/{project_id}/overview",
             dependencies=[Depends(require_role("viewer"))])
    def project_overview(project_id: str):
        project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        config = json.loads(project["config_json"] or "{}")

        stages: list[dict] = []
        template = None
        if project["default_template_id"]:
            template = db.one("SELECT * FROM workflow_templates WHERE id=?",
                              (project["default_template_id"],))
            if template is not None:
                stages = json.loads(template["stages_json"])

        assignments = [dict(r) for r in db.query(
            "SELECT par.role, par.agent_id, par.preference, a.name AS agent_name, "
            "a.executor_type FROM project_agent_roles par "
            "JOIN agents a ON a.id = par.agent_id WHERE par.project_id=? "
            "ORDER BY par.role, par.preference DESC", (project_id,))]
        by_role: dict[str, list[dict]] = {}
        for row in assignments:
            by_role.setdefault(row["role"], []).append(row)
        needed = []
        for stage in stages:
            role = stage.get("role")
            if role:
                needed.append({"stage": stage.get("name"), "role": role,
                               "agents": by_role.get(role, [])})

        from . import admission as admission_mod
        plan_tasks = lifecycle_mod.task_plan(db, project_id)["tasks"]
        admission_report = (admission_mod.project_plan_report(
            db, project_id, plan_tasks, require_default=False)
            if plan_tasks else admission_mod.project_workflow_report(db, project_id))

        # project / team / global grants all make a resource callable in this
        # project; only the project-scoped ones can be detached from here
        resources = [dict(r) for r in db.query(
            "SELECT r.id, r.name, r.kind, g.id AS grant_id, g.scope_type, "
            "g.budget_usd, g.max_concurrency, g.on_exceed "
            "FROM grants g JOIN resources r ON r.id = g.resource_id "
            "WHERE g.enabled=1 AND r.enabled=1 AND r.kind != 'secret' AND "
            "(g.scope_type='global' OR (g.scope_type='project' AND g.scope_id=?) OR "
            " (g.scope_type='team' AND g.scope_id=?)) ORDER BY r.kind, r.name",
            (project_id, project["team_id"]))]

        return {
            "project": {"id": project["id"], "team_id": project["team_id"],
                        "repo_path": project["repo_path"],
                        "description": config.get("description", ""),
                        "delivery_profile": config.get("delivery_profile", {}),
                        "template_id": project["default_template_id"]},
            "stages": stages,
            "role_coverage": needed,
            "admission": admission_report,
            "assignments": assignments,
            "resources": resources,
            "secrets": _secret_rows(("project", project_id))
                       + [s for s in _secret_rows(("team", project["team_id"]))
                          if s["id"] not in {x["id"] for x in
                                             _secret_rows(("project", project_id))}],
            "jobs": [dict(j) for j in db.query(
                "SELECT id, title, stage, status, updated_at FROM jobs "
                "WHERE project_id=? ORDER BY updated_at DESC LIMIT 10", (project_id,))],
        }

    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str, force: bool = False,
                             auth: Auth = Depends(require_role("admin"))):
        """Remove a project and everything scoped to it.

        Trial projects accumulate — a workflow test, a lifecycle probe — and
        until now there was no way to remove one, so the only option was editing
        the database by hand. What goes: the project's jobs (with their runs,
        gates, usage rows and worktrees), its role assignments, its
        project-scoped grants, and its chat sessions. What stays: the audit trail
        (append-only — that this project existed is part of the record) and its
        AMOS memories, which belong to the team and are removed from the memory
        tab.

        Refused unless `force=true` when there is work in flight (deleting rows
        under a running job leaves the runner driving a ghost) or when runs spent
        money (removing usage rows lowers reported spend). Both refusals say
        exactly what is in the way, and forcing records the written-off total."""
        project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        live = [r["id"] for r in db.query(
            "SELECT id FROM jobs WHERE project_id=? AND status IN "
            "('open','in_progress')", (project_id,))]
        spend = db.one(
            "SELECT COUNT(*) AS rows, COALESCE(SUM(cost_usd), 0) AS cost "
            "FROM usage_ledger WHERE run_id IN (SELECT id FROM runs WHERE job_id IN "
            "(SELECT id FROM jobs WHERE project_id=?))", (project_id,))
        if not force:
            if live:
                raise HTTPException(
                    status_code=409,
                    detail=f"這個專案還有 {len(live)} 個進行中的任務"
                           f"（{', '.join(live[:3])}）。先停掉它們，或用 force 一併取消。")
            if spend["rows"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"這個專案有 {spend['rows']} 筆用量紀錄"
                           f"（${spend['cost']:.4f}）。刪除會把這筆帳從報表移除 —— "
                           f"確定要刪就用 force，金額會記進稽核紀錄。")
        for job_id in live:                       # cancel in flight before removing
            try:
                await orch.cancel_job(job_id, actor=auth.actor)
            except Exception as exc:
                log.warning("could not cancel %s before delete: %r", job_id, exc)
        removed = orch.purge_project_jobs(project_id, actor=auth.actor)
        sessions = [r["id"] for r in db.query(
            "SELECT id FROM chat_sessions WHERE scope_type='project' AND scope_id=?",
            (project_id,))]
        for session_id in sessions:
            db.write("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
        db.write("DELETE FROM chat_sessions WHERE scope_type='project' AND scope_id=?",
                 (project_id,))
        removed["sessions"] = len(sessions)
        removed["roles"] = db.write(
            "DELETE FROM project_agent_roles WHERE project_id=?",
            (project_id,)).rowcount
        removed["grants"] = db.write(
            "DELETE FROM grants WHERE scope_type='project' AND scope_id=?",
            (project_id,)).rowcount
        room = db.one("SELECT id FROM project_rooms WHERE project_id=?", (project_id,))
        if room:
            removed["room_messages"] = db.write(
                "DELETE FROM room_messages WHERE room_id=?", (room["id"],)).rowcount
            db.write("DELETE FROM project_rooms WHERE id=?", (room["id"],))
        db.write("DELETE FROM stage_handoffs WHERE project_id=?", (project_id,))
        db.write("DELETE FROM test_evidence WHERE project_id=?", (project_id,))
        db.write("DELETE FROM projects WHERE id=?", (project_id,))
        db.audit(auth.actor, "project.delete", "project", project_id,
                 {"status": project["status"], "repo_path": project["repo_path"],
                  "cancelled": live, "forced": force, **removed})
        bus.emit("project.deleted", project_id=project_id)
        return {"deleted": project_id, "cancelled": live, **removed}

    @app.post("/api/projects/{project_id}/resources")
    def attach_resource(project_id: str, a: AttachResourceIn,
                        auth: Auth = Depends(require_role("operator"))):
        """Make a pool resource callable in this project (a project grant)."""
        if db.one("SELECT id FROM projects WHERE id=?", (project_id,)) is None:
            raise HTTPException(status_code=404, detail="project not found")
        resource = db.one("SELECT * FROM resources WHERE id=? AND kind != 'secret'",
                          (a.resource_id,))
        if resource is None:
            raise HTTPException(status_code=404, detail="resource not found")
        if db.one("SELECT id FROM grants WHERE resource_id=? AND scope_type='project' "
                  "AND scope_id=?", (a.resource_id, project_id)):
            raise HTTPException(status_code=409, detail="此專案已有這個資源")
        gid = new_id("grt")
        db.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, budget_usd, "
                 "max_concurrency, created_at) VALUES(?,?,'project',?,?,?,?)",
                 (gid, a.resource_id, project_id, a.budget_usd, a.max_concurrency, now()))
        db.audit(auth.actor, "grant.create", "grant", gid,
                 {"resource": a.resource_id, "scope": f"project:{project_id}",
                  "kind": resource["kind"]})
        return {"id": gid, "resource": resource["name"]}

    @app.delete("/api/projects/{project_id}/resources/{resource_id}")
    def detach_resource(project_id: str, resource_id: str,
                        auth: Auth = Depends(require_role("operator"))):
        """Remove only this project's own grant — inherited team/global access
        has to be removed where it was granted, and we say so."""
        row = db.one("SELECT * FROM grants WHERE resource_id=? AND scope_type='project' "
                     "AND scope_id=?", (resource_id, project_id))
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="此專案沒有自己的授權（可能是從 team/全域繼承，需在該層移除）")
        db.write("DELETE FROM grants WHERE id=?", (row["id"],))
        db.audit(auth.actor, "grant.delete", "grant", row["id"],
                 {"resource": resource_id, "scope": f"project:{project_id}"})
        return {"deleted": row["id"]}

    # ---- project lifecycle: plan → run → maintain → close (SPEC §5.12) -------

    from . import project_lifecycle as lifecycle_mod
    from . import project_runner as runner_mod

    app.state.project_runner = runner_mod.ProjectRunner(db, orch, bus)
    # the self-configuration skill: the guide file tracks the code, so it is
    # rewritten on every boot; the pool resource is created once
    from . import self_config as self_config_mod
    try:
        self_config_mod.seed_skill(db, home.root)
    except Exception as exc:                     # a broken seed must not stop serve
        log.warning("bastet-config skill seed failed: %r", exc)

    # reconcile/resume happens in the lifespan, where there is a running loop
    for healed in lifecycle_mod.reconcile_all(db):
        log.info("project %s reconciled at startup: %s", healed["project"], healed)

    def _lifecycle_error(exc: Exception) -> HTTPException:
        return HTTPException(status_code=409, detail=str(exc))

    @app.get("/api/projects/{project_id}/lifecycle",
             dependencies=[Depends(require_role("viewer"))])
    def project_lifecycle_state(project_id: str):
        try:
            lifecycle_mod.reconcile(db, project_id, actor="ui")
            state = lifecycle_mod.overview(db, project_id)
        except lifecycle_mod.LifecycleError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        state["running"] = app.state.project_runner.is_active(project_id)
        return state

    @app.post("/api/projects/{project_id}/lifecycle/{transition}")
    async def move_project(project_id: str, transition: str,
                           body: ProjectRunIn | None = None,
                           auth: Auth = Depends(require_role("operator"))):
        """One entry point for 執行 / 暫停 / 停止 / 結案 / 重啟, so the state
        machine (not the UI) decides what is legal."""
        runner = app.state.project_runner
        try:
            if transition == "stop":
                # cancel first: a run left streaming after stop keeps spending
                stopped = await runner.stop(project_id, actor=auth.actor)
                status = lifecycle_mod.apply(db, project_id, "stop", auth.actor, stopped)
                bus.emit("project.status", project_id, status=status,
                         transition="stop")
                return {"status": status, **stopped}
            if transition == "pause":
                status = lifecycle_mod.apply(db, project_id, "pause", auth.actor)
                return {"status": status,
                        "note": "目前任務會跑完，之後不再派下一個"}
            if transition in ("start", "resume"):
                # Admission happens before the state transition. A rejected
                # start must leave READY/PAUSED truthful and resumable.
                runner.admit(project_id, (body.agent_id if body else ""),
                             actor=auth.actor)
                status = lifecycle_mod.apply(db, project_id, transition, auth.actor)
                started = runner.start(project_id, (body.agent_id if body else ""),
                                       actor=auth.actor)
                bus.emit("project.status", project_id, status=status,
                         transition=transition)
                return {"status": status, **started}
            status = lifecycle_mod.apply(db, project_id, transition, auth.actor)
            bus.emit("project.status", project_id, status=status,
                     transition=transition)
            return {"status": status}
        except lifecycle_mod.LifecycleError as exc:
            raise _lifecycle_error(exc) from exc
        except runner_mod.PlanError as exc:
            # A pre-admission failure happens before the transition and must
            # leave READY/PAUSED untouched. Only unwind if start already moved.
            if lifecycle_mod.status_of(db, project_id) == lifecycle_mod.RUNNING:
                lifecycle_mod.apply(db, project_id, "stop", auth.actor,
                                    {"reason": "nothing to run"})
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/decompose")
    async def decompose_project(project_id: str, body: DecomposeIn,
                               auth: Auth = Depends(require_role("operator"))):
        """Removed: decomposition belongs to a proposed planning round."""
        del project_id, body, auth
        raise HTTPException(
            status_code=410,
            detail="請在專案對話完成 PM／系統分析方案後，由該規劃輪次產生任務圖")

    @app.delete("/api/projects/{project_id}/tasks")
    def clear_project_tasks(project_id: str,
                            auth: Auth = Depends(require_role("operator"))):
        """Drop a stale breakdown. Tasks already dispatched are kept — they are
        the link between the plan and the running jobs."""
        if db.one("SELECT id FROM projects WHERE id=?", (project_id,)) is None:
            raise HTTPException(status_code=404, detail="project not found")
        dropped = lifecycle_mod.clear_undispatched(db, project_id, actor=auth.actor)
        return {"dropped": dropped,
                "task_plan": lifecycle_mod.plan_with_jobs(db, project_id)}

    @app.put("/api/projects/{project_id}/tasks")
    def save_project_tasks(project_id: str, body: TaskPlanIn,
                           auth: Auth = Depends(require_role("operator"))):
        """Confirm (and optionally edit) the decomposition — the human gate
        between planning and execution."""
        if db.one("SELECT id FROM projects WHERE id=?", (project_id,)) is None:
            raise HTTPException(status_code=404, detail="project not found")
        from .delivery import normalize as normalize_delivery
        tasks = []
        try:
            for t in body.tasks:
                title = str(t.get("title", "")).strip()
                if not title:
                    continue
                task = {
                    "title": title,
                    "spec": str(t.get("spec", "")).strip(),
                    "role": str(t.get("role", "")).strip(),
                    "delivery": normalize_delivery(
                        t.get("delivery") or {"mode": "integration"}),
                    **({"job_id": t["job_id"]} if t.get("job_id") else {}),
                    **({"origin": t["origin"]} if t.get("origin") else {}),
                }
                if t.get("id"):
                    task["id"] = str(t["id"]).strip()
                if "needs" in t:
                    task["needs"] = t["needs"]
                tasks.append(task)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not tasks:
            raise HTTPException(status_code=400, detail="至少需要一個任務")
        from . import admission as admission_mod
        from . import planning_rounds
        try:
            round_row = planning_rounds.current(db, project_id)
            if round_row is not None and round_row["state"] != "proposed":
                raise planning_rounds.PlanningRoundError(
                    "方案與系統分析結論完成後才能確認任務圖")
            tasks = lifecycle_mod.normalize_task_graph(tasks)
            admission_report = admission_mod.project_plan_report(
                db, project_id, tasks, require_default=False)
            admission_mod.require(admission_report)
            lifecycle_mod.save_task_plan(db, project_id, tasks, by=auth.actor,
                                         confirmed=True)
            if round_row is not None:
                planning_rounds.approve(db, project_id,
                                        lifecycle_mod.task_plan(db, project_id)["tasks"],
                                        actor=auth.actor)
        except (lifecycle_mod.LifecycleError, admission_mod.AdmissionError,
                planning_rounds.PlanningRoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.audit(auth.actor, "project.tasks.confirm", "project", project_id,
                 {"tasks": len(tasks)})
        status = lifecycle_mod.status_of(db, project_id)
        if status == lifecycle_mod.PLANNING:
            status = lifecycle_mod.apply(db, project_id, "confirm_plan", auth.actor,
                                         {"tasks": len(tasks)})
        return {"tasks": tasks, "confirmed": True, "status": status}

    # ---- durable project planning rounds -----------------------------------

    from . import planning_rounds as planning_rounds_mod

    @app.get("/api/projects/{project_id}/planning")
    def planning_overview(project_id: str,
                          auth: Auth = Depends(require_role("viewer"))):
        del auth
        return planning_rounds_mod.overview(db, project_id)

    @app.post("/api/chat/sessions/{session_id}/planning-round")
    def start_planning_round(session_id: str,
                             auth: Auth = Depends(require_role("operator"))):
        try:
            session = chat_mod.get_session(db, session_id)
            if session["scope_type"] != "project":
                raise planning_rounds_mod.PlanningRoundError(
                    "planning rounds require a project session")
            round_id = planning_rounds_mod.start(
                db, session["scope_id"], session_id, actor=auth.actor)
            return {"id": round_id, **planning_rounds_mod.overview(
                db, session["scope_id"])}
        except (chat_mod.ChatError,
                planning_rounds_mod.PlanningRoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/planning-rounds/{round_id}/proposal")
    def propose_planning_round(round_id: str, body: PlanningProposalIn,
                               auth: Auth = Depends(require_role("operator"))):
        try:
            planning_rounds_mod.propose(db, round_id, solution=body.solution,
                                        negotiation=body.negotiation,
                                        actor=auth.actor)
        except planning_rounds_mod.PlanningRoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": round_id, "state": "proposed"}

    @app.post("/api/planning-rounds/{round_id}/negotiate")
    async def negotiate_planning_round(
            round_id: str, auth: Auth = Depends(require_role("operator"))):
        row = db.one("SELECT project_id FROM planning_rounds WHERE id=?", (round_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="planning round not found")
        try:
            result = await planning_rounds_mod.negotiate(
                db, home.root, round_id, actor=auth.actor,
                on_exchange=lambda exchange, verdict: bus.emit(
                    "planning.exchange", row["project_id"], round_id=round_id,
                    exchange=exchange, verdict=verdict))
        except planning_rounds_mod.PlanningRoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        bus.emit("planning.proposed", row["project_id"], round_id=round_id)
        return result

    @app.post("/api/projects/{project_id}/planning-intake")
    def add_planning_intake(project_id: str, body: PlanningIntakeIn,
                            auth: Auth = Depends(require_role("operator"))):
        try:
            item_id = planning_rounds_mod.add_intake(
                db, project_id, kind=body.kind, content=body.content,
                actor=auth.actor)
        except planning_rounds_mod.PlanningRoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": item_id}

    # ---- chat: the human input + authorisation channel (SPEC §5.11) ----------

    from . import chat as chat_mod

    # uploads live here until a message references them
    _pending_files: dict[str, dict[str, Any]] = {}

    def _chat_error(exc: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/chat/responders", dependencies=[Depends(require_role("viewer"))])
    def chat_responders():
        """The dropdown: enabled agents and pool LLM resources."""
        return chat_mod.responders(db)

    @app.get("/api/chat/sessions", dependencies=[Depends(require_role("viewer"))])
    def chat_sessions(scope_type: str = "", scope_id: str = ""):
        return chat_mod.list_sessions(db, scope_type or None, scope_id or None)

    @app.post("/api/chat/sessions")
    def create_chat_session(body: ChatSessionIn,
                            auth: Auth = Depends(require_role("operator"))):
        try:
            session_id = chat_mod.create_session(
                db, scope_type=body.scope_type, scope_id=body.scope_id,
                responder_kind=body.responder_kind, responder_id=body.responder_id,
                title=body.title, actor=auth.actor)
        except chat_mod.ChatError as exc:
            raise _chat_error(exc) from exc
        return {"id": session_id}

    @app.put("/api/chat/sessions/{session_id}")
    def update_chat_session(session_id: str, body: ChatSessionUpdateIn,
                            auth: Auth = Depends(require_role("operator"))):
        try:
            chat_mod.update_session(db, session_id, title=body.title,
                                    responder_kind=body.responder_kind,
                                    responder_id=body.responder_id, actor=auth.actor)
        except chat_mod.ChatError as exc:
            raise _chat_error(exc) from exc
        return {"id": session_id}

    @app.delete("/api/chat/sessions/{session_id}")
    def delete_chat_session(session_id: str,
                            auth: Auth = Depends(require_role("operator"))):
        try:
            chat_mod.delete_session(db, session_id, actor=auth.actor)
        except chat_mod.ChatError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"deleted": session_id}

    @app.get("/api/chat/sessions/{session_id}/messages",
             dependencies=[Depends(require_role("viewer"))])
    def chat_messages(session_id: str, limit: int = 200):
        try:
            session = chat_mod.get_session(db, session_id)
        except chat_mod.ChatError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        pending = []
        if session["scope_type"] == "project":
            # blocked gates are the authorisation the chat exists to collect
            pending = [dict(j) for j in db.query(
                "SELECT id, title, stage FROM jobs WHERE project_id=? AND "
                "status='blocked' ORDER BY updated_at DESC", (session["scope_id"],))]
        planning = None
        if session["scope_type"] == "project":
            planning = planning_rounds_mod.overview(db, session["scope_id"])
        return {"session": dict(session),
                "messages": chat_mod.messages(db, session_id, limit),
                "pending_approvals": pending,
                "planning": planning}

    @app.post("/api/chat/sessions/{session_id}/files")
    async def upload_chat_file(session_id: str, file: UploadFile = File(...),
                               auth: Auth = Depends(require_role("operator"))):
        """Files, docs and screenshots come in here; the next message claims them."""
        try:
            chat_mod.get_session(db, session_id)
        except chat_mod.ChatError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        data = await file.read()
        item = chat_mod.save_attachment(home.root, session_id,
                                        file.filename or "file", data)
        _pending_files[item["id"]] = item
        db.audit(auth.actor, "chat.file.upload", "chat", session_id,
                 {"name": item["name"], "size": item["size"], "mime": item["mime"]})
        return {k: v for k, v in item.items() if k != "path"}

    @app.get("/api/chat/sessions/{session_id}/files/{file_id}",
             dependencies=[Depends(require_role("viewer"))])
    def download_chat_file(session_id: str, file_id: str):
        from fastapi.responses import FileResponse
        for message in chat_mod.messages(db, session_id, limit=1000):
            for item in message["attachments"]:
                if item["id"] == file_id and Path(item["path"]).exists():
                    return FileResponse(item["path"], filename=item["name"],
                                        media_type=item.get("mime"))
        raise HTTPException(status_code=404, detail="attachment not found")

    @app.post("/api/chat/sessions/{session_id}/messages")
    async def post_chat_message(session_id: str, body: ChatMessageIn,
                                auth: Auth = Depends(require_role("operator"))):
        # async def: the responder call must run on the main event loop
        try:
            session = chat_mod.get_session(db, session_id)
            attachments = [_pending_files.pop(fid) for fid in body.attachment_ids
                           if fid in _pending_files]
            if not (body.content.strip() or attachments):
                raise chat_mod.ChatError("empty message")
            chat_mod.add_message(db, session_id, role="user", content=body.content,
                                 author=auth.actor, attachments=attachments)
            chat_mod.remember(db, session, "user", body.content)
            answer = None
            if body.reply:
                answer = await chat_mod.reply(db, home.root, session_id,
                                              actor=auth.actor)
        except chat_mod.ChatError as exc:
            raise _chat_error(exc) from exc
        bus.emit("chat.message", session["scope_id"], session_id=session_id)
        return {"reply": answer,
                "messages": chat_mod.messages(db, session_id, limit=200)}

    @app.post("/api/chat/sessions/{session_id}/decompose")
    async def decompose_from_chat(session_id: str, body: DecomposeIn,
                                 auth: Auth = Depends(require_role("operator"))):
        """Turn the conversation into the project's task breakdown, from inside
        the chat — this is where planning actually finishes, so this is where the
        task cards should come from."""
        try:
            session = chat_mod.get_session(db, session_id)
        except chat_mod.ChatError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if session["scope_type"] != "project":
            raise HTTPException(status_code=400,
                                detail="只有專案範圍的對話可以產生任務拆分")
        round_row = planning_rounds_mod.current(db, session["scope_id"])
        if round_row is None or round_row["id"] != session["planning_round_id"] or \
                round_row["state"] != "proposed":
            raise HTTPException(
                status_code=400,
                detail="PM 與系統分析完成具體方案並接受後才能產生任務圖")
        from . import admission as admission_mod
        try:
            admission_mod.require(admission_mod.project_workflow_report(
                db, session["scope_id"]))
        except admission_mod.AdmissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            tasks = await runner_mod.decompose(db, home.root, session["scope_id"],
                                              body.agent_id, actor=auth.actor)
        except runner_mod.PlanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        chat_mod.add_message(db, session_id, role="system", author=auth.actor,
                             content=f"🧩 已從這段對話產生 {len(tasks)} 項任務拆分"
                                     f"（尚待人工確認）",
                             meta={"tasks": len(tasks)})
        bus.emit("project.status", session["scope_id"], status="planned")
        return {"tasks": tasks, "confirmed": False,
                "project_id": session["scope_id"]}

    @app.post("/api/chat/sessions/{session_id}/dispatch")
    async def dispatch_from_chat(session_id: str, body: ChatDispatchIn,
                                 auth: Auth = Depends(require_role("operator"))):
        """Removed: one conversation must become a reviewed task graph, not one card."""
        del session_id, body, auth
        raise HTTPException(
            status_code=410,
            detail="整段對話單卡派工已取消；請完成規劃輪次並確認任務圖")

    # ---- WebUI login wizard (PTY over WS) -----------------------------------------

    from .login_sessions import LoginSessionManager

    login_manager = LoginSessionManager()

    @app.post("/api/login-sessions")
    async def start_login_session(req: LoginStartIn,
                                  auth: Auth = Depends(require_role("operator"))):
        # async def: the PTY reader registers on the main event loop
        home_dir = None
        if req.account_id:
            account = db.one("SELECT * FROM executor_accounts WHERE id=?",
                             (req.account_id,))
            if account is None:
                raise HTTPException(status_code=404, detail="account not found")
            home_dir = account["home_dir"]
            kind = account["executor_type"]
        else:
            kind = req.executor_type
        command = accounts_mod.login_command(kind, home_dir)
        if command is None:
            raise HTTPException(status_code=400,
                                detail="此 executor 不需要登入（憑證來自資源池）")
        env, argv = command
        try:
            session = login_manager.start(
                kind, env, argv,
                strip_alt_screen=kind in accounts_mod.STRIP_ALT_SCREEN)
        except (RuntimeError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.audit(auth.actor, "login_session.start", "executor", kind,
                 {"account": req.account_id, "argv": argv[0]})
        return {"id": session.id, "command": " ".join(argv)}

    @app.delete("/api/login-sessions/{session_id}")
    def kill_login_session(session_id: str,
                           auth: Auth = Depends(require_role("operator"))):
        login_manager.kill(session_id)
        return {"killed": session_id}

    # ---- AMOS memory view -------------------------------------------------------

    def _memory_row(item) -> dict:
        """One shape for the WebUI out of whatever AMOS hands back.

        `search` returns SearchResult(record, score, reason) and `list_recent`
        returns MemoryRecord; older builds returned plain dicts. Reading them
        with `.get` worked only for the dict case and raised on the rest, so
        normalise here instead of at each call site."""
        score = getattr(item, "score", None)
        record = getattr(item, "record", item)
        get = (record.get if isinstance(record, dict)
               else lambda key, default=None: getattr(record, key, default))
        return {"id": get("id"), "score": score,
                "content": get("summary") or get("content"),
                "scope": get("scope"), "type": get("type"),
                "owner": get("owner"), "tags": get("tags") or [],
                # the project/team id lives in the visibility grants, not in a
                # column — that grant is also what gates who can recall it
                "visibility": get("visibility") or [],
                "created_at": get("created_at") or get("at")}

    @app.get("/api/memory/search", dependencies=[Depends(require_role("viewer"))])
    def memory_search(q: str, limit: int = 20):
        client = amos_client()
        if client is None:
            raise HTTPException(status_code=502, detail="AMOS unavailable")
        try:
            hits = client.search(q, limit=min(limit, 50))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"AMOS search failed: "
                                f"{type(exc).__name__}") from exc
        return [_memory_row(h) for h in hits]

    # ---- resource pool: classified kinds, scoped visibility, installers ------

    @app.get("/api/resource-kinds", dependencies=[Depends(require_role("viewer"))])
    def resource_kinds():
        from . import resource_kinds as rk
        return rk.catalog()

    def _scope_rows(resource_id: str) -> list[dict]:
        return [{"grant_id": g["id"], "scope_type": g["scope_type"],
                 "scope_id": g["scope_id"]}
                for g in db.query("SELECT id, scope_type, scope_id FROM grants "
                                  "WHERE resource_id=? AND enabled=1", (resource_id,))]

    def _resource_row(row) -> dict:
        """Public shape of a pool resource: config visible, secret never."""
        from . import resource_install, resource_test
        from . import resource_kinds as rk
        config = json.loads(row["config_json"] or "{}")
        ref = row["secret_ref"] or ""
        credential = None
        if ref.startswith("secret:"):
            saved = db.one("SELECT name FROM resources WHERE id=?", (ref.split(":", 1)[1],))
            credential = saved["name"] if saved else "(deleted)"
        return {"id": row["id"], "kind": row["kind"], "name": row["name"],
                "endpoint": row["endpoint"], "api_flavor": row["api_flavor"],
                "enabled": row["enabled"],
                "secret_ref": (ref.split(":", 1)[0] + ":…") if ref else "",
                "credential_name": credential,
                "config": {k: v for k, v in config.items()
                           if k in rk.CONFIG_FIELDS or k == "note"},
                "install": resource_install.state_of(config),
                "test": resource_test.state_of(config),
                "scopes": _scope_rows(row["id"]),
                "problems": rk.validate(row["kind"], row["endpoint"], ref, config)}

    def _check_scope(scope_type: str, scope_id: str) -> None:
        if scope_type not in ("global", "team", "project"):
            raise HTTPException(status_code=400,
                                detail="scope 必須是 global/team/project")
        if scope_type != "global" and not scope_id:
            raise HTTPException(status_code=400, detail="team/project 範圍需要指定 id")

    @app.post("/api/resources")
    def create_resource(r: ResourceIn, auth: Auth = Depends(require_role("admin"))):
        from . import resource_kinds as rk
        try:
            secrets_store.reject_secrets_in_config(r.config)
        except secrets_store.SecretError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if r.kind not in rk.BY_ID:
            raise HTTPException(status_code=400, detail=f"unknown kind {r.kind}")
        if r.scope_type:
            _check_scope(r.scope_type, r.scope_id)
        config = {k: v for k, v in r.config.items()
                  if k in rk.CONFIG_FIELDS or k == "note"}
        rid = new_id("res")
        ts = now()
        # secret:<id> points at a saved credential; raw values get filed safely
        secret_ref = secrets_store.ensure_ref(r.secret_ref or "", home.root, rid) or None
        problems = rk.validate(r.kind, r.endpoint, secret_ref, config)
        db.write(
            "INSERT INTO resources(id, kind, name, endpoint, api_flavor, secret_ref, "
            "config_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, r.kind, r.name, r.endpoint, r.api_flavor, secret_ref,
             json.dumps(config), ts, ts),
        )
        if r.scope_type:
            db.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, "
                     "created_at) VALUES(?,?,?,?,?)",
                     (new_id("grt"), rid, r.scope_type, r.scope_id or "*", ts))
        db.audit(auth.actor, "resource.create", "resource", rid,
                 {"kind": r.kind, "name": r.name,
                  "scope": f"{r.scope_type}:{r.scope_id}" if r.scope_type else None,
                  "secret_scheme": (r.secret_ref or "").split(":", 1)[0],
                  "problems": problems})
        return {"id": rid, "problems": problems}

    @app.put("/api/resources/{resource_id}")
    def update_resource(resource_id: str, r: ResourceUpdateIn,
                        auth: Auth = Depends(require_role("admin"))):
        from . import resource_kinds as rk
        row = db.one("SELECT * FROM resources WHERE id=?", (resource_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="resource not found")
        config = json.loads(row["config_json"] or "{}")
        old_config = dict(config)
        if r.config is not None:
            try:
                secrets_store.reject_secrets_in_config(r.config)
            except secrets_store.SecretError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            config.update({k: v for k, v in r.config.items()
                           if k in rk.CONFIG_FIELDS or k == "note"})
        secret_ref = row["secret_ref"]
        if r.secret_ref is not None:
            secret_ref = (secrets_store.ensure_ref(r.secret_ref, home.root, resource_id)
                          or None)
        kind = row["kind"]
        if r.kind is not None and r.kind != kind:
            if r.kind not in rk.BY_ID:
                raise HTTPException(status_code=400,
                                    detail=f"unknown kind {r.kind!r}")
            # the new classification must still be a usable resource: kind
            # decides which fields are load-bearing (an llm without an endpoint
            # is broken, a skill without one is fine)
            problems = rk.validate(r.kind,
                                   r.endpoint if r.endpoint is not None
                                   else row["endpoint"],
                                   secret_ref, config)
            if problems:
                raise HTTPException(
                    status_code=400,
                    detail=f"cannot reclassify to {r.kind}: {', '.join(problems)}")
            kind = r.kind
        # An install/health receipt proves one exact Skill contract. Editing
        # any load-bearing field invalidates it; otherwise changing target or
        # digest after a green check could bypass admission.
        skill_contract_fields = {
            "skill_id", "skill_version", "skill_source", "skill_target",
            "skill_digest", "compatible_executors", "install_command",
            "health_command",
        }
        if kind == "skill" and any(
                old_config.get(key) != config.get(key)
                for key in skill_contract_fields):
            config.pop("install", None)
            config.pop("test", None)
        db.write("UPDATE resources SET name=?, kind=?, endpoint=?, api_flavor=?, "
                 "secret_ref=?, config_json=?, updated_at=? WHERE id=?",
                 (r.name or row["name"], kind,
                  r.endpoint if r.endpoint is not None else row["endpoint"],
                  r.api_flavor if r.api_flavor is not None else row["api_flavor"],
                  secret_ref, json.dumps(config), now(), resource_id))
        db.audit(auth.actor, "resource.update", "resource", resource_id,
                 {"fields": [k for k, v in r.model_dump().items() if v is not None]})
        return _resource_row(db.one("SELECT * FROM resources WHERE id=?", (resource_id,)))

    @app.delete("/api/resources/{resource_id}")
    def delete_resource(resource_id: str, auth: Auth = Depends(require_role("admin"))):
        row = db.one("SELECT * FROM resources WHERE id=?", (resource_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="resource not found")
        used = db.one("SELECT COUNT(*) AS n FROM jobs WHERE resource_id=?", (resource_id,))
        db.write("DELETE FROM grants WHERE resource_id=?", (resource_id,))
        db.write("DELETE FROM resources WHERE id=?", (resource_id,))
        db.audit(auth.actor, "resource.delete", "resource", resource_id,
                 {"name": row["name"], "kind": row["kind"], "past_jobs": used["n"]})
        return {"deleted": resource_id, "past_jobs": used["n"]}

    @app.post("/api/resources/{resource_id}/scopes")
    def add_resource_scope(resource_id: str, s: ScopeIn,
                           auth: Auth = Depends(require_role("admin"))):
        if db.one("SELECT id FROM resources WHERE id=?", (resource_id,)) is None:
            raise HTTPException(status_code=404, detail="resource not found")
        _check_scope(s.scope_type, s.scope_id)
        scope_id = s.scope_id or "*"
        if db.one("SELECT id FROM grants WHERE resource_id=? AND scope_type=? AND "
                  "scope_id=?", (resource_id, s.scope_type, scope_id)):
            raise HTTPException(status_code=409, detail="此範圍已授權")
        gid = new_id("grt")
        db.write("INSERT INTO grants(id, resource_id, scope_type, scope_id, created_at) "
                 "VALUES(?,?,?,?,?)", (gid, resource_id, s.scope_type, scope_id, now()))
        db.audit(auth.actor, "grant.create", "grant", gid,
                 {"resource": resource_id, "scope": f"{s.scope_type}:{scope_id}"})
        return {"id": gid}

    @app.delete("/api/resources/{resource_id}/scopes/{grant_id}")
    def drop_resource_scope(resource_id: str, grant_id: str,
                            auth: Auth = Depends(require_role("admin"))):
        row = db.one("SELECT * FROM grants WHERE id=? AND resource_id=?",
                     (grant_id, resource_id))
        if row is None:
            raise HTTPException(status_code=404, detail="grant not found")
        db.write("DELETE FROM grants WHERE id=?", (grant_id,))
        db.audit(auth.actor, "grant.delete", "grant", grant_id,
                 {"resource": resource_id,
                  "scope": f"{row['scope_type']}:{row['scope_id']}"})
        return {"deleted": grant_id}

    @app.post("/api/resources/{resource_id}/install")
    def install_resource(resource_id: str, auth: Auth = Depends(require_role("admin"))):
        """Run the vendor's install command. Admin-only shell execution: the
        command is shown in the UI before it runs and the log comes back."""
        from . import resource_install
        try:
            return resource_install.run(db, resource_id, auth.actor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/resources/{resource_id}/test")
    async def test_resource(resource_id: str,
                           auth: Auth = Depends(require_role("admin"))):
        """Exercise the resource the way an agent would (read-only, no tokens
        spent). async def: the blocking probe runs on a worker thread."""
        from . import resource_test
        try:
            return await asyncio.to_thread(resource_test.run, db, resource_id,
                                           auth.actor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/resources", dependencies=[Depends(require_role("viewer"))])
    def list_resources():
        return [_resource_row(r) for r in
                db.query("SELECT * FROM resources WHERE kind != 'secret' "
                         "ORDER BY kind, created_at")]

    @app.post("/api/grants")
    def create_grant(g: GrantIn, auth: Auth = Depends(require_role("admin"))):
        gid = new_id("grt")
        db.write(
            "INSERT INTO grants(id, resource_id, scope_type, scope_id, budget_usd, "
            "budget_tokens, period, max_concurrency, on_exceed, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (gid, g.resource_id, g.scope_type, g.scope_id, g.budget_usd, g.budget_tokens,
             g.period, g.max_concurrency, g.on_exceed, now()),
        )
        db.audit(auth.actor, "grant.create", "grant", gid,
                 {"resource": g.resource_id, "scope": f"{g.scope_type}:{g.scope_id}",
                  "budget_usd": g.budget_usd, "max_concurrency": g.max_concurrency})
        return {"id": gid}

    @app.get("/api/grants", dependencies=[Depends(require_role("viewer"))])
    def list_grants():
        return [dict(r) for r in db.query("SELECT * FROM grants ORDER BY created_at")]

    @app.post("/api/dispatch")
    async def dispatch(d: DispatchIn, auth: Auth = Depends(require_role("operator"))):
        # async def: dispatch must run on the main event loop to spawn the run task
        try:
            job_id = orch.dispatch(actor=auth.actor, req=DispatchRequest(
                project_id=d.project_id, prompt=d.prompt,
                title=d.title or d.prompt[:60], agent_id=d.agent_id,
                resource_id=d.resource_id, template_id=d.template_id,
                timeout_s=d.timeout_s, use_worktree=d.use_worktree,
                delivery=d.delivery,
            ))
        except QuotaError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"job_id": job_id}

    @app.post("/api/templates")
    def create_template(t: TemplateIn, auth: Auth = Depends(require_role("operator"))):
        from .workflow import parse_stages
        try:
            parse_stages(t.stages)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        row = db.one("SELECT version FROM workflow_templates WHERE id=?", (t.name,))
        version = (row["version"] + 1) if row else 1
        db.write("INSERT OR REPLACE INTO workflow_templates(id, name, version, stages_json) "
                 "VALUES(?,?,?,?)", (t.name, t.name, version, json.dumps(t.stages)))
        db.audit(auth.actor, "template.upsert", "template", t.name, {"version": version})
        return {"id": t.name, "version": version}

    @app.get("/api/templates", dependencies=[Depends(require_role("viewer"))])
    def list_templates():
        rows = [dict(r) for r in db.query("SELECT * FROM workflow_templates ORDER BY id")]
        assigned: dict[str, list[str]] = {}
        for project in db.query("SELECT id, default_template_id FROM projects "
                                "WHERE default_template_id IS NOT NULL"):
            assigned.setdefault(project["default_template_id"], []).append(project["id"])
        for row in rows:
            row["assigned_projects"] = assigned.get(row["id"], [])
        return rows

    @app.get("/api/workflow-catalog", dependencies=[Depends(require_role("viewer"))])
    def workflow_catalog():
        """Built-in presets plus the role/gate vocabulary the builder offers."""
        from .execution_capabilities import catalog as capability_catalog
        from .workflow_presets import EVIDENCE_TYPES, GATES, PRESETS, ROLES
        return {"presets": PRESETS, "roles": ROLES, "gates": GATES,
                "evidence_types": EVIDENCE_TYPES,
                "capabilities": capability_catalog()}

    @app.get("/api/execution-capabilities",
             dependencies=[Depends(require_role("viewer"))])
    async def execution_capability_health():
        """Live host probes; unlike the catalog, these prove the operation works."""
        from .execution_capabilities import CATALOG, probe
        statuses = await asyncio.gather(*(
            asyncio.to_thread(probe, capability) for capability in CATALOG))
        return [status.__dict__ for status in statuses]

    @app.delete("/api/templates/{template_id}")
    def delete_template(template_id: str, auth: Auth = Depends(require_role("operator"))):
        using = [r["id"] for r in db.query(
            "SELECT id FROM projects WHERE default_template_id=?", (template_id,))]
        if using:
            raise HTTPException(status_code=409,
                                detail=f"仍被專案使用中：{using} — 請先改指派其他範本")
        cur = db.write("DELETE FROM workflow_templates WHERE id=?", (template_id,))
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail="template not found")
        db.audit(auth.actor, "template.delete", "template", template_id, {})
        return {"deleted": template_id}

    @app.post("/api/projects/{project_id}/template")
    def assign_project_template(project_id: str, t: ProjectTemplateIn,
                                auth: Auth = Depends(require_role("operator"))):
        """Bind a workflow to a project — dispatches then default to it."""
        if db.one("SELECT id FROM projects WHERE id=?", (project_id,)) is None:
            raise HTTPException(status_code=404, detail="project not found")
        template_id = t.template_id or None
        if template_id and db.one("SELECT id FROM workflow_templates WHERE id=?",
                                  (template_id,)) is None:
            raise HTTPException(status_code=404, detail="template not found")
        db.write("UPDATE projects SET default_template_id=?, updated_at=? WHERE id=?",
                 (template_id, now(), project_id))
        db.audit(auth.actor, "project.template", "project", project_id,
                 {"template": template_id})
        return {"project_id": project_id, "template_id": template_id}

    @app.get("/api/roles", dependencies=[Depends(require_role("viewer"))])
    def list_roles(project_id: str | None = None):
        where, params = ("WHERE project_id=?", (project_id,)) if project_id else ("", ())
        return [dict(r) for r in db.query(
            "SELECT par.*, a.name AS agent_name, a.executor_type, a.enabled "
            "FROM project_agent_roles par JOIN agents a ON a.id = par.agent_id "
            f"{where} ORDER BY par.project_id, par.role, par.preference DESC", params)]

    @app.delete("/api/roles")
    def unassign_role(project_id: str, agent_id: str, role: str,
                      auth: Auth = Depends(require_role("operator"))):
        cur = db.write("DELETE FROM project_agent_roles WHERE project_id=? AND "
                       "agent_id=? AND role=?", (project_id, agent_id, role))
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail="assignment not found")
        remaining = db.one("SELECT COUNT(*) AS n FROM project_agent_roles "
                           "WHERE project_id=? AND agent_id=?",
                           (project_id, agent_id))["n"]
        if not remaining:
            _drop_project_membership(project_id, agent_id)
        db.audit(auth.actor, "role.unassign", "project", project_id,
                 {"agent": agent_id, "role": role,
                  "membership_removed": not remaining})
        return {"removed": True, "membership_removed": not remaining}

    @app.post("/api/roles")
    def assign_role(r: RoleIn, auth: Auth = Depends(require_role("operator"))):
        db.write("INSERT OR REPLACE INTO project_agent_roles(project_id, agent_id, role, "
                 "preference) VALUES(?,?,?,?)",
                 (r.project_id, r.agent_id, r.role, r.preference))
        member = _sync_project_membership(r.project_id, r.agent_id)
        db.audit(auth.actor, "role.assign", "project", r.project_id,
                 {"agent": r.agent_id, "role": r.role, "amos_member": member})
        return {"ok": True, "amos_member": member,
                "note": ("已同步為 AMOS 專案成員（可讀取該專案記憶）" if member
                         else "AMOS 無法連線 — 成員身分待下次啟動自動補上")}

    @app.get("/api/jobs", dependencies=[Depends(require_role("viewer"))])
    def list_jobs(project_id: str | None = None, limit: int = 50,
                  include_archived: bool = False):
        clauses, params = [], []
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if not include_archived:
            clauses.append("archived=0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = [dict(r) for r in db.query(
            "SELECT id, project_id, template_id, title, stage, status, priority, "
            "archived, rework_count, delivery_status, delivery_json, "
            "stages_snapshot_json, created_at, updated_at "
            f"FROM jobs {where} ORDER BY updated_at DESC LIMIT ?",
            (*params, limit))]
        # liveness for the board: the latest run's heartbeat says whether an
        # in-progress card is working or stuck — updated_at alone cannot,
        # because a long stage legitimately goes minutes between DB writes
        for row in rows:
            row["stage_nodes"] = [dict(node) for node in db.query(
                "SELECT stage,status,needs_json,workspace,head_commit,started_at,"
                "finished_at FROM job_stage_nodes WHERE job_id=? ORDER BY rowid",
                (row["id"],))]
            if row["status"] != "in_progress":
                continue
            beat = db.one(
                "SELECT status, heartbeat_at, progress_at, progress_text, "
                "started_at FROM runs WHERE job_id=? ORDER BY rowid DESC LIMIT 1",
                (row["id"],))
            if beat is not None:
                row["run_status"] = beat["status"]
                row["heartbeat_at"] = beat["heartbeat_at"] or beat["started_at"]
                # silence is the diagnostic: a card can be alive (heartbeat) and
                # yet not have said a word for an hour, which is what being stuck
                # behind a blocked child process actually looks like
                row["progress_at"] = beat["progress_at"] or beat["started_at"]
                row["progress_text"] = beat["progress_text"]
        return rows

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(require_role("viewer"))])
    def get_job(job_id: str):
        row = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        job = dict(row)
        # `error` and `executor_type` matter here: the drawer is where a stuck
        # card is diagnosed, and without the reason there is nothing to act on
        job["runs"] = [dict(r) for r in db.query(
            "SELECT id, stage, attempt, agent_id, executor_type, status, error, "
            "cost_usd, accounting_precision, started_at, finished_at "
            "FROM runs WHERE job_id=? ORDER BY started_at", (job_id,))]
        job["gates"] = [dict(g) for g in db.query(
            "SELECT g.* FROM gate_results g JOIN runs r ON r.id=g.run_id "
            "WHERE r.job_id=? ORDER BY g.at", (job_id,))]
        job["deliveries"] = [dict(d) for d in db.query(
            "SELECT * FROM deliveries WHERE job_id=? ORDER BY started_at", (job_id,))]
        job["stage_nodes"] = [dict(node) for node in db.query(
            "SELECT stage,status,needs_json,workspace,worktree_path,head_commit,"
            "started_at,finished_at,updated_at FROM job_stage_nodes "
            "WHERE job_id=? ORDER BY rowid", (job_id,))]
        runs_by_stage = {}
        for run in job["runs"]:
            previous = runs_by_stage.get(run["stage"])
            if previous is None or run["attempt"] >= previous["attempt"]:
                runs_by_stage[run["stage"]] = run
        gates_by_run = {gate["run_id"]: gate for gate in job["gates"]}
        nodes_by_stage = {node["stage"]: node for node in job["stage_nodes"]}
        try:
            frozen_stages = json.loads(job["stages_snapshot_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            frozen_stages = []
        job["evidence_matrix"] = []
        for stage in frozen_stages:
            run = runs_by_stage.get(stage.get("name"))
            gate = gates_by_run.get(run["id"]) if run else None
            node = nodes_by_stage.get(stage.get("name"), {})
            for kind in stage.get("evidence") or []:
                job["evidence_matrix"].append({
                    "kind": kind, "stage": stage.get("name"),
                    "gate": stage.get("gate", "auto"),
                    "verdict": gate["verdict"] if gate else "pending",
                    "run_id": run["id"] if run else None,
                    "head_commit": node.get("head_commit"),
                })
        # what the PM did about this card, and above all what it is ASKING.
        # An escalation that lives only in the audit log is an escalation to
        # nobody: the operator saw "blocked" and a retry button, with no sign
        # that the PM had a question for them.
        pm = db.one("SELECT actor, at, detail_json FROM audit_log WHERE "
                    "action='job.pm_intervention' AND target_id=? "
                    "ORDER BY id DESC LIMIT 1", (job_id,))
        job["pm_decision"] = None
        if pm is not None:
            try:
                detail = json.loads(pm["detail_json"] or "{}")
                decision = detail.get("decision") or {}
                job["pm_decision"] = {
                    "pm": pm["actor"].split(":", 1)[-1], "at": pm["at"],
                    "action": decision.get("action"),
                    "reason": decision.get("reason"),
                    "cycle": detail.get("cycle"), "max": detail.get("max"),
                }
            except json.JSONDecodeError:
                pass
        return job

    @app.post("/api/jobs/{job_id}/supplies")
    def add_supply(job_id: str, body: SupplyIn,
                   auth: Auth = Depends(require_role("operator"))):
        """Hand data to a job after dispatch — a deploy target, a project id, a
        decision the spec could not contain. It reaches the agents two ways:
        every later run's brief carries it, and if a worktree is live it is also
        dropped into `._bastet/inbox/` for the current run to pick up.

        Secrets are refused: a supply travels inside a prompt, which means it
        travels to the LLM provider. Credentials belong on the Admin card, where
        they arrive as env vars and never enter a prompt."""
        job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job["status"] in ("done", "cancelled"):
            raise HTTPException(status_code=409,
                                detail=f"任務已{job['status']}，補給沒有對象了 — "
                                       f"需要的話請重試或重新派工再提供")
        if not body.name.strip() or not body.content.strip():
            raise HTTPException(status_code=400, detail="名稱與內容都要填")
        looks_secret = secrets_store.smells_like_secret(body.content)
        if looks_secret:
            raise HTTPException(
                status_code=400,
                detail=f"內容看起來是機敏資料（{looks_secret}）。補給會進入 prompt "
                       f"送到 LLM 供應商 —— 請改用「管理 → 憑證」建立後掛到專案資源，"
                       f"它會以環境變數交給 agent，不經過 prompt。")
        supply_id = new_id("sup")
        db.write("INSERT INTO job_supplies(id, job_id, name, content, created_by, "
                 "created_at) VALUES(?,?,?,?,?,?)",
                 (supply_id, job_id, body.name.strip(), body.content.strip(),
                  auth.actor, now()))
        db.audit(auth.actor, "job.supply", "job", job_id,
                 {"name": body.name.strip(), "chars": len(body.content)})
        # best effort for the run that is already going: an inbox file in the
        # worktree. The brief tells agents to check it; a -p CLI that never
        # looks still gets the supply on its next stage.
        delivered_live = False
        if job["worktree_path"] and Path(job["worktree_path"]).is_dir():
            inbox = Path(job["worktree_path"]) / "._bastet" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if c.isalnum() or c in "-_" else "-"
                           for c in body.name.strip())[:60] or "supply"
            (inbox / f"{supply_id}-{safe}.md").write_text(
                f"# {body.name.strip()}\n\n{body.content.strip()}\n")
            delivered_live = True
        bus.emit("job.supplied", project_id=job["project_id"], job_id=job_id,
                 name=body.name.strip())
        return {"id": supply_id, "delivered_to_live_worktree": delivered_live}

    @app.get("/api/jobs/{job_id}/supplies",
             dependencies=[Depends(require_role("viewer"))])
    def list_supplies(job_id: str):
        return [dict(r) for r in db.query(
            "SELECT id, name, content, created_by, created_at FROM job_supplies "
            "WHERE job_id=? ORDER BY created_at", (job_id,))]

    @app.delete("/api/jobs/{job_id}/supplies/{supply_id}")
    def delete_supply(job_id: str, supply_id: str,
                      auth: Auth = Depends(require_role("operator"))):
        gone = db.write("DELETE FROM job_supplies WHERE id=? AND job_id=?",
                        (supply_id, job_id)).rowcount
        if not gone:
            raise HTTPException(status_code=404, detail="supply not found")
        db.audit(auth.actor, "job.supply_removed", "job", job_id,
                 {"supply": supply_id})
        return {"deleted": supply_id}

    # ---- previews for human approval -----------------------------------------

    @app.get("/api/jobs/{job_id}/previews",
             dependencies=[Depends(require_role("viewer"))])
    def list_previews(job_id: str):
        # containment, not sanitisation: Path("..").name is ".." (only slashes
        # strip), which the first attempt at this fix learned from its own test
        folder = (home.artifacts_dir / job_id / "preview").resolve()
        if not folder.is_relative_to(home.artifacts_dir.resolve()):
            raise HTTPException(status_code=404, detail="preview not found")
        if not folder.is_dir():
            return []
        return sorted(p.name for p in folder.iterdir() if p.is_file())

    @app.get("/api/jobs/{job_id}/previews/{name}",
             dependencies=[Depends(require_role("viewer"))])
    def get_preview(job_id: str, name: str):
        from fastapi.responses import FileResponse
        path = (home.artifacts_dir / job_id / "preview" / name).resolve()
        if not path.is_relative_to(home.artifacts_dir.resolve()) \
                or not path.is_file():
            raise HTTPException(status_code=404, detail="preview not found")
        return FileResponse(str(path), filename=path.name)

    @app.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: str, body: RetryIn,
                        auth: Auth = Depends(require_role("operator"))):
        # async def: the re-dispatch spawns its driver on the main event loop
        try:
            return orch.retry(job_id, agent_id=body.agent_id, user=auth.name,
                              spec=body.spec,
                              refresh_workflow=body.refresh_workflow,
                              renew_recovery_lease=body.renew_recovery_lease,
                              restart_from_rework_target=(
                                  body.restart_from_rework_target))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/revalidate")
    async def revalidate_job_gate(job_id: str,
                                  auth: Auth = Depends(require_role("operator"))):
        try:
            return orch.revalidate_gate(job_id, user=auth.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/jobs/{job_id}/delivery")
    async def configure_job_delivery(
        job_id: str, body: DeliveryIn,
        auth: Auth = Depends(require_role("operator")),
    ):
        try:
            return orch.configure_delivery(job_id, body.model_dump(exclude_none=True),
                                           user=auth.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/archive")
    def archive_job(job_id: str, body: ArchiveIn,
                    auth: Auth = Depends(require_role("operator"))):
        """Clear a finished card off the board, keeping its history."""
        try:
            return orch.archive_job(job_id, body.archived, actor=auth.actor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: str, auth: Auth = Depends(require_role("operator"))):
        """Recoverably remove a finished card from the board.

        Kept as DELETE for client compatibility; the record and all history are
        retained as archived and can be restored with the archive endpoint.
        """
        try:
            return orch.delete_job(job_id, actor=auth.actor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/approve")
    async def approve_job(job_id: str, a: ApproveIn,
                          auth: Auth = Depends(require_role("operator"))):
        # async def: resuming the job driver needs the main event loop
        try:
            return orch.approve(job_id, a.approved, a.comment, user=auth.name,
                                stage_name=a.stage)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}", dependencies=[Depends(require_role("viewer"))])
    def get_run(run_id: str):
        row = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        run = dict(row)
        run["executor_handle_json"] = None  # internal
        run["ledger"] = [dict(led) for led in db.query(
            "SELECT model, tokens_in, tokens_out, cache_read, cache_write, cost_usd, at "
            "FROM usage_ledger WHERE run_id=? ORDER BY at", (run_id,))]
        return run

    @app.get("/api/runs", dependencies=[Depends(require_role("viewer"))])
    def list_runs(limit: int = 50):
        return [dict(r) for r in db.query(
            "SELECT id, job_id, stage, agent_id, executor_type, status, cost_usd, "
            "accounting_precision, started_at, finished_at, heartbeat_at, "
            "progress_at, progress_text FROM runs "
            "ORDER BY started_at DESC LIMIT ?", (limit,))]

    @app.get("/api/usage", dependencies=[Depends(require_role("viewer"))])
    def usage(project_id: str | None = None):
        where, params = ("WHERE j.project_id=?", (project_id,)) if project_id else ("", ())
        rows = db.query(
            "SELECT j.project_id, r.agent_id, r.accounting_precision, COUNT(*) runs, "
            "SUM(r.tokens_in) tokens_in, SUM(r.tokens_out) tokens_out, "
            "SUM(r.cache_read) cache_read, SUM(r.cache_write) cache_write, "
            "SUM(r.cost_usd) cost_usd "
            f"FROM runs r JOIN jobs j ON j.id = r.job_id {where} "
            "GROUP BY j.project_id, r.agent_id, r.accounting_precision", params)
        return [dict(r) for r in rows]

    # ---- system settings (timezone first) -----------------------------------

    @app.get("/api/settings", dependencies=[Depends(require_role("viewer"))])
    def get_settings():
        """Installation-level settings the UI needs. Storage stays UTC — an
        audit trail in local time cannot be compared across machines — and the
        timezone here only decides how the browser renders timestamps."""
        from . import settings as settings_mod
        return settings_mod.public(home.config())

    @app.put("/api/settings")
    def put_settings(body: SettingsIn,
                     auth: Auth = Depends(require_role("admin"))):
        from . import settings as settings_mod
        if not settings_mod.valid_timezone(body.timezone):
            raise HTTPException(status_code=400,
                                detail=f"未知的時區：{body.timezone}（要 IANA 名稱，"
                                       f"例如 Asia/Taipei）")
        config = home.config()
        previous = config.get("timezone") or "UTC"
        config["timezone"] = body.timezone
        home.save_config(config)
        db.audit(auth.actor, "settings.timezone", "settings", "timezone",
                 {"from": previous, "to": body.timezone})
        return settings_mod.public(config)

    @app.post("/api/config/apply")
    def config_apply(body: ConfigApplyIn,
                     auth: Auth = Depends(require_role("admin"))):
        """Apply a chat-proposed configuration. The model proposed; the human
        pressing this is the authority, and the audit rows carry their name."""
        from . import self_config as self_config_mod
        results = self_config_mod.apply(db, home.root, body.actions, auth.actor)
        return {"results": results,
                "ok": sum(1 for r in results if r["status"] == "ok"),
                "failed": sum(1 for r in results if r["status"] != "ok")}

    @app.get("/api/audit", dependencies=[Depends(require_role("viewer"))])
    def audit_log(limit: int = 100, q: str = "", action: str = "", actor: str = "",
                  target_type: str = "", since: str = "", until: str = ""):
        """Newest first, filterable. An audit log you cannot search is a log you
        never read: `action` matches a prefix (`job.` covers every job event),
        `q` matches actor/action/target/detail."""
        clauses, params = [], []
        if action:
            clauses.append("(action = ? OR action LIKE ?)")
            params += [action, f"{action.rstrip('.')}.%"]
        if actor:
            clauses.append("actor LIKE ?")
            params.append(f"%{actor}%")
        if target_type:
            clauses.append("target_type = ?")
            params.append(target_type)
        if since:
            clauses.append("at >= ?")
            params.append(since)
        if until:
            clauses.append("at <= ?")
            params.append(f"{until}T23:59:59+00:00" if len(until) == 10 else until)
        if q:
            clauses.append("(actor LIKE ? OR action LIKE ? OR target_id LIKE ? "
                           "OR detail_json LIKE ?)")
            params += [f"%{q}%"] * 4
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = [dict(r) for r in db.query(
            "SELECT at, actor, action, target_type, target_id, detail_json "
            f"FROM audit_log {where} ORDER BY id DESC LIMIT ?",
            (*params, max(1, min(limit, 1000))))]
        return {"rows": rows, "count": len(rows),
                "actions": [r["action"] for r in db.query(
                    "SELECT DISTINCT action FROM audit_log ORDER BY action")],
                "categories": sorted({a["action"].split(".")[0] for a in db.query(
                    "SELECT DISTINCT action FROM audit_log")})}

    # ---- maintenance: check & update the moving parts (SPEC §5.13) -----------

    @app.get("/api/context/evaluations",
             dependencies=[Depends(require_role("viewer"))])
    def context_evaluations(limit: int = 100):
        from . import context_eval
        return context_eval.recent(db, max(1, min(limit, 500)))

    @app.post("/api/context/evaluate")
    def context_evaluate(body: ContextEvalIn,
                         auth: Auth = Depends(require_role("operator"))):
        from . import context_eval
        try:
            result = context_eval.evaluate(db, **body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        db.audit(auth.actor, "context.evaluated", "job", body.job_id,
                 {"evaluation_id": result["id"], "passed": result["passed"]})
        return result

    @app.get("/api/maintenance/state",
             dependencies=[Depends(require_role("viewer"))])
    def maintenance_state():
        from . import maintenance_mode
        return maintenance_mode.state(db)

    @app.post("/api/maintenance/enter")
    def maintenance_enter(body: MaintenanceIn,
                          auth: Auth = Depends(require_role("admin"))):
        from . import maintenance_mode
        return maintenance_mode.enter(db, auth.actor, body.reason)

    @app.post("/api/maintenance/leave")
    def maintenance_leave(auth: Auth = Depends(require_role("admin"))):
        from . import maintenance_mode
        return maintenance_mode.leave(db, auth.actor)

    @app.get("/api/maintenance/components",
             dependencies=[Depends(require_role("admin"))])
    async def maintenance_components():
        """What is installed vs available. Runs pip/CLI probes, so off-thread."""
        from . import maintenance
        return await asyncio.to_thread(maintenance.check_all)

    @app.post("/api/maintenance/components/{component_id}/update")
    async def maintenance_update(component_id: str,
                                 auth: Auth = Depends(require_role("admin"))):
        from . import maintenance, maintenance_mode
        try:
            maintenance_mode.require_drained(db)
            return await asyncio.to_thread(maintenance.update, db, component_id,
                                           auth.actor)
        except maintenance_mode.MaintenanceModeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/maintenance/update-all")
    async def maintenance_update_all(auth: Auth = Depends(require_role("admin"))):
        from . import maintenance, maintenance_mode
        try:
            maintenance_mode.require_drained(db)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await asyncio.to_thread(maintenance.update_all, db, auth.actor)

    @app.get("/api/memory/browse", dependencies=[Depends(require_role("viewer"))])
    def memory_browse(scope: str = "", limit: int = 50):
        """Recent memories, so the tab is not search-only: you cannot search for
        what you do not know exists."""
        client = amos_client()
        if client is None:
            raise HTTPException(status_code=502, detail="AMOS unavailable")
        try:
            rows = client.list_recent(limit=max(1, min(limit, 200)))
        except Exception as exc:
            raise HTTPException(status_code=502,
                                detail=f"AMOS list failed: {type(exc).__name__}") from exc
        items = [row for row in (_memory_row(r) for r in (rows or []))
                 if not scope or scope in (row["scope"] or "")]
        stats = {}
        try:
            stats = client.stats() or {}
        except Exception:            # a missing summary must not hide the list
            stats = {}
        from . import maintenance
        return {"items": items, "stats": stats,
                "console": maintenance.amos_web(cfg),
                "recall": maintenance.semantic_status()}

    @app.post("/api/runs/{run_id}/respond")
    async def respond_run(run_id: str, r: RespondIn,
                          auth: Auth = Depends(require_role("operator"))):
        # async def: executor.respond resolves a future on the main event loop
        try:
            return await orch.respond(run_id, r.request_id, r.reply, user=auth.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/diff", dependencies=[Depends(require_role("viewer"))])
    def run_diff(run_id: str):
        row = db.one("SELECT artifacts_json FROM runs WHERE id=?", (run_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        diff_path = json.loads(row["artifacts_json"] or "{}").get("diff")
        if not diff_path or not Path(diff_path).exists():
            return {"diff": None}
        return {"diff": Path(diff_path).read_text()[:200_000]}

    @app.post("/api/resources/{resource_id}/enabled")
    def set_resource_enabled(resource_id: str, e: UserEnabledIn,
                             auth: Auth = Depends(require_role("admin"))):
        cur = db.write("UPDATE resources SET enabled=?, updated_at=? WHERE id=?",
                       (1 if e.enabled else 0, now(), resource_id))
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail="resource not found")
        db.audit(auth.actor, "resource.enabled" if e.enabled else "resource.disabled",
                 "resource", resource_id, {})
        return {"id": resource_id, "enabled": e.enabled}

    @app.get("/api/runs/{run_id}/interactions",
             dependencies=[Depends(require_role("viewer"))])
    def run_interactions(run_id: str):
        return [dict(r) for r in db.query(
            "SELECT request_id, kind, payload_json, status, created_at, answered_at "
            "FROM run_interactions WHERE run_id=? ORDER BY created_at", (run_id,))]

    @app.post("/api/gc")
    def gc(auth: Auth = Depends(require_role("admin"))):
        removed = orch.gc_worktrees()
        db.audit(auth.actor, "gc.worktrees", "server", "gc", {"removed": removed})
        return {"worktrees_removed": removed}

    # ---- users (multi-user auth, SPEC D9 / M3) --------------------------------

    @app.get("/api/me")
    def me(auth: Auth = Depends(get_auth)):
        return {"user_id": auth.user_id, "name": auth.name, "role": auth.role}

    @app.post("/api/users")
    def create_user(u: UserIn, auth: Auth = Depends(require_role("admin"))):
        try:
            user_id, token = users_mod.create_user(db, u.name, u.role)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.audit(auth.actor, "user.create", "user", user_id, {"name": u.name, "role": u.role})
        return {"id": user_id, "token": token,
                "note": "store this token now — it is never shown again"}

    @app.get("/api/user-roles", dependencies=[Depends(require_role("viewer"))])
    def user_roles():
        """What each role really allows — the dropdown explains itself."""
        return users_mod.capabilities()

    @app.put("/api/users/{user_id}")
    def update_user(user_id: str, body: UserUpdateIn,
                    auth: Auth = Depends(require_role("admin"))):
        if db.one("SELECT id FROM users WHERE id=?", (user_id,)) is None:
            raise HTTPException(status_code=404, detail="user not found")
        if body.role:
            try:
                users_mod.set_role(db, user_id, body.role)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if body.name:
            db.write("UPDATE users SET name=? WHERE id=?", (body.name, user_id))
        db.audit(auth.actor, "user.update", "user", user_id,
                 {"role": body.role, "name": body.name})
        return dict(db.one("SELECT id, name, role, enabled, created_at, last_used_at "
                           "FROM users WHERE id=?", (user_id,)))

    @app.post("/api/users/{user_id}/token")
    def rotate_user_token(user_id: str, auth: Auth = Depends(require_role("admin"))):
        """New token, old one dead immediately (the stored hash is replaced)."""
        try:
            token = users_mod.rotate_token(db, user_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        db.audit(auth.actor, "user.token.rotate", "user", user_id, {})
        return {"id": user_id, "token": token,
                "note": "舊 token 立即失效；這個新 token 只顯示這一次"}

    @app.delete("/api/users/{user_id}")
    def delete_user(user_id: str, auth: Auth = Depends(require_role("admin"))):
        row = db.one("SELECT name FROM users WHERE id=?", (user_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="user not found")
        users_mod.delete_user(db, user_id)
        db.audit(auth.actor, "user.delete", "user", user_id, {"name": row["name"]})
        return {"deleted": user_id}

    @app.get("/api/users", dependencies=[Depends(require_role("admin"))])
    def list_users():
        return [dict(r) for r in db.query(
            "SELECT id, name, role, enabled, created_at, last_used_at "
            "FROM users ORDER BY created_at")]  # token_hash never leaves the DB

    @app.post("/api/users/{user_id}/enabled")
    def set_user_enabled(user_id: str, e: UserEnabledIn,
                         auth: Auth = Depends(require_role("admin"))):
        if not users_mod.set_enabled(db, user_id, e.enabled):
            raise HTTPException(status_code=404, detail="user not found")
        db.audit(auth.actor, "user.enabled" if e.enabled else "user.disabled",
                 "user", user_id, {})
        return {"id": user_id, "enabled": e.enabled}

    # ---- channels (SPEC §5.7) ---------------------------------------------------

    @app.post("/api/channels")
    def create_channel(c: ChannelIn, auth: Auth = Depends(require_role("admin"))):
        secrets_store.reject_secrets_in_config(c.config)
        cid = new_id("chn")
        # raw tokens pasted into the ref field get secured into <home>/secrets
        secret_ref = secrets_store.ensure_ref(c.secret_ref, home.root, cid)
        db.write("INSERT INTO channels(id, kind, name, config_json, secret_ref, enabled) "
                 "VALUES(?,?,?,?,?,1)",
                 (cid, c.kind, c.name or c.kind, json.dumps(c.config), secret_ref))
        db.audit(auth.actor, "channel.create", "channel", cid,
                 {"kind": c.kind, "name": c.name,
                  "secret_scheme": secret_ref.split(":", 1)[0]})
        return {"id": cid, "note": "restart `bastet serve` to start the channel"}

    @app.get("/api/channels", dependencies=[Depends(require_role("admin"))])
    def list_channels():
        rows = [dict(r) for r in db.query("SELECT * FROM channels")]
        for row in rows:
            try:
                secrets_store.resolve(row["secret_ref"])
                credential = "ok"
            except secrets_store.SecretError:
                credential = "error"
            row["secret_ref"] = (row["secret_ref"] or "").split(":", 1)[0] + ":…"
            config = json.loads(row.pop("config_json") or "{}")
            row["paired_users"] = [b.get("name") for b in
                                   config.get("bindings", {}).values()]
            row["responder"] = config.get("responder") or None
            row["project_id"] = config.get("project_id") or ""
            live = next((ch for ch in app.state.channels
                         if ch.channel_id == row["id"]), None)
            # "polling" used to mean "the object exists"; a dead notify loop looked
            # perfectly healthy while approvals silently went nowhere
            row["notify_errors"] = getattr(live, "notify_errors", 0) if live else 0
            row["status"] = (
                "credential_error" if credential == "error"
                else "disabled" if not row["enabled"]
                else "notify_down" if live is not None and not getattr(
                    live, "notify_alive", False)
                else "polling" if live is not None
                else "restart_needed")
        return rows

    @app.put("/api/channels/{channel_id}/chat")
    def set_channel_chat(channel_id: str, body: ChannelChatIn,
                         auth: Auth = Depends(require_role("admin"))):
        from . import chat as chat_mod
        row = db.one("SELECT * FROM channels WHERE id=?", (channel_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="channel not found")
        config = json.loads(row["config_json"] or "{}")
        if body.responder_id:
            try:
                chat_mod._responder(db, body.responder_kind, body.responder_id)
            except chat_mod.ChatError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            config["responder"] = {"kind": body.responder_kind,
                                   "id": body.responder_id}
        else:
            config.pop("responder", None)
        if body.project_id:
            if db.one("SELECT id FROM projects WHERE id=?", (body.project_id,)) is None:
                raise HTTPException(status_code=400, detail="project not found")
            config["project_id"] = body.project_id
        else:
            config.pop("project_id", None)
        db.write("UPDATE channels SET config_json=? WHERE id=?",
                 (json.dumps(config), channel_id))
        db.audit(auth.actor, "channel.chat.configure", "channel", channel_id,
                 {"responder": config.get("responder"),
                  "project": config.get("project_id")})
        return {"id": channel_id, "responder": config.get("responder"),
                "project_id": config.get("project_id", "")}

    @app.post("/api/channels/{channel_id}/enabled")
    def set_channel_enabled(channel_id: str, e: UserEnabledIn,
                            auth: Auth = Depends(require_role("admin"))):
        cur = db.write("UPDATE channels SET enabled=? WHERE id=?",
                       (1 if e.enabled else 0, channel_id))
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail="channel not found")
        db.audit(auth.actor, "channel.enabled" if e.enabled else "channel.disabled",
                 "channel", channel_id, {})
        return {"id": channel_id, "enabled": e.enabled,
                "note": "重啟 bastet serve 生效"}

    @app.delete("/api/channels/{channel_id}")
    def delete_channel(channel_id: str, auth: Auth = Depends(require_role("admin"))):
        cur = db.write("DELETE FROM channels WHERE id=?", (channel_id,))
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail="channel not found")
        db.audit(auth.actor, "channel.delete", "channel", channel_id, {})
        return {"deleted": channel_id, "note": "重啟 bastet serve 停止輪詢"}

    @app.post("/api/channels/{channel_id}/pair")
    def pair_channel(channel_id: str, p: PairIn,
                     auth: Auth = Depends(require_role("admin"))):
        from .channels.telegram import issue_pairing_code
        if db.one("SELECT id FROM channels WHERE id=?", (channel_id,)) is None:
            raise HTTPException(status_code=404, detail="channel not found")
        target_user, target_name = auth.user_id, auth.name
        if p.user_id:
            row = db.one("SELECT * FROM users WHERE id=? AND enabled=1", (p.user_id,))
            if row is None:
                raise HTTPException(status_code=404, detail="user not found")
            target_user, target_name = row["id"], row["name"]
        code = issue_pairing_code(db, target_user, target_name)
        db.audit(auth.actor, "channel.pair_code", "channel", channel_id,
                 {"for_user": target_user})
        return {"code": code, "note": f"send `/pair {code}` to the bot within 15 minutes"}

    # ---- WebSocket event stream (SPEC §5.10) --------------------------------
    # Browser WebSocket clients can't set Authorization headers, so the first
    # message must be {"token": "<api token>"} — never put the token in the URL.

    @app.websocket("/api/ws")
    async def events_ws(ws: WebSocket):
        if not _host_ok(ws.headers.get("host", ""), allowed_hosts):
            log.warning("ws rejected: bad host %r", ws.headers.get("host"))
            await ws.close(code=4403)
            return
        origin = ws.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            if not _host_ok(urlparse(origin).netloc, allowed_hosts):
                log.warning("ws rejected: bad origin %r", origin)
                await ws.close(code=4403)
                return
        await ws.accept()
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=10)
        except Exception:
            await ws.close(code=4401)
            return
        if users_mod.verify(db, str(first.get("token") or ""), api_token) is None:
            await ws.close(code=4401)
            return
        project_filter = first.get("project_id")
        queue = bus.subscribe()
        try:
            await ws.send_json({"type": "hello", "filtered": bool(project_filter)})
            while True:
                event = await queue.get()
                if project_filter and event.get("project_id") not in (project_filter, None):
                    continue
                await ws.send_text(event_dumps(event))
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(queue)

    @app.websocket("/api/login-sessions/{session_id}/ws")
    async def login_session_ws(ws: WebSocket, session_id: str):
        if not _host_ok(ws.headers.get("host", ""), allowed_hosts):
            await ws.close(code=4403)
            return
        await ws.accept()
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=10)
        except Exception:
            await ws.close(code=4401)
            return
        who = users_mod.verify(db, str(first.get("token") or ""), api_token)
        if who is None or not who.at_least("operator"):
            await ws.close(code=4401)
            return
        try:
            session, queue = login_manager.subscribe(session_id)
        except KeyError:
            await ws.close(code=4404)
            return

        async def pump_output():
            while True:
                chunk = await queue.get()
                if chunk is None:
                    await ws.send_json({"done": True,
                                        "exit_code": session.exit_code})
                    return
                await ws.send_json({"output": chunk.decode(errors="replace")})

        pump = asyncio.get_running_loop().create_task(pump_output())
        try:
            while True:
                message = await ws.receive_json()
                if "input" in message:
                    login_manager.write(session_id, str(message["input"]))
        except Exception:
            pass
        finally:
            pump.cancel()
            login_manager.unsubscribe(session_id, queue)

    # ---- version (unauthenticated: the UI shows it before you have a token) ---

    @app.get("/api/version")
    def version():
        from . import __version__
        return {"name": "Bastet Agent OS", "version": __version__}

    # ---- Kanban UI (built by `npm run build` in web/) -------------------------

    ui_dist = Path(__file__).parent / "ui_dist"
    if ui_dist.exists():
        from fastapi.staticfiles import StaticFiles
        app.mount("/ui", StaticFiles(directory=str(ui_dist), html=True), name="ui")

    @app.get("/", response_class=HTMLResponse)
    def status_page():
        runs = db.query(
            "SELECT r.id, r.status, r.cost_usd, r.accounting_precision, j.title "
            "FROM runs r JOIN jobs j ON j.id=r.job_id ORDER BY r.started_at DESC LIMIT 20")
        rows = "".join(
            f"<tr><td>{r['id']}</td><td>{r['title']}</td><td>{r['status']}</td>"
            f"<td>${r['cost_usd']:.4f}</td><td>{r['accounting_precision'] or '—'}</td></tr>"
            for r in runs)
        return f"""<!doctype html><meta charset="utf-8"><title>Bastet</title>
<style>body{{font-family:system-ui;margin:2rem;max-width:60rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:.4rem .6rem;text-align:left}}</style>
<h1>🐈 Bastet Agent OS</h1><p>M1 minimal status page. Recent runs:</p>
<table><tr><th>run</th><th>title</th><th>status</th><th>cost</th><th>precision</th></tr>{rows}</table>"""

    synced = _reconcile_memberships()
    if synced:
        log.info("AMOS membership reconciled for %d role assignments", synced)

    return app
