"""`bastet` CLI — thin client over the control-plane API (SPEC §4)."""

from __future__ import annotations

import json

import httpx
import typer

from .config import Home

app = typer.Typer(help="Bastet Agent OS — local-first control plane for agent teams.")
resource_app = typer.Typer(help="Manage resources (LLM endpoints, secrets, …).")
project_app = typer.Typer(help="Manage projects (1:1 with AMOS projects).")
agent_app = typer.Typer(help="Manage agents (executor bindings).")
grant_app = typer.Typer(help="Manage grants (who may use which resource).")
app.add_typer(resource_app, name="resource")
app.add_typer(project_app, name="project")
app.add_typer(agent_app, name="agent")
app.add_typer(grant_app, name="grant")


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
def serve(host: str = "127.0.0.1", port: int = 0):
    """Run the control plane + gateway (binds 127.0.0.1 only by default)."""
    import uvicorn

    from .server import create_app

    home = Home()
    home.ensure()
    cfg = home.config()
    port = port or cfg.get("port", 8890)
    if host not in ("127.0.0.1", "::1", "localhost"):
        typer.echo("WARNING: binding beyond localhost exposes dispatch (= shell "
                   "execution) and your LLM budgets to the network.", err=True)
    uvicorn.run(create_app(home), host=host, port=port, log_level="info")


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
    title: str = typer.Option(""),
    timeout: int = typer.Option(3600),
    no_worktree: bool = typer.Option(False, help="run directly in the project repo"),
):
    """Dispatch a task to an agent (M1: single-stage job, gate: auto)."""
    _print(_call("POST", "/api/dispatch",
                 {"project_id": project_id, "prompt": prompt, "title": title,
                  "agent_id": agent, "resource_id": resource, "timeout_s": timeout,
                  "use_worktree": not no_worktree}))


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


if __name__ == "__main__":
    app()
