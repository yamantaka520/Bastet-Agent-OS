"""Does this resource actually work? One honest check per kind.

"Configured" and "working" are different things, and each kind fails
differently: an LLM endpoint can be reachable with a rejected key, an MCP
server can install and still not speak the protocol, a skill source can simply
not exist on this host. So each kind gets a check that exercises the thing an
agent will actually do, and the verdict is three-state — `ok`, `warn` (it
answered, but not the way we hoped), `failed` — because collapsing "reachable
but 404" into a red cross sends people debugging the wrong thing.

Checks are cheap and read-only on purpose: no completions are requested, so
testing an LLM resource costs nothing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

from . import secrets_store
from .db import now

log = logging.getLogger("bastet.resources")

HTTP_TIMEOUT_S = 15.0
MCP_TIMEOUT_S = 25.0
GIT_TIMEOUT_S = 30.0
DETAIL_LIMIT = 2000

MCP_PROTOCOL = "2024-11-05"


def state_of(config: dict[str, Any]) -> dict[str, Any]:
    test = config.get("test") or {}
    return {"status": test.get("status", "unknown"), "at": test.get("at"),
            "checked": test.get("checked", ""), "detail": test.get("detail", ""),
            "digest": test.get("digest", "")}


def run(db, resource_id: str, actor: str) -> dict[str, Any]:
    """Test a resource and remember the verdict. Raises ValueError when the
    resource has nothing testable configured."""
    row = db.one("SELECT * FROM resources WHERE id=?", (resource_id,))
    if row is None:
        raise ValueError("resource not found")
    config = json.loads(row["config_json"] or "{}")
    secret = None
    ref = row["secret_ref"] or ""
    if ref:
        try:
            secret = secrets_store.resolve(secrets_store.expand(db, ref))
        except secrets_store.SecretError as exc:
            return _store(db, resource_id, config, actor, {
                "status": "failed", "checked": "credential",
                "detail": f"credential could not be resolved: {exc}"})

    kind = row["kind"]
    if kind == "mcp":
        result = _test_mcp(row, config, secret)
    elif kind == "skill":
        result = _test_skill(config)
    elif kind == "git":
        result = _test_git(row, config, secret)
    elif kind == "llm":
        result = _test_llm(row, secret)
    else:                                   # api + media kinds
        result = _test_http_endpoint(row, config, secret)
    return _store(db, resource_id, config, actor, result)


def _store(db, resource_id: str, config: dict[str, Any], actor: str,
           result: dict[str, Any]) -> dict[str, Any]:
    # Digest is execution evidence for managed Skills; other probe internals
    # stay transient.
    result = {"status": result["status"], "checked": result.get("checked", ""),
              "detail": str(result.get("detail", ""))[:DETAIL_LIMIT],
              **({"digest": result["digest"]} if result.get("digest") else {})}
    config["test"] = {**result, "at": now()}
    db.write("UPDATE resources SET config_json=?, updated_at=? WHERE id=?",
             (json.dumps(config), now(), resource_id))
    db.audit(actor, f"resource.test.{result['status']}", "resource", resource_id,
             {"checked": result.get("checked"), "detail": result["detail"][:300]})
    return state_of(config)


# ---- HTTP-shaped resources ---------------------------------------------------------

def _base(endpoint: str) -> str:
    return (endpoint or "").rstrip("/")


def _get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    """One GET, no redirects followed blindly, errors reported as they are."""
    try:
        resp = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT_S,
                         follow_redirects=True)
    except httpx.HTTPError as exc:
        return {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}
    body = resp.text[:400].replace("\n", " ")
    try:                       # keep the parsed body: summaries need all of it
        payload = resp.json()
    except ValueError:
        payload = None
    if resp.status_code in (401, 403):
        if _looks_like_challenge(resp):
            return {"status": "warn", "payload": payload,
                    "detail": f"HTTP {resp.status_code} — 主機回的是瀏覽器驗證頁"
                              f"（Cloudflare 之類），不是 API 回應，所以無法判斷憑證"
                              f"是否有效。{body[:200]}"}
        return {"status": "failed", "payload": payload,
                "detail": f"HTTP {resp.status_code} — credential rejected. {body}"}
    if resp.status_code < 400:
        return {"status": "ok", "payload": payload,
                "detail": f"HTTP {resp.status_code}. {body}"}
    if resp.status_code < 500:
        return {"status": "warn",
                "detail": f"HTTP {resp.status_code} — the host answered, so it is "
                          f"reachable, but this path is not it. {body}"}
    return {"status": "failed", "detail": f"HTTP {resp.status_code}. {body}"}


def _looks_like_challenge(resp: httpx.Response) -> bool:
    """A bot-check interstitial says nothing about the credential."""
    if "html" not in resp.headers.get("content-type", "").lower():
        return False
    head = resp.text[:600].lower()
    return any(marker in head for marker in
               ("just a moment", "cf-browser-verification", "captcha",
                "enable javascript and cookies", "attention required"))


def _test_llm(row, secret: str | None) -> dict[str, Any]:
    """List models: the cheapest call that proves endpoint + key together.
    Deliberately no completion request — testing must not cost tokens."""
    from .resource_kinds import base_endpoint

    base, stripped = base_endpoint(row["endpoint"])
    if not base:
        return {"status": "failed", "checked": "endpoint",
                "detail": "no endpoint configured"}
    flavor = (row["api_flavor"] or "openai").lower()
    if flavor == "anthropic":
        url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        headers = {"anthropic-version": "2023-06-01"}
        if secret:
            headers["x-api-key"] = secret
    else:
        url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        headers = {}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
    result = _get(url, headers)
    result["checked"] = f"GET {url}"
    if result["status"] == "ok":
        result["detail"] = _summarise_models(result.get("payload"), result["detail"])
    if stripped:
        # the stored endpoint is an operation URL; the gateway would append its
        # own path to it at run time, so say so even when the probe passed
        result["status"] = "warn" if result["status"] == "ok" else result["status"]
        result["detail"] = (f"{result['detail']} — the stored endpoint is a full "
                            f"operation URL; the pool wants the base "
                            f"({base}), or runs will hit "
                            f"{_base(row['endpoint'])}/v1/chat/completions.")
    return result


def _summarise_models(payload: Any, fallback: str) -> str:
    """"14 models, e.g. …" beats 400 characters of raw JSON."""
    if not isinstance(payload, dict):
        return fallback
    models = payload.get("data") or payload.get("models") or []
    if not isinstance(models, list) or not models:
        return fallback
    names = [m.get("id") or m.get("name") for m in models if isinstance(m, dict)]
    shown = ", ".join(n for n in names[:6] if n)
    return f"credential accepted; {len(models)} models available, e.g. {shown}"


def _test_http_endpoint(row, config: dict[str, Any], secret: str | None) -> dict[str, Any]:
    base = _base(row["endpoint"] or config.get("mcp_url") or "")
    if not base:
        return {"status": "failed", "checked": "endpoint",
                "detail": "no endpoint configured"}
    headers: dict[str, str] = {}
    if secret:
        from . import resource_kinds as rk
        name, value = rk.auth_header_pair(config, secret)
        headers[name] = value
    result = _get(base, headers)
    result["checked"] = f"GET {base}"
    return result


def _url_shape(url: str) -> str:
    """ssh (git@host:path or ssh://), repo (https://host/group/project), host
    (https://host with no project path), or empty."""
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith(("git@", "ssh://")) or (":" in value.split("/")[0]
                                                and "@" in value.split(":")[0]):
        return "ssh"
    if value.startswith(("http://", "https://")):
        path = value.split("://", 1)[1]
        return "repo" if path.strip("/").count("/") >= 1 else "host"
    return "path"


def _looks_like_private_key(secret: str | None) -> bool:
    return bool(secret) and "PRIVATE KEY" in secret


def _test_git(row, config: dict[str, Any], secret: str | None) -> dict[str, Any]:
    """Prove an agent could reach this repo with this credential.

    The endpoint people paste is the repo URL, so `git ls-remote` — the same
    handshake a clone starts with — is the honest check. An identity endpoint is
    only used when the URL names a host and no project."""
    provider = config.get("git_provider") or "custom"
    url = (row["endpoint"] or "").strip()
    shape = _url_shape(url)
    key_like = _looks_like_private_key(secret)

    if not url:
        return {"status": "failed", "checked": "endpoint",
                "detail": "沒有設定 endpoint（repo URL 或主機網址）"}

    # a credential of the wrong type can never work, whatever the network says
    if shape == "repo" and key_like:
        return {"status": "failed", "checked": f"credential vs {url}",
                "detail": "憑證是 SSH 私鑰，但 endpoint 是 HTTPS URL —— HTTPS 無法用"
                          "私鑰驗證。要嘛把 endpoint 改成 SSH 形式"
                          "（git@host:group/project.git），要嘛換成 access token。"}
    if shape == "ssh" and secret and not key_like:
        return {"status": "failed", "checked": f"credential vs {url}",
                "detail": "endpoint 是 SSH URL，但憑證看起來是 token 而不是 SSH 私鑰。"
                          "SSH 需要私鑰；用 token 請改成 HTTPS 形式的 repo URL。"}

    if shape == "ssh":
        return _ls_remote_ssh(url, secret)
    if shape == "repo":
        return _ls_remote_https(url, secret, provider)
    if shape == "path":
        return _git_ls_remote(url)

    # host-only URL: ask the provider who we are
    if provider == "gitlab":
        base = _base(url) or "https://gitlab.com"
        headers = {"PRIVATE-TOKEN": secret} if secret else {}
        result = _get(f"{base}/api/v4/user" if secret else base, headers)
        result["checked"] = f"GET {base}/api/v4/user"
        return result
    if provider == "github":
        if not secret:
            result = _get("https://api.github.com", {})
            result["checked"] = "GET https://api.github.com (public only)"
            result["detail"] = ("no credential configured — only public reachability "
                                f"checked. {result['detail']}")
            return result
        result = _get("https://api.github.com/user",
                      {"Authorization": f"Bearer {secret}",
                       "Accept": "application/vnd.github+json"})
        result["checked"] = "GET https://api.github.com/user"
        return result
    result = _get(_base(url), {})
    result["checked"] = f"GET {_base(url)}"
    return result


def _ls_remote_ssh(url: str, secret: str | None) -> dict[str, Any]:
    """The clone handshake over SSH, using the configured key and nothing else.

    `IdentitiesOnly` stops ssh silently succeeding with some agent key that the
    agent process will not have; BatchMode stops it hanging on a passphrase
    prompt; accept-new avoids a first-connection prompt without disabling host
    key checks entirely."""
    import stat
    import tempfile

    checked = f"git ls-remote (ssh) {url}"
    if not secret:
        return {"status": "failed", "checked": checked,
                "detail": "SSH endpoint 需要一把私鑰憑證，目前沒有設定。"}
    key_dir = tempfile.mkdtemp(prefix="bastet-git-")
    key_path = Path(key_dir) / "key"
    try:
        body = secret if secret.endswith("\n") else secret + "\n"   # ssh demands it
        key_path.write_text(body)
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        env = {**os.environ,
               "GIT_TERMINAL_PROMPT": "0",
               "GIT_SSH_COMMAND": (f"ssh -i {key_path} -o IdentitiesOnly=yes "
                                   f"-o StrictHostKeyChecking=accept-new "
                                   f"-o BatchMode=yes -o ConnectTimeout=15")}
        try:
            proc = subprocess.run(["git", "ls-remote", "--exit-code", url, "HEAD"],
                                  capture_output=True, text=True,
                                  timeout=GIT_TIMEOUT_S, env=env)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"status": "failed", "checked": checked,
                    "detail": f"{type(exc).__name__}: {exc}"}
        output = (proc.stdout + proc.stderr).strip()
        if proc.returncode == 0:
            return {"status": "ok", "checked": checked,
                    "detail": f"SSH 金鑰可用，repo 可讀。{output[:200]}"}
        return {"status": "failed", "checked": checked,
                "detail": _ssh_hint(output) + output[-400:]}
    finally:
        shutil.rmtree(key_dir, ignore_errors=True)


def _ssh_hint(output: str) -> str:
    lowered = output.lower()
    if "permission denied" in lowered or "publickey" in lowered:
        return ("這把金鑰被拒絕 —— 確認公鑰已加到 GitLab 帳號的 SSH Keys，"
                "且貼進 Bastet 的是私鑰全文。")
    if "host key verification failed" in lowered:
        return "主機金鑰驗證失敗。"
    if "could not resolve" in lowered or "connection timed out" in lowered:
        return "連不到主機（DNS 或防火牆）。"
    if "invalid format" in lowered or "error in libcrypto" in lowered:
        return "私鑰格式不正確（常見原因：貼上時少了結尾換行，或貼的是公鑰）。"
    return ""


def _ls_remote_https(url: str, secret: str | None, provider: str) -> dict[str, Any]:
    """The clone handshake over HTTPS. The token travels in an env-provided
    header, never in the URL or argv."""
    checked = f"git ls-remote (https) {url}"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if secret:
        user = {"gitlab": "oauth2", "github": "x-access-token"}.get(provider, "git")
        token = base64.b64encode(f"{user}:{secret}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token}"})
    try:
        proc = subprocess.run(["git", "ls-remote", "--exit-code", url, "HEAD"],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
                              env=env)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"status": "failed", "checked": checked,
                "detail": f"{type(exc).__name__}: {exc}"}
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 0:
        return {"status": "ok", "checked": checked,
                "detail": f"repo 可讀。{output[:200]}"}
    lowered = output.lower()
    if "authentication failed" in lowered or "403" in lowered or "401" in lowered:
        hint = ("憑證被拒絕 —— GitLab 需要有 read_repository 權限的 access token"
                "（不是 SSH 金鑰）。" if not secret or provider == "gitlab"
                else "憑證被拒絕。")
        return {"status": "failed", "checked": checked,
                "detail": hint + output[-400:]}
    return {"status": "failed", "checked": checked, "detail": output[-400:]}


