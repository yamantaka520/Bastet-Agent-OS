"""Running a vendor's install command for a pool resource (MCP servers, skills).

Vendors ship one-liners (`npx -y @scope/server`, `pip install …`, `uv tool
install …`). We keep the command with the resource, run it on demand, and store
the full output — a failed install is the normal case (missing node, wrong
package name), and the operator needs the log to fix the command and retry.

This is arbitrary shell execution, so it is admin-only, audited, and never
implicit: nothing installs itself on create, dispatch or startup.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from .db import now

LOG_LIMIT = 8000        # keep enough to debug, not enough to bloat the row
DEFAULT_TIMEOUT_S = 600


def state_of(config: dict[str, Any]) -> dict[str, Any]:
    """Install state as the UI sees it (never None: 'absent' is a state)."""
    install = config.get("install") or {}
    return {"status": install.get("status", "absent"),
            "at": install.get("at"), "exit_code": install.get("exit_code"),
            "command": config.get("install_command", ""),
            "log": install.get("log", ""), "digest": install.get("digest", ""),
            "target": install.get("target", ""),
            "version": install.get("version", "")}


def run(db, resource_id: str, actor: str, timeout_s: int = DEFAULT_TIMEOUT_S,
        cwd: str | None = None) -> dict[str, Any]:
    """Execute the resource's install command; record status + log. Raises
    ValueError when there is no command to run."""
    row = db.one("SELECT * FROM resources WHERE id=?", (resource_id,))
    if row is None:
        raise ValueError("resource not found")
    config = json.loads(row["config_json"] or "{}")
    command = (config.get("install_command") or "").strip()
    if not command:
        raise ValueError("this resource has no install command")

    db.audit(actor, "resource.install.start", "resource", resource_id,
             {"command": command})
    try:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True,
                              timeout=timeout_s, cwd=cwd)
        output = (proc.stdout or "") + (proc.stderr or "")
        exit_code: int | None = proc.returncode
        status = "installed" if proc.returncode == 0 else "failed"
    except subprocess.TimeoutExpired as exc:
        output = f"timed out after {timeout_s}s\n{exc.stdout or ''}{exc.stderr or ''}"
        exit_code, status = None, "failed"
    except OSError as exc:                       # no shell, permissions, …
        output, exit_code, status = f"{type(exc).__name__}: {exc}", None, "failed"

    extra: dict[str, Any] = {}
    if row["kind"] == "skill" and status == "installed":
        # Installation is not success until the target, digest and optional
        # health command have been verified through the same probe used later.
        from .resource_test import _test_skill
        from .skill_supply import effective_path
        health = _test_skill(config)
        config["test"] = {**health, "at": now()}
        path = effective_path(config)
        extra = {"digest": health.get("digest", ""),
                 "target": str(path) if path else "",
                 "version": config.get("skill_version", "")}
        if health["status"] != "ok":
            status = "failed"
            output += f"\npost-install verification failed: {health['detail']}"
    config["install"] = {"status": status, "at": now(), "exit_code": exit_code,
                         "log": output[-LOG_LIMIT:], **extra}
    db.write("UPDATE resources SET config_json=?, updated_at=? WHERE id=?",
             (json.dumps(config), now(), resource_id))
    db.audit(actor, f"resource.install.{status}", "resource", resource_id,
             {"exit_code": exit_code, "log_tail": output[-500:]})
    return state_of(config)
