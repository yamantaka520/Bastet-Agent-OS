"""`bastet` CLI — thin client over the control-plane API (SPEC §4)."""

from __future__ import annotations

import json
import time

import httpx
import typer

from .config import Home

app = typer.Typer(help="Bastet Agent OS — local-first control plane for agent teams.")
resource_app = typer.Typer(help="Manage resources (LLM endpoints, secrets, …).")
project_app = typer.Typer(help="Manage projects (1:1 with AMOS projects).")
agent_app = typer.Typer(help="Manage agents (executor bindings).")
grant_app = typer.Typer(help="Manage grants (who may use which resource).")
maintenance_app = typer.Typer(help="Drain dispatch safely for upgrades.")
@app.callback()
def _prepare() -> None:
    """Every command sees the same PATH the service gives its subprocesses.

    Without this `bastet doctor` reported a gate tool as missing while the very
    same runner sat in Bastet's own venv — the report has to match reality."""
    from .config import augment_path

    augment_path()


app.add_typer(resource_app, name="resource")
app.add_typer(project_app, name="project")
app.add_typer(agent_app, name="agent")
app.add_typer(grant_app, name="grant")
app.add_typer(maintenance_app, name="maintenance")


@maintenance_app.command("status")
def maintenance_status():
    """Show the durable dispatch fence and live drain counts."""
    _print(_call("GET", "/api/maintenance/state"))


@maintenance_app.command("enter")
def maintenance_enter(
    reason: str = typer.Option("planned upgrade", help="Audit reason."),
    wait: bool = typer.Option(False, "--wait", help="Wait until jobs and runs drain."),
    poll: float = typer.Option(2.0, min=0.2, help="Polling interval in seconds."),
):
    """Fence new dispatch/retries, optionally waiting for a safe restart point."""
    current = _call("POST", "/api/maintenance/enter", {"reason": reason})
    while wait and not current["drained"]:
        typer.echo(f"draining: {current['active_jobs']} jobs, "
                   f"{current['active_runs']} runs")
        time.sleep(poll)
        current = _call("GET", "/api/maintenance/state")
    _print(current)


@maintenance_app.command("leave")
def maintenance_leave():
    """Open the dispatch fence after the upgrade is verified."""
    _print(_call("POST", "/api/maintenance/leave", {}))


def _client() -> httpx.Client:
    home = Home()
    if not home.token_path.exists():
        typer.echo("Bastet home not initialized — run `bastet init` first.", err=True)
        raise typer.Exit(1)
    return httpx.Client(
        base_url=home.server_url(),
        headers={"Authorization": f"Bearer {home.api_token()}"},
        timeout=30,
    )


def _call(method: str, path: str, payload: dict | None = None) -> dict | list:
    with _client() as client:
        try:
            resp = client.request(method, path, json=payload)
        except httpx.ConnectError:
            typer.echo("Cannot reach the Bastet server — is `bastet serve` running?", err=True)
            raise typer.Exit(1) from None
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail") or resp.json().get("error")
        except json.JSONDecodeError:
            detail = resp.text[:200]
        typer.echo(f"error ({resp.status_code}): {detail}", err=True)
        raise typer.Exit(1)
    return resp.json()


def _print(data) -> None:
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


@app.command()
def init():
    """Initialize ~/.bastet (database, API token, config)."""
    home = Home()
    home.ensure()
    from .db import Db

    Db(home.db_path).close()
    typer.echo(f"initialized {home.root}")


@app.command()
def serve(host: str = typer.Option("", help="bind address; default from "
                                   "config.json host, else 127.0.0.1"),
          port: int = 0):
    """Run the control plane + gateway (default bind: 127.0.0.1)."""
    import logging

    import uvicorn

    from .server import create_app

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s: %(name)s: %(message)s")
    # httpx logs full request URLs at INFO — Telegram bot tokens live in the
    # URL path, so that logger must never speak below WARNING (SPEC §5.8)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    home = Home()
    home.ensure()
    cfg = home.config()
    host = host or cfg.get("host") or "127.0.0.1"
    port = port or cfg.get("port", 8890)
    if host not in ("127.0.0.1", "::1", "localhost"):
        typer.echo("NOTE: binding beyond localhost — every request still needs the "
                   "API token, and Host/Origin validation only admits this "
                   "machine's own addresses (plus config allowed_hosts).", err=True)
        # the app reads cfg[\"host\"] to widen its Host allow-list; keep them in sync
        if cfg.get("host") != host:
            import json as _json
            cfg["host"] = host
            home.config_path.write_text(_json.dumps(cfg, indent=2))
    # Uvicorn waits for open WebSockets before entering application lifespan
    # shutdown. A dashboard tab can therefore hold the service forever unless
    # the server owns a bounded graceful-connection window.
    uvicorn.run(create_app(home), host=host, port=port, log_level="info",
                timeout_graceful_shutdown=5)