def _git_ls_remote(url: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(["git", "ls-remote", "--exit-code", url, "HEAD"],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"status": "failed", "checked": f"git ls-remote {url}",
                "detail": f"{type(exc).__name__}: {exc}"}
    ok = proc.returncode == 0
    return {"status": "ok" if ok else "failed",
            "checked": f"git ls-remote {url}",
            "detail": (proc.stdout or proc.stderr).strip()[:400]}


def _test_skill(config: dict[str, Any]) -> dict[str, Any]:
    from .skill_supply import digest_path, effective_path, normalise_digest

    source = (config.get("skill_source") or "").strip()
    if not source:
        return {"status": "failed", "checked": "skill_source",
                "detail": "no skill source configured"}
    if source.startswith(("http://", "https://", "git@")) and not config.get("skill_target"):
        return _git_ls_remote(source)
    path = effective_path(config) or Path(source).expanduser().resolve()
    if not path.exists():
        return {"status": "failed", "checked": f"path {path}",
                "detail": "not found on the Bastet host (source/target paths are "
                          "resolved there, not on the browser machine)"}
    actual = digest_path(path)
    expected = normalise_digest(config.get("skill_digest") or "")
    if expected and actual != expected:
        return {"status": "failed", "checked": f"digest {path}", "digest": actual,
                "detail": f"digest mismatch: expected {expected}, got {actual}"}
    command = (config.get("health_command") or "").strip()
    if command:
        try:
            proc = subprocess.run(command, shell=True, capture_output=True, text=True,
                                  timeout=60, cwd=str(path if path.is_dir() else path.parent))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"status": "failed", "checked": f"health: {command}",
                    "digest": actual, "detail": f"{type(exc).__name__}: {exc}"}
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()[-800:]
        if proc.returncode:
            return {"status": "failed", "checked": f"health: {command}",
                    "digest": actual,
                    "detail": f"health command exited {proc.returncode}: {output}"}
    return {"status": "ok", "checked": f"path/digest/health {path}",
            "digest": actual,
            "detail": f"installed Skill is healthy; digest {actual}"}


