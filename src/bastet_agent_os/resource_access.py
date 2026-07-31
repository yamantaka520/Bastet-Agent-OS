"""Turning granted resources into something an agent can actually call.

A resource in the pool is inert until a run can reach it. At run start we take
every resource whose grant covers this project (project → team → global),
and hand it to the agent three ways:

  env vars     BASTET_RES_<NAME>_URL / _KEY / _TOKEN / _MODEL / _SOURCE
  MCP config   a `mcpServers` JSON file (Claude-Code shape) for kind=mcp
  a manifest   listed in the prompt, so the agent knows what exists

Everything injected must be assumed readable by the agent (SPEC §5.9), so the
MCP file lives outside the worktree in <home>/run-access/<run_id> at 0600 and
is deleted when the run ends.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import secrets_store
from .resource_kinds import env_prefix, slug

log = logging.getLogger("bastet.resources")

MANIFEST_ENV = "BASTET_RESOURCES"
MCP_ENV = "BASTET_MCP_CONFIG"


@dataclass
class RunAccess:
    env: dict[str, str] = field(default_factory=dict)
    mcp_config_path: str | None = None
    manifest: list[dict[str, Any]] = field(default_factory=list)

    @property
    def notes(self) -> str:
        """Prompt block: what is available and how to reach it (no secrets)."""
        if not self.manifest:
            return ""
        lines = ["## 可用資源（由 Bastet 資源池授權，可直接使用）"]
        for item in self.manifest:
            detail = "、".join(item["how"])
            lines.append(f"- **{item['name']}**（{item['kind']}）：{detail}")
        if any("ssh" in " ".join(item["how"]) for item in self.manifest):
            lines.append("SSH repo：金鑰已寫成檔案並設好 GIT_SSH_COMMAND，"
                         "直接 `git clone $BASTET_RES_<名稱>_URL` 即可；"
                         "不要把金鑰內容印出來或複製到別處。")
        if self.mcp_config_path:
            lines.append(f"- MCP 伺服器設定：`{self.mcp_config_path}`"
                         f"（已透過 {MCP_ENV} 提供）")
        lines.append("金鑰只以環境變數提供，請勿寫入檔案或輸出到訊息中。")
        return "\n".join(lines)


def visible(db, project_id: str, team_id: str) -> list[Any]:
    """Resources whose grant covers this project. Credentials (kind=secret)
    ride the separate secret path — they are not callable objects."""
    return list(db.query(
        "SELECT DISTINCT r.*, g.scope_type FROM grants g "
        "JOIN resources r ON r.id = g.resource_id "
        "WHERE r.enabled=1 AND g.enabled=1 AND r.kind != 'secret' AND "
        "(g.scope_type='global' OR (g.scope_type='project' AND g.scope_id=?) "
        " OR (g.scope_type='team' AND g.scope_id=?)) ORDER BY r.kind, r.name",
        (project_id, team_id)))


def _secret_value(db, ref: str) -> str | None:
    if not ref:
        return None
    try:
        return secrets_store.resolve(secrets_store.expand(db, ref))
    except secrets_store.SecretError as exc:
        log.warning("resource secret unresolved: %s", exc)
        return None


def access_dir(home_root: Path | str, run_id: str) -> Path:
    return Path(home_root) / "run-access" / run_id


def cleanup(home_root: Path | str, run_id: str) -> None:
    shutil.rmtree(access_dir(home_root, run_id), ignore_errors=True)


def build(db, home_root: Path | str, project_id: str, team_id: str,
          run_id: str, audit_actor: str = "") -> RunAccess:
    access = RunAccess()
    mcp_servers: dict[str, Any] = {}

    for row in visible(db, project_id, team_id):
        config = json.loads(row["config_json"] or "{}")
        prefix = env_prefix(row["name"])
        kind = row["kind"]
        how: list[str] = []
        secret = _secret_value(db, row["secret_ref"] or "")

        if kind == "git" and _is_ssh_url(row["endpoint"] or ""):
            # SSH repos need a key on disk and a git that will use it; without
            # both, an agent can test fine and still be unable to clone
            how += _expose_git_ssh(access, home_root, run_id, row, secret, prefix)
        elif kind == "mcp":
            server = _mcp_server(row, config, secret)
            if server is None:
                continue
            mcp_servers[slug(row["name"]).lower()] = server
            how.append(f"MCP server `{slug(row['name']).lower()}`")
        else:
            endpoint = row["endpoint"] or config.get("mcp_url") or ""
            if endpoint:
                access.env[f"{prefix}_URL"] = endpoint
                how.append(f"endpoint `${prefix}_URL`")
            if secret:
                var = f"{prefix}_TOKEN" if kind == "git" else f"{prefix}_KEY"
                access.env[var] = secret
                how.append(f"credential `${var}`")
            if config.get("default_model"):
                access.env[f"{prefix}_MODEL"] = config["default_model"]
                how.append(f"model `{config['default_model']}`")
            if config.get("skill_source"):
                access.env[f"{prefix}_SOURCE"] = config["skill_source"]
                how.append(f"source `{config['skill_source']}`")
            if kind == "api" and config.get("auth_header"):
                access.env[f"{prefix}_AUTH_HEADER"] = config["auth_header"]
                how.append(f"auth header `{config['auth_header']}`")

        if not how:
            continue  # nothing usable — do not advertise it to the agent
        access.manifest.append({"id": row["id"], "name": row["name"], "kind": kind,
                                "scope": row["scope_type"], "how": how})
        if audit_actor:
            db.audit(audit_actor, "resource.exposed", "resource", row["id"],
                     {"kind": kind, "project": project_id, "run": run_id,
                      "env": [h for h in how]})

    if mcp_servers:
        directory = access_dir(home_root, run_id)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        path = directory / "mcp.json"
        path.write_text(json.dumps({"mcpServers": mcp_servers}, indent=2))
        os.chmod(path, 0o600)          # holds resolved credentials
        access.mcp_config_path = str(path)
        access.env[MCP_ENV] = str(path)

    if access.manifest:
        access.env[MANIFEST_ENV] = json.dumps(
            [{k: v for k, v in item.items() if k != "id"} for item in access.manifest],
            ensure_ascii=False)
    return access


def _is_ssh_url(url: str) -> bool:
    value = (url or "").strip()
    return value.startswith(("git@", "ssh://")) or (
        ":" in value.split("/")[0] and "@" in value.split(":")[0])


def _expose_git_ssh(access: RunAccess, home_root: Path | str, run_id: str, row,
                    secret: str | None, prefix: str) -> list[str]:
    """Materialise the deploy key and hand the agent a git that uses it.

    `GIT_SSH_COMMAND` is set for the whole run from the first SSH repo so a plain
    `git clone` works; every resource also gets its own `_SSH_COMMAND` so an agent
    juggling two repos can pick deliberately."""
    url = row["endpoint"]
    access.env[f"{prefix}_URL"] = url
    how = [f"repo `${prefix}_URL` (ssh)"]
    if not secret:
        how.append("⚠ no key configured — clone will fail")
        return how
    directory = access_dir(home_root, run_id) / "ssh"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    key_path = directory / f"{slug(row['name']).lower()}.key"
    key_path.write_text(secret if secret.endswith("\n") else secret + "\n")
    os.chmod(key_path, 0o600)          # ssh refuses a world-readable key
    command = (f"ssh -i {key_path} -o IdentitiesOnly=yes "
               f"-o StrictHostKeyChecking=accept-new -o BatchMode=yes")
    access.env[f"{prefix}_SSH_KEY"] = str(key_path)
    access.env[f"{prefix}_SSH_COMMAND"] = command
    access.env.setdefault("GIT_SSH_COMMAND", command)
    how += [f"deploy key `${prefix}_SSH_KEY`", "git over SSH ready (GIT_SSH_COMMAND)"]
    return how


def _mcp_server(row, config: dict[str, Any], secret: str | None) -> dict[str, Any] | None:
    """One entry of the `mcpServers` map, in Claude-Code's shape."""
    transport = config.get("mcp_transport") or "stdio"
    if transport == "http":
        url = config.get("mcp_url") or row["endpoint"]
        if not url:
            return None
        server: dict[str, Any] = {"type": "http", "url": url}
        if secret:
            header = config.get("auth_header") or "Authorization"
            value = secret if secret.lower().startswith("bearer ") else f"Bearer {secret}"
            server["headers"] = {header: value}
        return server
    command = (config.get("mcp_command") or "").strip()
    if not command:
        return None
    import shlex

    parts = shlex.split(command)
    server = {"command": parts[0], "args": parts[1:]}
    if secret:
        # the vendor decides the variable name (GITHUB_TOKEN, BRAVE_API_KEY, …)
        server["env"] = {config.get("mcp_secret_env") or "API_KEY": secret}
    return server