@project_app.command("add")
def project_add(project_id: str, repo_path: str, team_id: str = typer.Option(None)):
    _print(_call("POST", "/api/projects",
                 {"id": project_id, "repo_path": repo_path, "team_id": team_id}))


@project_app.command("list")
def project_list():
    _print(_call("GET", "/api/projects"))


@agent_app.command("add")
def agent_add(agent_id: str, name: str = "", executor: str = "claude-code"):
    _print(_call("POST", "/api/agents",
                 {"id": agent_id, "name": name or agent_id, "executor_type": executor}))


@agent_app.command("list")
def agent_list():
    _print(_call("GET", "/api/agents"))


@resource_app.command("add")
def resource_add(
    name: str,
    endpoint: str = typer.Option(None, help="Base URL, e.g. https://api.anthropic.com"),
    flavor: str = typer.Option("anthropic", help="openai|anthropic"),
    secret_ref: str = typer.Option(None, help="keyring:svc/name | file:/path | env:NAME"),
    kind: str = "llm",
):
    _print(_call("POST", "/api/resources",
                 {"name": name, "kind": kind, "endpoint": endpoint,
                  "api_flavor": flavor, "secret_ref": secret_ref}))


@resource_app.command("list")
def resource_list():
    _print(_call("GET", "/api/resources"))


@grant_app.command("add")
def grant_add(
    resource_id: str,
    scope: str = typer.Argument(..., help="team:<id> | project:<id> | agent:<id>"),
    budget_usd: float = typer.Option(None),
    max_concurrency: int = typer.Option(None),
    period: str = "lifetime",
    on_exceed: str = "block",
):
    scope_type, _, scope_id = scope.partition(":")
    if scope_type not in ("team", "project", "agent") or not scope_id:
        typer.echo("scope must be team:<id> | project:<id> | agent:<id>", err=True)
        raise typer.Exit(1)
    _print(_call("POST", "/api/grants",
                 {"resource_id": resource_id, "scope_type": scope_type, "scope_id": scope_id,
                  "budget_usd": budget_usd, "max_concurrency": max_concurrency,
                  "period": period, "on_exceed": on_exceed}))


@grant_app.command("list")
def grant_list():
    _print(_call("GET", "/api/grants"))


@app.command()
def dispatch(
    project_id: str,
    prompt: str,
    agent: str = typer.Option(..., "--agent", "-a"),
    resource: str = typer.Option(None, "--resource", "-r",
                                 help="LLM resource id (omit for subscription/direct path)"),
    template: str = typer.Option(None, "--template", "-t",
                                 help="workflow template id (omit for single-stage)"),
    title: str = typer.Option(""),
    timeout: int = typer.Option(3600),
    no_worktree: bool = typer.Option(False, help="run directly in the project repo"),
):
    """Dispatch a task: creates a job on a workflow template and drives it."""
    _print(_call("POST", "/api/dispatch",
                 {"project_id": project_id, "prompt": prompt, "title": title,
                  "agent_id": agent, "resource_id": resource, "template_id": template,
                  "timeout_s": timeout, "use_worktree": not no_worktree}))


template_app = typer.Typer(help="Manage workflow templates (stage pipelines).")
app.add_typer(template_app, name="template")


@template_app.command("add")
def template_add(file: str):
    """Add/replace a template from a YAML or JSON file (see SPEC §5.4.1)."""
    from .workflow import load_template_file

    name, stages = load_template_file(file)
    _print(_call("POST", "/api/templates",
                 {"name": name, "stages": [s.to_dict() for s in stages]}))


@template_app.command("list")
def template_list():
    _print(_call("GET", "/api/templates"))


@app.command("role-assign")
def role_assign(project_id: str, agent_id: str, role: str,
                preference: int = typer.Option(0)):
    """Assign a role to an agent within a project (drives stage matching)."""
    _print(_call("POST", "/api/roles",
                 {"project_id": project_id, "agent_id": agent_id, "role": role,
                  "preference": preference}))


@app.command()
def jobs(project_id: str = typer.Option(None), limit: int = 20):
    path = "/api/jobs" + (f"?project_id={project_id}&limit={limit}" if project_id
                          else f"?limit={limit}")
    _print(_call("GET", path))


@app.command("job")
def job_show(job_id: str):
    """Show a job: stages, runs, gate results."""
    _print(_call("GET", f"/api/jobs/{job_id}"))


@app.command("job-revalidate")
def job_revalidate(job_id: str):
    """Re-run a blocked deterministic gate without re-running its Agent."""
    _print(_call("POST", f"/api/jobs/{job_id}/revalidate", {}))


