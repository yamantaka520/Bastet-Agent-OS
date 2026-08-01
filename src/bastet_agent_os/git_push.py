"""Push a finished job's branch to the project's remote, automatically.

The ask: detect the project's git hosting (github / gitlab / custom) and push
when the code is done. What "done" means here is precise: the job walked its
whole pipeline — tests green, reviews approved, any human gate passed — and its
work sits committed on `bastet/<job_id>`. That branch is what gets pushed.

What is deliberately NOT automatic: the project's own branch is never pushed,
never fast-forwarded, never touched. Pushing a job branch parks the work
somewhere durable and reviewable (and openable as an MR/PR); merging it into
anything is still a person's move. Auto-push is on by default and per-project
switchable (`config_json {"git_auto_push": false}`).

Remote detection, in order of how explicit the signal is:

1. the repo's own `origin` remote — the project was cloned from somewhere, so
   that somewhere is where its branches belong;
2. failing that, a granted `git` resource whose endpoint is a repo URL.

Credentials come from the project's granted git resources: a resource whose
host matches the remote supplies the deploy key (`GIT_SSH_COMMAND`) or the
token (env-provided `http.extraHeader`, never in the URL or argv). No matching
credential is not an error — the push is tried anyway, because the host may
authenticate this machine some other way (an ssh-agent, a credential helper),
and git's own refusal is more honest than ours.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlparse

from . import secrets_store
from .db import Db, now

log = logging.getLogger("bastet.gitpush")

PUSH_TIMEOUT_S = 120


def _host_of(url: str) -> str:
    """The host part of an HTTPS or scp-style SSH remote URL."""
    if url.startswith(("http://", "https://")):
        return (urlparse(url).hostname or "").lower()
    if "@" in url and ":" in url:                       # git@host:group/repo.git
        return url.split("@", 1)[1].split(":", 1)[0].lower()
    return ""


def _origin_url(repo: str) -> str:
    proc = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _granted_git_resources(db: Db, project_id: str, team_id: str) -> list:
    return [r for r in db.query(
        "SELECT DISTINCT r.* FROM resources r JOIN grants g ON g.resource_id = r.id "
        "WHERE r.kind='git' AND r.enabled=1 AND ("
        "  g.scope_type='global' OR (g.scope_type='team' AND g.scope_id=?) "
        "  OR (g.scope_type='project' AND g.scope_id=?))",
        (team_id, project_id))]


def _resolve_secret(db: Db, row) -> str | None:
    if not row["secret_ref"]:
        return None
    try:
        return secrets_store.resolve(secrets_store.expand(db, row["secret_ref"]))
    except secrets_store.SecretError as exc:
        log.warning("git push: credential for %s unresolved: %s", row["name"], exc)
        return None


def _env_for(db: Db, url: str, resources: list, scratch: str) -> dict[str, str]:
    """Env that lets `git push <url>` authenticate, from the matching resource.

    The key goes to a 0600 file in a scratch dir the caller removes; the token
    goes into an env-provided header. Nothing lands in argv or the URL."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    host = _host_of(url)
    match = next((r for r in resources if _host_of(r["endpoint"] or "") == host),
                 None) or next(iter(resources), None)
    if match is None:
        return env
    secret = _resolve_secret(db, match)
    if not secret:
        return env
    provider = (json.loads(match["config_json"] or "{}").get("git_provider")
                or "custom")
    if url.startswith(("http://", "https://")):
        user = {"gitlab": "oauth2", "github": "x-access-token"}.get(provider, "git")
        token = base64.b64encode(f"{user}:{secret}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token}"})
    else:
        key_path = os.path.join(scratch, "push.key")
        with open(key_path, "w") as handle:
            handle.write(secret if secret.endswith("\n") else secret + "\n")
        os.chmod(key_path, 0o600)
        env["GIT_SSH_COMMAND"] = (f"ssh -i {key_path} -o IdentitiesOnly=yes "
                                  f"-o StrictHostKeyChecking=accept-new "
                                  f"-o BatchMode=yes")
    return env


def push_job_branch(db: Db, job, *, emit=None) -> dict[str, Any] | None:
    """Push `bastet/<job_id>` to the project's remote. Returns what happened,
    or None when there was nothing to do (opted out, no remote, no branch).

    Failure is reported, audited and non-fatal: the job is already done and its
    work is safe on the local branch — a push that failed is a delivery problem,
    not a reason to un-finish the job."""
    project = db.one("SELECT * FROM projects WHERE id=?", (job["project_id"],))
    if project is None:
        return None
    config = json.loads(project["config_json"] or "{}")
    if config.get("git_auto_push") is False:
        return None
    from .config import expand_repo_path
    repo = expand_repo_path(project["repo_path"] or "")
    if not repo or not os.path.isdir(repo):
        return None
    branch = f"bastet/{job['id']}"
    tip = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "-q",
                          f"refs/heads/{branch}"], capture_output=True, text=True)
    if tip.returncode != 0:
        return None                       # no branch — nothing was even started
    head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    if head.returncode == 0 and tip.stdout.strip() == head.stdout.strip():
        # `worktree add -b` creates the branch at HEAD before any work happens;
        # a tip still equal to HEAD means the job committed nothing (a read-only
        # review, a look-around) and pushing it would deliver an empty branch
        return None

    resources = _granted_git_resources(db, project["id"], project["team_id"])
    url = _origin_url(repo)
    detected = "origin"
    if not url:
        candidate = next((r for r in resources if (r["endpoint"] or "").strip()),
                         None)
        if candidate is None:
            db.audit("orchestrator", "job.push_skipped", "job", job["id"],
                     {"reason": "no origin remote and no granted git resource"})
            return {"pushed": False, "reason": "no-remote"}
        url = candidate["endpoint"].strip()
        detected = f"resource:{candidate['name']}"

    with tempfile.TemporaryDirectory(prefix="bastet-push-") as scratch:
        env = _env_for(db, url, resources, scratch)
        proc = subprocess.run(
            ["git", "-C", repo, "push", url, f"{branch}:{branch}"],
            capture_output=True, text=True, timeout=PUSH_TIMEOUT_S, env=env)

    output = (proc.stdout + proc.stderr).strip()[-600:]
    ok = proc.returncode == 0
    db.audit("orchestrator", "job.pushed" if ok else "job.push_failed",
             "job", job["id"],
             {"remote": detected, "host": _host_of(url), "branch": branch,
              "detail": output[:400]})
    if emit:
        emit("job.pushed" if ok else "job.push_failed", job["project_id"],
             job_id=job["id"], title=job["title"], branch=branch,
             host=_host_of(url), detail=output[:200])
    if ok:
        log.info("job %s: pushed %s to %s", job["id"], branch, _host_of(url) or url)
    else:
        log.warning("job %s: push failed (%s): %s", job["id"], detected, output[:200])
    return {"pushed": ok, "remote": detected, "branch": branch, "detail": output,
            "at": now()}
