"""Isolated production deployment and live-provider receipt rehearsal."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import threading
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Home
from .db import Db, now
from .delivery_rehearsal import _git, _seed_repo
from .executors.base import RunResult, TaskSpec, register_builtin


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return None


@dataclass
class _Handle:
    task: TaskSpec

    def state(self) -> dict[str, str]:
        return {"kind": "production-rehearsal", "workdir": self.task.workdir}


@register_builtin
class _ProductionRehearsalExecutor:
    kind = "production-rehearsal"
    capabilities = {"code"}

    async def start(self, task: TaskSpec) -> _Handle:
        version = task.prompt.rsplit("version=", 1)[-1].strip()
        workdir = Path(task.workdir)
        (workdir / "package.json").write_text(json.dumps({
            "name": "production-canary",
            "version": version,
        }))
        (workdir / "release.txt").write_text(f"release {version}\n")
        return _Handle(task)

    async def stream(self, handle: _Handle):
        if False:
            yield None

    async def respond(self, handle, request_id, reply) -> None:
        return None

    async def cancel(self, handle) -> None:
        return None

    async def result(self, handle: _Handle) -> RunResult:
        return RunResult(status="succeeded", summary="production canary built")


def _python_command(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _profile(target: str, provider_file: Path, *, deploy: bool) -> dict[str, str]:
    deploy_script = (
        "import json,os,pathlib;"
        "payload={'status':'verified',"
        "'commit_sha':os.environ['BASTET_DELIVERY_COMMIT'],"
        "'version':os.environ['BASTET_DELIVERY_VERSION'],"
        "'target':os.environ['BASTET_DELIVERY_TARGET']};"
        f"pathlib.Path({str(provider_file)!r}).write_text(json.dumps(payload))"
    )
    verify_script = (
        "import urllib.request;"
        f"print(urllib.request.urlopen({target!r},timeout=5).read().decode())"
    )
    return {
        "target_branch": "main",
        "target": target,
        "predeploy_command": "test -f package.json && test -f release.txt",
        "deploy_command": _python_command(deploy_script) if deploy else "true",
        "verify_command": _python_command(verify_script),
    }


async def run(root: str | Path) -> dict[str, Any]:
    """Prove a real tag/deploy/HTTP-readback and a rejected stale provider."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    repo, remote, _ = _seed_repo(root)
    provider = root / "provider"
    provider.mkdir()
    live_file = provider / "live.json"
    handler = partial(_QuietHandler, directory=str(provider))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    target = f"http://127.0.0.1:{server.server_port}/live.json"

    home = Home(root / "home")
    home.ensure()
    db = Db(home.db_path)
    try:
        stamp = now()
        config = {"delivery_profile": _profile(target, live_file, deploy=True)}
        db.write_many([
            ("INSERT INTO projects(id,team_id,repo_path,status,config_json,created_at,"
             "updated_at) VALUES('production','team',?,'running',?,?,?)",
             (str(repo), json.dumps(config), stamp, stamp)),
            ("INSERT INTO agents(id,amos_agent_id,name,executor_type,config_json,"
             "created_at,updated_at) VALUES('builder','builder','Builder',"
             "'production-rehearsal','{}',?,?)", (stamp, stamp)),
            ("INSERT INTO workflow_templates(id,name,version,stages_json) "
             "VALUES('production-canary','Production canary',1,?)",
             (json.dumps([{"name": "release", "gate": "auto",
                           "delivery_modes": ["production"]}]),)),
        ])

        from .orchestrator import DispatchRequest, Orchestrator
        from .pricing import PriceBook
        orch = Orchestrator(db, home, PriceBook(), "http://127.0.0.1:0")

        def dispatch(version: str) -> str:
            return orch.dispatch(DispatchRequest(
                project_id="production", prompt=f"build canary version={version}",
                title=f"production canary v{version}", agent_id="builder",
                template_id="production-canary", use_worktree=True,
                delivery={"mode": "production", "version": version}),
                actor="rehearsal")

        success_job = dispatch("1.4.0")
        await orch.wait_idle()
        success = db.one("SELECT status,delivery_status FROM jobs WHERE id=?",
                         (success_job,))
        success_delivery = db.one(
            "SELECT * FROM deliveries WHERE job_id=? ORDER BY rowid DESC",
            (success_job,))
        if success is None or dict(success) != {
                "status": "done", "delivery_status": "succeeded"}:
            raise RuntimeError(f"production canary did not deploy: {dict(success or {})}")
        success_evidence = json.loads(success_delivery["evidence_json"] or "{}")
        live = json.loads(live_file.read_text())
        if success_evidence.get("verification_receipt") != live:
            raise RuntimeError("provider readback does not match delivery evidence")

        _git(repo, "fetch", "-q", "origin", "main")
        _git(repo, "merge", "--ff-only", "FETCH_HEAD")
        project = db.one("SELECT config_json FROM projects WHERE id='production'")
        project_config = json.loads(project["config_json"])
        project_config["delivery_profile"] = _profile(target, live_file, deploy=False)
        db.write("UPDATE projects SET config_json=?,updated_at=? WHERE id='production'",
                 (json.dumps(project_config), now()))

        stale_job = dispatch("1.4.1")
        await orch.wait_idle()
        stale = db.one("SELECT status,delivery_status FROM jobs WHERE id=?", (stale_job,))
        stale_delivery = db.one(
            "SELECT status,error FROM deliveries WHERE job_id=? ORDER BY rowid DESC",
            (stale_job,))
        if stale is None or dict(stale) != {
                "status": "blocked", "delivery_status": "failed"}:
            raise RuntimeError(f"stale provider was not blocked: {dict(stale or {})}")
        if "online deployment receipt mismatch" not in (stale_delivery["error"] or ""):
            raise RuntimeError("stale provider failure lacks receipt mismatch evidence")
        if json.loads(live_file.read_text()) != live:
            raise RuntimeError("stale canary unexpectedly changed live provider state")

        remote_main = _git(remote, "rev-parse", "refs/heads/main")
        tags = _git(remote, "tag", "--list").splitlines()
        deployed = db.one(
            "SELECT COUNT(*) AS n FROM audit_log WHERE action='job.deployed' "
            "AND target_id=?", (success_job,))["n"]
        stale_deployed = db.one(
            "SELECT COUNT(*) AS n FROM audit_log WHERE action='job.deployed' "
            "AND target_id=?", (stale_job,))["n"]
        if deployed != 1 or stale_deployed != 0:
            raise RuntimeError("job.deployed audit boundary was violated")
        return {
            "ok": True,
            "provider": {"target": target, "live_receipt": live},
            "success": {"job_id": success_job, "status": success["status"],
                        "delivery_status": success["delivery_status"],
                        "commit_sha": success_delivery["commit_sha"]},
            "stale_canary": {"job_id": stale_job, "status": stale["status"],
                             "delivery_status": stale["delivery_status"],
                             "blocked_by_receipt": True},
            "git": {"remote_main": remote_main, "tags": tags},
        }
    finally:
        db.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="bastet-production-") as directory:
        print(json.dumps(asyncio.run(run(directory)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
