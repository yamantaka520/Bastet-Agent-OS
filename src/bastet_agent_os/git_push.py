"""Push a finished job's branch to the project's remote, automatically.

The ask: detect the project's git hosting (github / gitlab / custom) and push
when the code is done. What "done" means here is precise: the job walked its
whole pipeline — tests green, reviews approved, any human gate passed — and its
work sits committed on `bastet/<job_id>`. That branch is what gets pushed.

For ordinary and legacy jobs, the project's own branch is never touched:
pushing a job branch only parks the work somewhere durable and reviewable.
An explicit production delivery contract is the sole exception; its separate
path merges a fresh target and atomically pushes that target with a release tag.
Optional auto-push is on by default and per-project switchable
(`config_json {"git_auto_push": false}`).

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
import re
import subprocess
import tempfile
from collections.abc import Callable
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
    # exact host match ONLY (review finding): the earlier "else take any git
    # resource" fallback would have sent, say, a GitLab token in a header to
    # github.com — a credential must never travel to a host it was not
    # configured for. No match ⇒ push unauthenticated and let git say no.
    match = next((r for r in resources
                  if host and _host_of(r["endpoint"] or "") == host), None)
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

    This primitive reports and audits failure. The caller decides terminal
    semantics: optional legacy delivery remains non-fatal, while an explicit
    branch/integration/production contract blocks the job."""
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
        try:
            proc = subprocess.run(
                ["git", "-C", repo, "push", url, f"{branch}:{branch}"],
                capture_output=True, text=True, timeout=PUSH_TIMEOUT_S, env=env)
            output = (proc.stdout + proc.stderr).strip()[-600:]
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            output = f"push timed out after {PUSH_TIMEOUT_S}s"
            ok = False
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


