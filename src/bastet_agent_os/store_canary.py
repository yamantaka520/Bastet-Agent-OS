"""Credentialed, non-publishing canary for built-in mobile-store adapters."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import secrets_store
from .db import Db, now
from .delivery import DeliveryError, _submission_receipt, _verification_receipt, validate_profile
from .store_adapters import StoreAdapterError, official_status

APPLE_SECRET_NAMES = {
    "APP_STORE_CONNECT_KEY_ID",
    "APP_STORE_CONNECT_ISSUER_ID",
    "APP_STORE_CONNECT_PRIVATE_KEY",
}
GOOGLE_SECRET_NAMES = {"GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"}


class StoreCanaryError(RuntimeError):
    pass


def _credentials(db: Db, project_id: str, team_id: str, provider: str,
                 *, actor: str) -> dict[str, str]:
    wanted = APPLE_SECRET_NAMES if provider == "app_store_connect" \
        else GOOGLE_SECRET_NAMES
    rows = db.query(
        "SELECT DISTINCT r.id,r.name,r.secret_ref,r.config_json FROM grants g "
        "JOIN resources r ON r.id=g.resource_id "
        "WHERE r.kind='secret' AND r.enabled=1 AND g.enabled=1 AND "
        "(g.scope_type='global' OR (g.scope_type='project' AND g.scope_id=?) "
        "OR (g.scope_type='team' AND g.scope_id=?))",
        (project_id, team_id))
    selected: dict[str, Any] = {}
    for row in rows:
        config = json.loads(row["config_json"] or "{}")
        env_name = str(config.get("env_name") or "")
        if env_name not in wanted:
            continue
        if env_name in selected:
            raise StoreCanaryError(
                f"multiple granted secrets provide required environment name {env_name}")
        selected[env_name] = row
    missing = sorted(wanted - selected.keys())
    if missing:
        raise StoreCanaryError("missing granted store secrets: " + ", ".join(missing))
    env: dict[str, str] = {}
    for env_name, row in selected.items():
        try:
            env[env_name] = secrets_store.resolve(
                secrets_store.expand(db, row["secret_ref"] or ""))
        except secrets_store.SecretError as exc:
            raise StoreCanaryError(f"could not resolve store secret {env_name}") from exc
        db.audit(actor, "secret.resolve", "resource", row["id"], {
            "env_name": env_name,
            "project": project_id,
            "purpose": "store-canary",
        })
    return env


def _load_json(path: str | Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise StoreCanaryError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise StoreCanaryError(f"{label} must be a JSON object")
    return value


def run(home_root: str | Path, *, project_id: str = "", job_id: str = "",
        submission_file: str | Path | None = None,
        status_reader: Callable[[dict[str, Any], dict[str, Any], dict[str, str]],
                                dict[str, Any]] = official_status) -> dict[str, Any]:
    """Read one exact provider object without changing release state.

    Job mode gives the strongest binding by reusing the frozen delivery contract,
    integrated commit receipt and durable uploader receipt. Project/file mode is a
    preflight for an already-existing provider object and labels that weaker provenance.
    """
    if bool(job_id) == bool(submission_file):
        raise StoreCanaryError("choose exactly one of job_id or submission_file")
    db_path = Path(home_root) / "bastet.db"
    if not db_path.is_file():
        raise StoreCanaryError("Bastet home is not initialized")
    db = Db(db_path)
    actor = "cli:store-canary"
    try:
        if job_id:
            job = db.one(
                "SELECT j.project_id,j.delivery_json,p.team_id FROM jobs j "
                "JOIN projects p ON p.id=j.project_id WHERE j.id=?", (job_id,))
            if job is None:
                raise StoreCanaryError(f"job not found: {job_id}")
            project_id = job["project_id"]
            contract = json.loads(job["delivery_json"] or "{}")
            profile = contract.get("profile") or {}
            delivery = db.one(
                "SELECT * FROM deliveries WHERE job_id=? ORDER BY rowid DESC LIMIT 1",
                (job_id,))
            if delivery is None:
                raise StoreCanaryError("job has no delivery receipt")
            evidence = json.loads(delivery["evidence_json"] or "{}")
            supplied = evidence.get("submission_receipt")
            if not isinstance(supplied, dict):
                raise StoreCanaryError("job has no durable submission receipt")
            commit_sha = str(delivery["commit_sha"] or "")
            version = str(delivery["version"] or contract.get("version") or "")
            target = str(delivery["target"] or profile.get("target") or "")
            team_id = str(job["team_id"] or "")
            binding = "frozen_job_delivery"
        else:
            project = db.one("SELECT team_id,config_json FROM projects WHERE id=?",
                             (project_id,))
            if project is None:
                raise StoreCanaryError(f"project not found: {project_id}")
            config = json.loads(project["config_json"] or "{}")
            profile = config.get("delivery_profile") or {}
            supplied = _load_json(submission_file, "submission receipt")
            commit_sha = str(supplied.get("commit_sha") or "")
            version = str(supplied.get("version") or "")
            target = str(supplied.get("target") or "")
            team_id = str(project["team_id"] or "")
            binding = "supplied_submission_receipt"
        try:
            validate_profile(profile, "production")
        except ValueError as exc:
            raise StoreCanaryError(str(exc)) from exc
        if profile.get("status_adapter") != "official_api":
            raise StoreCanaryError("project delivery profile does not use official_api")
        if not commit_sha or not version or not target:
            raise StoreCanaryError("submission binding requires commit_sha, version and target")
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit_sha):
            raise StoreCanaryError("submission commit_sha must be a full Git object ID")
        if binding == "supplied_submission_receipt":
            target = str(profile.get("target") or profile.get("target_branch") or "main")
        try:
            submission = _submission_receipt(
                json.dumps(supplied), commit_sha=commit_sha, version=version,
                target=target, profile=profile)
        except DeliveryError as exc:
            raise StoreCanaryError(str(exc)) from exc
        provider = str(profile["provider"])
        env = _credentials(db, project_id, team_id, provider, actor=actor)
        try:
            receipt = status_reader(profile, submission, env)
            checked, meets_goal = _verification_receipt(
                receipt, commit_sha=commit_sha, version=version, target=target,
                profile=profile, allow_rejected=True)
        except (DeliveryError, StoreAdapterError) as exc:
            raise StoreCanaryError(str(exc)) from exc
        report = {
            "ok": True,
            "read_only": True,
            "project_id": project_id,
            "job_id": job_id or None,
            "binding": binding,
            "provider": provider,
            "meets_release_goal": meets_goal,
            "observed_at": now(),
            "receipt": checked,
        }
        db.audit(actor, "store.canary.checked", "project", project_id, {
            "job_id": job_id or None,
            "binding": binding,
            "provider": provider,
            "milestone": checked["milestone"],
            "provider_status": checked["provider_status"],
            "meets_release_goal": meets_goal,
        })
        return report
    finally:
        db.close()
