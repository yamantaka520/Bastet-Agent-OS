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

from fastapi import Depends, FastAPI, HTTPException, Request
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
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "host.docker.internal"}


def _host_ok(value: str) -> bool:
    host = value.split(":")[0].lower() if not value.startswith("[") else value.rsplit(":", 1)[0].lower()
    return host in ALLOWED_HOSTS


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


def create_app(home: Home) -> FastAPI:
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

    app = FastAPI(title="Bastet Agent OS", version="0.0.1.dev0", docs_url=None, redoc_url=None)
    app.state.db = db
    app.state.orchestrator = orch

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
        if not _host_ok(request.headers.get("host", "")):
            return JSONResponse({"error": "bad host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            if not _host_ok(urlparse(origin).netloc):
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
        db.write(
            "INSERT INTO agents(id, amos_agent_id, name, executor_type, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (a.id, amos_id, a.name, a.executor_type, ts, ts),
        )
        db.audit(auth.actor, "agent.create", "agent", a.id, {"executor": a.executor_type})
        return {"id": a.id}

    @app.get("/api/agents", dependencies=[Depends(require_role("viewer"))])
    def list_agents():
        return [dict(r) for r in db.query("SELECT * FROM agents ORDER BY created_at")]

    @app.post("/api/resources")
    def create_resource(r: ResourceIn, auth: Auth = Depends(require_role("admin"))):
        try:
            secrets_store.reject_secrets_in_config(r.config)
        except secrets_store.SecretError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        rid = new_id("res")
        ts = now()
        db.write(
            "INSERT INTO resources(id, kind, name, endpoint, api_flavor, secret_ref, "
            "config_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, r.kind, r.name, r.endpoint, r.api_flavor, r.secret_ref,
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
        return [dict(r) for r in db.query("SELECT * FROM workflow_templates ORDER BY id")]

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

    # ---- WebSocket event stream (SPEC §5.10) --------------------------------
    # Browser WebSocket clients can't set Authorization headers, so the first
    # message must be {"token": "<api token>"} — never put the token in the URL.

    from fastapi import WebSocket, WebSocketDisconnect

    @app.websocket("/api/ws")
    async def events_ws(ws: WebSocket):
        if not _host_ok(ws.headers.get("host", "")):
            await ws.close(code=4403)
            return
        origin = ws.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            if not _host_ok(urlparse(origin).netloc):
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
