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

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
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
    secret_ref: str | None = None
    config: dict[str, Any] = {}


class GrantIn(BaseModel):
    resource_id: str
    scope_type: str
    scope_id: str
    budget_usd: float | None = None
    budget_tokens: int | None = None
    period: str = "lifetime"
    max_concurrency: int | None = None
    on_exceed: str = "block"


class DispatchIn(BaseModel):
    project_id: str
    prompt: str
    title: str = ""
    agent_id: str
    resource_id: str | None = None
    template_id: str | None = None
    timeout_s: int = 3600
    use_worktree: bool = True


class TemplateIn(BaseModel):
    name: str
    stages: list[dict]


class RoleIn(BaseModel):
    project_id: str
    agent_id: str
    role: str
    preference: int = 0


class ApproveIn(BaseModel):
    approved: bool
    comment: str = ""


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

    channels: list = []

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        from .channels.telegram import TelegramChannel
        tasks = []
        for row in db.query("SELECT * FROM channels WHERE enabled=1 AND kind='telegram'"):
            try:
                bot_token = secrets_store.resolve(row["secret_ref"])
            except secrets_store.SecretError as exc:
                log.warning("channel %s: credential error (%s); not started", row["id"], exc)
                continue
            channel = TelegramChannel(db, orch, bus, row["id"], bot_token)
            channels.append(channel)
            tasks.append(asyncio.get_running_loop().create_task(channel.run()))
            log.info("telegram channel %s started (long polling)", row["id"])
        yield
        for channel in channels:
            channel.stop()
        for task in tasks:
            task.cancel()

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

    # ---- endpoints ----------------------------------------------------------

    @app.post("/api/projects")
    def create_project(p: ProjectIn, auth: Auth = Depends(require_role("operator"))):
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
        db.audit(auth.actor, "project.create", "project", p.id, {"team": team_id})
        return {"id": p.id, "team_id": team_id}

    @app.get("/api/projects", dependencies=[Depends(require_role("viewer"))])
    def list_projects():
        return [dict(r) for r in db.query("SELECT * FROM projects ORDER BY created_at")]

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
            (b.project_id, amos_project["team_id"], b.repo_path, "{}", ts, ts))
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

    @app.put("/api/agents/{agent_id}")
    def update_agent(agent_id: str, a: AgentUpdateIn,
                     auth: Auth = Depends(require_role("operator"))):
        row = db.one("SELECT * FROM agents WHERE id=?", (agent_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="agent not found")
        fields = {k: v for k, v in a.model_dump().items() if v is not None}
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
        return [{"id": h.get("id"), "score": h.get("score"),
                 "content": h.get("content"), "scope": h.get("scope"),
                 "type": h.get("type")} for h in hits]

    @app.post("/api/resources")
    def create_resource(r: ResourceIn, auth: Auth = Depends(require_role("admin"))):
        try:
            secrets_store.reject_secrets_in_config(r.config)
        except secrets_store.SecretError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        rid = new_id("res")
        ts = now()
        secret_ref = secrets_store.ensure_ref(r.secret_ref or "", home.root, rid) or None
        db.write(
            "INSERT INTO resources(id, kind, name, endpoint, api_flavor, secret_ref, "
            "config_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, r.kind, r.name, r.endpoint, r.api_flavor, secret_ref,
             json.dumps(r.config), ts, ts),
        )
        db.audit(auth.actor, "resource.create", "resource", rid,
                 {"kind": r.kind, "name": r.name,
                  "secret_scheme": (r.secret_ref or "").split(":", 1)[0]})
        return {"id": rid}

    @app.get("/api/resources", dependencies=[Depends(require_role("viewer"))])
    def list_resources():
        rows = [dict(r) for r in db.query("SELECT * FROM resources ORDER BY created_at")]
        for row in rows:
            row["secret_ref"] = (row["secret_ref"] or "").split(":", 1)[0] + ":…"  # never echo
        return rows

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
        from .workflow_presets import GATES, PRESETS, ROLES
        return {"presets": PRESETS, "roles": ROLES, "gates": GATES}

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

    @app.post("/api/roles")
    def assign_role(r: RoleIn, auth: Auth = Depends(require_role("operator"))):
        db.write("INSERT OR REPLACE INTO project_agent_roles(project_id, agent_id, role, "
                 "preference) VALUES(?,?,?,?)",
                 (r.project_id, r.agent_id, r.role, r.preference))
        db.audit(auth.actor, "role.assign", "project", r.project_id,
                 {"agent": r.agent_id, "role": r.role})
        return {"ok": True}

    @app.get("/api/jobs", dependencies=[Depends(require_role("viewer"))])
    def list_jobs(project_id: str | None = None, limit: int = 50):
        where, params = ("WHERE project_id=?", (project_id,)) if project_id else ("", ())
        return [dict(r) for r in db.query(
            "SELECT id, project_id, template_id, title, stage, status, priority, "
            "stages_snapshot_json, created_at, updated_at "
            f"FROM jobs {where} ORDER BY updated_at DESC LIMIT ?",
            (*params, limit))]

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(require_role("viewer"))])
    def get_job(job_id: str):
        row = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        job = dict(row)
        job["runs"] = [dict(r) for r in db.query(
            "SELECT id, stage, attempt, agent_id, status, cost_usd, accounting_precision, "
            "started_at, finished_at FROM runs WHERE job_id=? ORDER BY started_at", (job_id,))]
        job["gates"] = [dict(g) for g in db.query(
            "SELECT g.* FROM gate_results g JOIN runs r ON r.id=g.run_id "
            "WHERE r.job_id=? ORDER BY g.at", (job_id,))]
        return job

    @app.post("/api/jobs/{job_id}/approve")
    async def approve_job(job_id: str, a: ApproveIn,
                          auth: Auth = Depends(require_role("operator"))):
        # async def: resuming the job driver needs the main event loop
        try:
            return orch.approve(job_id, a.approved, a.comment, user=auth.name)
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
            "accounting_precision, started_at, finished_at FROM runs "
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

    @app.get("/api/audit", dependencies=[Depends(require_role("viewer"))])
    def audit_log(limit: int = 100):
        return [dict(r) for r in db.query(
            "SELECT at, actor, action, target_type, target_id, detail_json "
            "FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]

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
        running = {ch.channel_id for ch in app.state.channels}
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
            row["status"] = ("polling" if row["id"] in running
                             else "credential_error" if credential == "error"
                             else "restart_needed" if row["enabled"] else "disabled")
        return rows

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

    return app