user_app = typer.Typer(help="Manage users (multi-user auth; admin only).")
app.add_typer(user_app, name="user")


@user_app.command("add")
def user_add(name: str, role: str = typer.Option("operator", help="viewer|operator|admin")):
    """Create a user; the token is printed ONCE — store it safely."""
    _print(_call("POST", "/api/users", {"name": name, "role": role}))


@user_app.command("list")
def user_list():
    _print(_call("GET", "/api/users"))


@user_app.command("disable")
def user_disable(user_id: str):
    _print(_call("POST", f"/api/users/{user_id}/enabled", {"enabled": False}))


@user_app.command("enable")
def user_enable(user_id: str):
    _print(_call("POST", f"/api/users/{user_id}/enabled", {"enabled": True}))


@app.command()
def whoami():
    _print(_call("GET", "/api/me"))


channel_app = typer.Typer(help="Chat channels (Telegram first; admin only).")
app.add_typer(channel_app, name="channel")


@channel_app.command("add")
def channel_add(kind: str = typer.Argument("telegram"),
                secret_ref: str = typer.Option(..., help="bot token ref, e.g. keyring:bastet/tg-bot")):
    _print(_call("POST", "/api/channels", {"kind": kind, "secret_ref": secret_ref}))


@channel_app.command("list")
def channel_list():
    _print(_call("GET", "/api/channels"))


@channel_app.command("pair")
def channel_pair(channel_id: str, user_id: str = typer.Option(None,
                 help="bastet user to bind (default: you)")):
    """Generate a one-time pairing code; send `/pair <code>` to the bot."""
    _print(_call("POST", f"/api/channels/{channel_id}/pair", {"user_id": user_id}))


@app.command()
def approve(job_id: str,
            reject: bool = typer.Option(False, help="reject instead of approve"),
            comment: str = typer.Option("")):
    """Decide a human-approve gate the job is waiting on."""
    _print(_call("POST", f"/api/jobs/{job_id}/approve",
                 {"approved": not reject, "comment": comment}))


@app.command("run")
def run_show(run_id: str):
    """Show a run: status, usage ledger, artifacts."""
    _print(_call("GET", f"/api/runs/{run_id}"))


@app.command()
def runs(limit: int = 20):
    _print(_call("GET", f"/api/runs?limit={limit}"))


@app.command()
def usage(project_id: str = typer.Option(None)):
    """Usage & cost by project/agent/precision."""
    path = "/api/usage" + (f"?project_id={project_id}" if project_id else "")
    _print(_call("GET", path))


@app.command()
def audit(limit: int = 50):
    _print(_call("GET", f"/api/audit?limit={limit}"))


service_app = typer.Typer(help="開機/登入自動啟動：systemd (Linux)、launchd (macOS)、"
                               "工作排程器 (Windows)，皆含自動重啟。")
app.add_typer(service_app, name="service")


@service_app.command("install")
def service_install():
    """Install + start the auto-restart service for `bastet serve`."""
    from . import service

    try:
        typer.echo(service.install())
    except RuntimeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


@service_app.command("uninstall")
def service_uninstall():
    from . import service

    typer.echo(service.uninstall())


@service_app.command("status")
def service_status():
    from . import service

    typer.echo(service.status())


@app.command()
def gc():
    """Sweep worktrees left behind by finished jobs (branches survive)."""
    _print(_call("POST", "/api/gc"))


@app.command("reliability-rehearsal")
def reliability_rehearsal():
    """Run isolated two-process dispatch/claim/kill-restart acceptance checks."""
    import tempfile

    from .reliability_rehearsal import run

    with tempfile.TemporaryDirectory(prefix="bastet-reliability-") as directory:
        try:
            _print(run(directory))
        except Exception as exc:
            typer.echo(f"reliability rehearsal failed: {type(exc).__name__}: {exc}",
                       err=True)
            raise typer.Exit(1) from exc


@app.command("delivery-rehearsal")
def delivery_rehearsal():
    """Prove parallel DAG join and remote-main integration in temporary Git repos."""
    import asyncio
    import tempfile

    from .delivery_rehearsal import run

    with tempfile.TemporaryDirectory(prefix="bastet-delivery-") as directory:
        try:
            _print(asyncio.run(run(directory)))
        except Exception as exc:
            typer.echo(f"delivery rehearsal failed: {type(exc).__name__}: {exc}", err=True)
            raise typer.Exit(1) from exc


@app.command("production-rehearsal")
def production_rehearsal():
    """Prove production tagging, deployment and HTTP provider receipts."""
    import asyncio
    import tempfile

    from .production_rehearsal import run

    with tempfile.TemporaryDirectory(prefix="bastet-production-") as directory:
        try:
            _print(asyncio.run(run(directory)))
        except Exception as exc:
            typer.echo(f"production rehearsal failed: {type(exc).__name__}: {exc}",
                       err=True)
            raise typer.Exit(1) from exc