# ---- MCP: speak the protocol ------------------------------------------------------

def _initialize_payload() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "bastet-agent-os", "version": "test"}}}


def _test_mcp(row, config: dict[str, Any], secret: str | None) -> dict[str, Any]:
    transport = config.get("mcp_transport") or "stdio"
    if transport == "http":
        url = config.get("mcp_url") or row["endpoint"]
        if not url:
            return {"status": "failed", "checked": "mcp_url", "detail": "no URL configured"}
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if secret:
            headers["Authorization"] = (secret if secret.lower().startswith("bearer ")
                                        else f"Bearer {secret}")
        try:
            resp = httpx.post(url, json=_initialize_payload(), headers=headers,
                              timeout=HTTP_TIMEOUT_S)
        except httpx.HTTPError as exc:
            return {"status": "failed", "checked": f"POST {url} initialize",
                    "detail": f"{type(exc).__name__}: {exc}"}
        return {**_verdict_from_mcp_text(resp.text, resp.status_code),
                "checked": f"POST {url} initialize"}

    command = (config.get("mcp_command") or "").strip()
    if not command:
        return {"status": "failed", "checked": "mcp_command",
                "detail": "no launch command configured"}
    env = {**os.environ}
    if secret:
        env[config.get("mcp_secret_env") or "API_KEY"] = secret
    stdin = (json.dumps(_initialize_payload()) + "\n"
             + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
             + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
    try:
        proc = subprocess.run(shlex.split(command), input=stdin, capture_output=True,
                              text=True, timeout=MCP_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired as exc:
        # a server that keeps the pipe open past the handshake window: we cannot
        # call that healthy, but we say what happened instead of guessing
        return {"status": "warn", "checked": f"stdio handshake: {command}",
                "detail": f"no complete handshake within {MCP_TIMEOUT_S:.0f}s. "
                          f"{(exc.stdout or '')[-300:]}{(exc.stderr or '')[-300:]}"}
    except (OSError, ValueError) as exc:
        return {"status": "failed", "checked": f"stdio handshake: {command}",
                "detail": f"{type(exc).__name__}: {exc}"}
    verdict = _verdict_from_mcp_text(proc.stdout, None)
    if verdict["status"] != "ok" and proc.stderr:
        verdict["detail"] = f"{verdict['detail']} stderr: {proc.stderr.strip()[:400]}"
    verdict["checked"] = f"stdio handshake: {command}"
    return verdict


def _verdict_from_mcp_text(text: str, status_code: int | None) -> dict[str, Any]:
    """Find the initialize result in whatever the server sent (raw JSON lines or
    SSE frames) and summarise the server + its tools."""
    server: dict[str, Any] | None = None
    tools: list[Any] | None = None
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = message.get("result")
        if not isinstance(result, dict):
            if message.get("error"):
                return {"status": "failed",
                        "detail": f"server returned an error: {message['error']}"}
            continue
        if "serverInfo" in result or "protocolVersion" in result:
            server = result
        if isinstance(result.get("tools"), list):
            tools = result["tools"]
    if server is None:
        prefix = f"HTTP {status_code}: " if status_code else ""
        return {"status": "failed",
                "detail": f"{prefix}no MCP initialize result in the response. "
                          f"{(text or '')[:300]}"}
    info = server.get("serverInfo") or {}
    detail = (f"handshake ok — {info.get('name', 'server')} "
              f"{info.get('version', '')} (protocol "
              f"{server.get('protocolVersion', '?')})").strip()
    if tools is not None:
        names = [t.get("name") for t in tools if isinstance(t, dict)][:6]
        detail += f"; {len(tools)} tools: {', '.join(n for n in names if n)}"
    return {"status": "ok", "detail": detail}
