"""Resource-pool taxonomy: what kinds exist and what each one needs.

One catalog drives three things that used to drift apart: the WebUI form (which
fields to show), the API validation (what must be present), and run-time access
(how an agent actually calls the thing). Adding a kind means adding a row here
plus i18n labels — no new endpoints.

Field ids are UI hints; values live in `resources.config_json` except the four
promoted columns (endpoint, api_flavor, secret_ref, name).
"""

from __future__ import annotations

from typing import Any

# groups order = display order in the UI
GROUPS = ["model", "tool", "asset", "media"]

KINDS: list[dict[str, Any]] = [
    {"id": "llm", "group": "model", "auth": "required",
     "fields": ["endpoint", "api_flavor", "default_model", "secret"]},
    {"id": "mcp", "group": "tool", "auth": "optional",
     "fields": ["mcp_transport", "mcp_command", "mcp_url", "mcp_secret_env",
                "secret", "install_command"]},
    {"id": "api", "group": "tool", "auth": "required",
     "fields": ["endpoint", "auth_header", "secret"]},
    {"id": "skill", "group": "asset", "auth": "none",
     "fields": ["skill_source", "install_command"]},
    {"id": "git", "group": "asset", "auth": "optional",
     "fields": ["git_provider", "endpoint", "secret"]},
    {"id": "image", "group": "media", "auth": "required",
     "fields": ["endpoint", "default_model", "secret"]},
    {"id": "video", "group": "media", "auth": "required",
     "fields": ["endpoint", "default_model", "secret"]},
    {"id": "music", "group": "media", "auth": "required",
     "fields": ["endpoint", "default_model", "secret"]},
    {"id": "tts", "group": "media", "auth": "required",
     "fields": ["endpoint", "default_model", "secret"]},
    {"id": "stt", "group": "media", "auth": "required",
     "fields": ["endpoint", "default_model", "secret"]},
]

BY_ID = {k["id"]: k for k in KINDS}

# config keys that are plain strings (validated, stored verbatim)
CONFIG_FIELDS = {"default_model", "mcp_transport", "mcp_command", "mcp_url",
                 "mcp_secret_env", "install_command", "skill_source",
                 "git_provider", "auth_header"}

MCP_TRANSPORTS = ("stdio", "http")
GIT_PROVIDERS = ("github", "gitlab", "custom")


def catalog() -> dict[str, Any]:
    """Everything the UI needs to render the pool: kinds, groups, enums."""
    return {"groups": GROUPS, "kinds": KINDS,
            "config_fields": sorted(CONFIG_FIELDS),
            "enums": {"mcp_transport": list(MCP_TRANSPORTS),
                      "git_provider": list(GIT_PROVIDERS)}}


def slug(name: str) -> str:
    """Resource name → env-var-safe token (stable: agents rely on these)."""
    out = "".join(ch.upper() if ch.isalnum() else "_" for ch in name).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out[:40] or "RES"


def env_prefix(name: str) -> str:
    return f"BASTET_RES_{slug(name)}"


def validate(kind: str, endpoint: str | None, secret_ref: str | None,
             config: dict[str, Any]) -> list[str]:
    """Return human-readable problems; empty list means the resource is usable.

    Deliberately permissive about *extra* config (a vendor may need odd keys)
    and strict about what run-time access depends on."""
    spec = BY_ID.get(kind)
    if spec is None:
        return [f"unknown kind {kind!r}"]
    problems: list[str] = []
    if spec["auth"] == "required" and not secret_ref:
        problems.append("credential-required")
    if kind == "mcp":
        transport = config.get("mcp_transport") or "stdio"
        if transport not in MCP_TRANSPORTS:
            problems.append("mcp-transport-invalid")
        if transport == "stdio" and not config.get("mcp_command"):
            problems.append("mcp-command-missing")
        if transport == "http" and not (config.get("mcp_url") or endpoint):
            problems.append("mcp-url-missing")
    if kind == "api" and not endpoint:
        problems.append("endpoint-missing")
    if kind == "skill" and not config.get("skill_source"):
        problems.append("skill-source-missing")
    if kind == "git":
        provider = config.get("git_provider") or "custom"
        if provider not in GIT_PROVIDERS:
            problems.append("git-provider-invalid")
        if provider == "custom" and not endpoint:
            problems.append("endpoint-missing")
    return problems