@app.command("store-canary")
def store_canary(
    project_id: str = typer.Option("", "--project", help="Project for supplied receipt."),
    job_id: str = typer.Option("", "--job", help="Use a frozen job delivery receipt."),
    submission: str = typer.Option(
        "", "--submission", help="Existing uploader submission-receipt JSON file."),
):
    """Read one exact Apple/Google release object without publishing changes."""
    from .store_canary import StoreCanaryError, run

    if submission and not project_id:
        typer.echo("store canary failed: --project is required with --submission", err=True)
        raise typer.Exit(1)
    try:
        _print(run(Home().root, project_id=project_id, job_id=job_id,
                   submission_file=submission or None))
    except StoreCanaryError as exc:
        typer.echo(f"store canary failed: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("pricing-update")
def pricing_update():
    """Refresh the local model price table from the public LiteLLM JSON."""
    from .pricing import PRICES_URL

    home = Home()
    home.ensure()
    typer.echo(f"fetching {PRICES_URL} …")
    resp = httpx.get(PRICES_URL, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    (home.root / "model_prices.json").write_text(resp.text)
    typer.echo(f"saved {len(resp.text)//1024} KiB to {home.root / 'model_prices.json'}")


@app.command()
def doctor():
    """Health checks: home, DB, audit chain, secrets hygiene, executor deps."""
    import shutil
    import stat
    import subprocess

    from . import secrets_store
    from .db import Db

    home = Home()
    problems = 0

    def ok(msg: str) -> None:
        typer.echo(f"  ✓ {msg}")

    def bad(msg: str) -> None:
        nonlocal problems
        problems += 1
        typer.echo(f"  ✗ {msg}")

    typer.echo(f"home: {home.root}")
    if not home.root.exists():
        bad("home missing — run `bastet init`")
        raise typer.Exit(1)
    ok("home exists")

    if home.token_path.exists():
        mode = stat.S_IMODE(home.token_path.stat().st_mode)
        if mode & 0o077:
            bad(f"api_token is group/world readable (mode {oct(mode)}); chmod 600 it")
        else:
            ok("api_token permissions are 0600")
    else:
        bad("api_token missing — run `bastet init`")

    from .config import gate_tools

    for tool in gate_tools():
        if tool["path"]:
            ok(f"gate tool `{tool['program']}` → {tool['path']}")
        else:
            bad(f"gate tool `{tool['program']}` not found — "
                f"{', '.join(tool['used_by'][:3])} 的測試關卡會失敗"
                f"（裝起來，或把該階段的指令改成專案真的有的）")

    try:
        db = Db(home.db_path)
        ok("database opens (WAL, foreign_keys=ON)")
        if db.verify_audit_chain():
            ok("audit hash chain verifies")
        else:
            bad("audit hash chain BROKEN — log was modified out-of-band")
        smuggled = 0
        for row in db.query("SELECT id, config_json FROM resources"):
            try:
                secrets_store.reject_secrets_in_config(json.loads(row["config_json"]))
            except secrets_store.SecretError:
                smuggled += 1
                bad(f"resource {row['id']} has secret material in config_json")
        if not smuggled:
            ok("no secrets smuggled in config_json")
        stale = db.one("SELECT COUNT(*) AS n FROM runs "
                       "WHERE status IN ('queued','running','waiting_input')")
        typer.echo(f"  · active/queued runs: {stale['n']}")
        db.close()
    except Exception as exc:
        bad(f"database check failed: {type(exc).__name__}")

    for tool, why in [("git", "worktrees & diff artifacts"), ("claude", "claude-code executor")]:
        if shutil.which(tool):
            ok(f"{tool} on PATH ({why})")
        else:
            bad(f"{tool} not found on PATH — needed for {why}")

    from .container import docker_available
    if docker_available():
        ok("docker daemon running (container isolation available)")
    else:
        typer.echo("  · docker unavailable — isolation=container runs will fail loudly "
                   "(worktree isolation unaffected)")

    prices = home.root / "model_prices.json"
    if prices.exists():
        ok("local model price table present")
    else:
        typer.echo("  · no local price table (bundled fallback only) — "
                   "run `bastet pricing-update`")

    if shutil.which("claude"):
        proc = subprocess.run(["claude", "-p", "ping", "--output-format", "json"],
                              capture_output=True, text=True, timeout=60)
        if '"is_error":true' in proc.stdout.replace(" ", "") or proc.returncode != 0:
            bad("claude CLI cannot run tasks here (not logged in?) — "
                "dispatches will fail until `claude /login` in this environment")
        else:
            ok("claude CLI is logged in and responding")

    raise typer.Exit(1 if problems else 0)


if __name__ == "__main__":
    app()