def integrate_job_branch(db: Db, job, *, workdir: str,
                         target_branch: str = "main",
                         release_tag: str = "",
                         prepush_gate: Callable[[str], str] | None = None,
                         ) -> dict[str, Any]:
    """Merge the fresh remote target and atomically push HEAD and its tag.

    This is used only by an explicit integration or production delivery
    contract. It never force-pushes: a concurrently advanced target or release tag
    makes the atomic push fail and leaves the card blocked with its worktree
    intact.  A production release therefore cannot silently update ``main``
    without also publishing its immutable version tag.
    """
    if release_tag and not re.fullmatch(r"v[0-9A-Za-z][0-9A-Za-z._+-]*", release_tag):
        return {"pushed": False, "detail": "invalid release tag"}
    valid_target = subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{target_branch}"],
        capture_output=True, text=True)
    if valid_target.returncode:
        return {"pushed": False, "detail": "invalid target branch"}
    project = db.one("SELECT * FROM projects WHERE id=?", (job["project_id"],))
    if project is None:
        return {"pushed": False, "detail": "project not found"}
    resources = _granted_git_resources(db, project["id"], project["team_id"])
    from .config import expand_repo_path
    repo = expand_repo_path(project["repo_path"] or "")
    url = _origin_url(repo)
    detected = "origin"
    if not url:
        candidate = next((r for r in resources if (r["endpoint"] or "").strip()), None)
        if candidate is None:
            return {"pushed": False, "detail": "no remote or granted git resource"}
        url = candidate["endpoint"].strip()
        detected = f"resource:{candidate['name']}"
    with tempfile.TemporaryDirectory(prefix="bastet-release-") as scratch:
        env = _env_for(db, url, resources, scratch)
        steps = [
            ["git", "-C", workdir, "fetch", "--no-tags", url, target_branch],
            ["git", "-C", workdir, "merge", "--no-edit", "FETCH_HEAD"],
        ]
        output = []
        for command in steps:
            try:
                proc = subprocess.run(command, capture_output=True, text=True,
                                      timeout=PUSH_TIMEOUT_S, env=env)
            except subprocess.TimeoutExpired:
                return {"pushed": False, "detail": f"{command[3]} timed out"}
            output.append((proc.stdout + proc.stderr).strip())
            if proc.returncode:
                detail = "\n".join(output)[-1200:]
                db.audit("orchestrator", "job.integration_failed", "job", job["id"],
                         {"target_branch": target_branch, "remote": detected,
                          "detail": detail[:800]})
                return {"pushed": False, "detail": detail}
        commit_sha = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "HEAD"], capture_output=True,
            text=True).stdout.strip()
        # The release gate runs against the exact merged candidate, before any
        # public ref moves.  A callback keeps host-specific validation in the
        # delivery layer while this function owns the atomic git transaction.
        gate_output = prepush_gate(commit_sha) if prepush_gate else ""
        if release_tag:
            remote_tag = subprocess.run(
                ["git", "ls-remote", "--tags", url, f"refs/tags/{release_tag}",
                 f"refs/tags/{release_tag}^{{}}"], capture_output=True, text=True,
                timeout=PUSH_TIMEOUT_S, env=env)
            if remote_tag.returncode:
                return {"pushed": False, "detail": remote_tag.stderr[-1200:]}
            remote_lines = remote_tag.stdout.splitlines()
            remote_sha = remote_lines[-1].split()[0] if remote_lines else ""
            if remote_sha and remote_sha != commit_sha:
                return {"pushed": False,
                        "detail": f"release tag {release_tag} already points elsewhere"}
            local_tag = subprocess.run(
                ["git", "-C", workdir, "rev-parse", "--verify", "-q",
                 f"refs/tags/{release_tag}^{{}}"], capture_output=True, text=True)
            if local_tag.returncode == 0 and local_tag.stdout.strip() != commit_sha:
                return {"pushed": False,
                        "detail": f"local release tag {release_tag} points elsewhere"}
            if local_tag.returncode != 0:
                tagged = subprocess.run(
                    ["git", "-C", workdir, "tag", "-a", release_tag, "-m",
                     f"Release {release_tag}"], capture_output=True, text=True)
                if tagged.returncode:
                    return {"pushed": False, "detail": tagged.stderr[-1200:]}
        push = ["git", "-C", workdir, "push", "--atomic", url,
                f"HEAD:{target_branch}"]
        if release_tag:
            push.append(f"refs/tags/{release_tag}:refs/tags/{release_tag}")
        proc = subprocess.run(push, capture_output=True, text=True,
                              timeout=PUSH_TIMEOUT_S, env=env)
        output.append((proc.stdout + proc.stderr).strip())
        if proc.returncode:
            detail = "\n".join(output)[-1200:]
            db.audit("orchestrator", "job.integration_failed", "job", job["id"],
                     {"target_branch": target_branch, "release_tag": release_tag,
                      "remote": detected, "detail": detail[:800]})
            return {"pushed": False, "detail": detail}
        verified = subprocess.run(
            ["git", "ls-remote", "--heads", url, f"refs/heads/{target_branch}"],
            capture_output=True, text=True, timeout=PUSH_TIMEOUT_S, env=env)
        remote_sha = verified.stdout.split()[0] if verified.returncode == 0 \
            and verified.stdout.split() else ""
        if remote_sha != commit_sha:
            return {"pushed": False,
                    "detail": "remote target verification failed: "
                              f"expected {commit_sha}, got {remote_sha or 'missing'}"}
    detail = "\n".join(output)[-1200:]
    db.audit("orchestrator", "job.integrated", "job", job["id"],
             {"target_branch": target_branch, "remote": detected,
              "release_tag": release_tag, "commit_sha": commit_sha,
              "detail": detail[:800]})
    return {"pushed": True, "target_branch": target_branch, "remote": detected,
            "release_tag": release_tag, "commit_sha": commit_sha,
            "remote_commit_sha": remote_sha,
            "gate_output": gate_output, "detail": detail}
