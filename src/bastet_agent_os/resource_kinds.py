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

MEDIA_ASYNC_FIELDS = [
    "async_status_path", "async_status_field", "async_success_values",
    "async_failure_values", "async_result_url_field", "async_download_hosts",
    "async_poll_interval_seconds", "async_max_attempts", "async_max_bytes",
]

KINDS: list[dict[str, Any]] = [
    {"id": "llm", "group": "model", "auth": "required",
     "fields": ["endpoint", "api_flavor", "default_model", "secret"]},
    {"id": "mcp", "group": "tool", "auth": "optional",
     "fields": ["mcp_transport", "mcp_command", "mcp_url", "mcp_secret_env",
                "secret", "install_command"]},
    {"id": "api", "group": "tool", "auth": "required",
     "fields": ["endpoint", "auth_header", "secret"]},
    {"id": "skill", "group": "asset", "auth": "none",
     "fields": ["skill_id", "skill_version", "skill_source", "skill_target",
                "skill_digest", "compatible_executors", "install_command",
                "health_command"]},
    {"id": "git", "group": "asset", "auth": "optional",
     "fields": ["git_provider", "endpoint", "secret"]},
    {"id": "image", "group": "media", "auth": "required",
     "fields": ["endpoint", "default_model", "secret", *MEDIA_ASYNC_FIELDS]},
    {"id": "video", "group": "media", "auth": "required",
     "fields": ["endpoint", "default_model", "secret", *MEDIA_ASYNC_FIELDS]},
    {"id": "music", "group": "media", "auth": "required",
     "fields": ["endpoint", "default_model", "secret", *MEDIA_ASYNC_FIELDS]},
    {"id": "tts", "group": "media", "auth": "required",
     "fields": ["endpoint", "default_model", "secret", *MEDIA_ASYNC_FIELDS]},
    {"id": "stt", "group": "media", "auth": "required",
     "fields": ["endpoint", "default_model", "secret", *MEDIA_ASYNC_FIELDS]},
    # 3D generation (Meshy-style text/image→model, rigging, animation) was being
    # filed under "image" for lack of anything truer — a category that misleads
    # both the resource browser and the media brief an agent receives
    {"id": "model3d", "group": "media", "auth": "required",
     "fields": ["endpoint", "default_model", "secret", *MEDIA_ASYNC_FIELDS]},
]

BY_ID = {k["id"]: k for k in KINDS}

# config keys that are plain strings (validated, stored verbatim)
CONFIG_FIELDS = {"default_model", "mcp_transport", "mcp_command", "mcp_url",
                 "mcp_secret_env", "install_command", "skill_source",
                 "skill_id", "skill_version", "skill_target", "skill_digest",
                 "compatible_executors", "health_command", "git_provider",
                 "auth_header", *MEDIA_ASYNC_FIELDS}

MCP_TRANSPORTS = ("stdio", "http")
GIT_PROVIDERS = ("github", "gitlab", "custom")

# People paste the vendor's example URL, which is the operation, not the base.
# The gateway appends the operation path itself, so storing a full one produces
# .../chat/completions/v1/chat/completions at run time — catch it while editing.
OPERATION_SUFFIXES = ("/chat/completions", "/completions", "/messages",
                      "/responses", "/embeddings")


def base_endpoint(endpoint: str | None) -> tuple[str, bool]:
    """Strip a trailing operation path. Returns (base, stripped_anything)."""
    base = (endpoint or "").rstrip("/")
    for suffix in OPERATION_SUFFIXES:
        if base.lower().endswith(suffix):
            return base[: -len(suffix)].rstrip("/"), True
    return base, False


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
    if kind in ("llm", "image", "video", "music", "tts", "stt", "model3d"):
        if not endpoint:
            problems.append("endpoint-missing")
        elif base_endpoint(endpoint)[1]:
            problems.append("endpoint-is-operation-url")
    if kind in {"image", "video", "music", "tts", "stt", "model3d"}:
        status_path = str(config.get("async_status_path") or "")
        if status_path and (not status_path.startswith("/")
                            or "{task_id}" not in status_path):
            problems.append("async-status-path-invalid")
        for field in ("async_poll_interval_seconds", "async_max_attempts",
                      "async_max_bytes"):
            value = str(config.get(field) or "").strip()
            if value and (not value.isdigit() or int(value) < 1):
                problems.append("async-limit-invalid")
    if kind == "skill":
        if not config.get("skill_source"):
            problems.append("skill-source-missing")
        # Declaring an id opts into schedulable Skill semantics.  The complete
        # supply contract is required so admission never guesses.
        if config.get("skill_id"):
            for key, problem in (("skill_version", "skill-version-missing"),
                                 ("skill_target", "skill-target-missing"),
                                 ("skill_digest", "skill-digest-missing"),
                                 ("compatible_executors",
                                  "skill-compatible-executors-missing"),
                                 ("install_command", "skill-install-command-missing")):
                if not config.get(key):
                    problems.append(problem)
    if kind == "git":
        provider = config.get("git_provider") or "custom"
        if provider not in GIT_PROVIDERS:
            problems.append("git-provider-invalid")
        if provider == "custom" and not endpoint:
            problems.append("endpoint-missing")
        # an SSH URL needs a key and an HTTPS URL needs a token; the pairing is
        # visible from the config alone, so say it before a run finds out
        url = (endpoint or "").strip()
        if url.startswith(("git@", "ssh://")) and (secret_ref or "").startswith("env:"):
            pass                      # cannot tell what an env var holds
        if url.startswith(("http://", "https://")) and "ssh" in (secret_ref or "").lower():
            problems.append("git-https-with-ssh-key")
    return problems


def auth_header_pair(config: dict[str, Any], secret: str) -> tuple[str, str]:
    """(header name, header value) from whatever shape `auth_header` is in.

    The field means "header name" (`X-API-Key`), but agents reading vendor docs
    naturally write the whole line — `Authorization: Bearer {API_KEY}` — and the
    first live Novita setup did exactly that. Consuming it verbatim as a *name*
    crashed every probe with `Illegal header name`. Both shapes are legitimate
    input; this is the one place that understands them:

      "X-API-Key"                        -> ("X-API-Key", secret)
      "Authorization"                    -> ("Authorization", "Bearer <secret>")
      "Authorization: Bearer {API_KEY}"  -> ("Authorization", "Bearer <secret>")
    """
    raw = (config.get("auth_header") or "Authorization").strip()
    if ":" in raw:
        name, _, template = raw.partition(":")
        name = name.strip()
        template = template.strip()
        value = template
        for placeholder in ("{API_KEY}", "{KEY}", "{TOKEN}", "{SECRET}",
                            "{api_key}", "{key}", "{token}", "{secret}"):
            value = value.replace(placeholder, secret)
        if value == template and secret not in value:
            # a line with no placeholder: treat the template as a prefix
            value = f"{template} {secret}".strip()
        return name, value
    if raw.lower() == "authorization" and not secret.lower().startswith("bearer "):
        return raw, f"Bearer {secret}"
    return raw, secret
