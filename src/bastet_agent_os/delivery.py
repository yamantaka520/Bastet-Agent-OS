"""Durable, deterministic job delivery.

An Agent finishing its prose/code stage is not proof that anything reached a
remote branch, target branch, or production. Delivery contracts are executed by
Bastet's trusted host process and leave an immutable receipt. A required delivery that
fails keeps the card blocked and preserves its worktree for repair/retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .db import Db

MODES = {"none", "branch", "integration", "production"}
COMMAND_TIMEOUT_S = 1800
OUTPUT_LIMIT = 8000
STORE_PROVIDERS = {"app_store_connect", "google_play"}
STORE_MILESTONES = {"uploaded": 0, "submitted": 1, "approved": 2, "published": 3}
APPLE_VERSION_METADATA_FIELDS = {
    "description",
    "keywords",
    "marketingUrl",
    "promotionalText",
    "supportUrl",
    "whatsNew",
}


class DeliveryError(RuntimeError):
    pass


@dataclass
class DeliveryResult:
    mode: str
    target: str
    version: str
    commit_sha: str
    evidence: dict[str, Any]
    complete: bool = True
    provider_status: str = ""
    next_poll_at: str = ""


def normalize(raw: dict | None) -> dict[str, Any]:
    value = dict(raw or {})
    mode = str(value.get("mode") or "none").strip().lower()
    if mode not in MODES:
        raise ValueError(f"delivery.mode must be one of {sorted(MODES)}")
    value["mode"] = mode
    if mode == "production":
        version = str(value.get("version") or "").strip()
        if not version:
            raise ValueError("production delivery requires a new version")
        value["version"] = version.removeprefix("v")
    return value


def required(raw: dict | None) -> bool:
    return normalize(raw).get("mode") != "none"


def validate_profile(profile: Any, mode: str) -> dict[str, Any]:
    """Validate trusted host delivery configuration before Agent work begins."""
    if not isinstance(profile, dict):
        raise ValueError("delivery profile must be an object")
    provider = str(profile.get("provider") or "web").strip()
    if provider != "web" and provider not in STORE_PROVIDERS:
        raise ValueError(
            f"delivery profile provider must be web or one of {sorted(STORE_PROVIDERS)}")
    submission_adapter = str(profile.get("submission_adapter") or "command").strip()
    if submission_adapter not in {"command", "official_api"}:
        raise ValueError(
            "store submission_adapter must be command or official_api")
    if submission_adapter == "official_api" and provider not in STORE_PROVIDERS:
        raise ValueError("built-in submission requires a mobile-store provider")
    required_commands = ["predeploy_command"]
    if mode == "production" and submission_adapter == "command":
        required_commands.append("deploy_command")
    missing = [key for key in required_commands
               if not str(profile.get(key) or "").strip()]
    if missing:
        raise ValueError(
            f"{mode} delivery profile is missing: {', '.join(missing)}")
    if provider in STORE_PROVIDERS:
        status_adapter = str(profile.get("status_adapter") or "command").strip()
        if status_adapter not in {"command", "official_api"}:
            raise ValueError("store status_adapter must be command or official_api")
        recovery = str(profile.get("submission_recovery") or "command").strip()
        if recovery not in {"command", "official_api"}:
            raise ValueError(
                "store submission_recovery must be command or official_api")
        if recovery == "official_api" and status_adapter != "official_api":
            raise ValueError(
                "official submission recovery requires status_adapter=official_api")
        if submission_adapter == "official_api" and (
                status_adapter != "official_api" or recovery != "official_api"):
            raise ValueError(
                "built-in submission requires official status and submission recovery")
        if status_adapter == "command" and not str(profile.get("verify_command") or "").strip():
            missing.append("verify_command")
        goal = str(profile.get("release_goal") or "published").strip()
        if goal not in STORE_MILESTONES:
            raise ValueError(
                f"store release_goal must be one of {sorted(STORE_MILESTONES)}")
        identity_fields = (["app_id"] if provider == "app_store_connect"
                           else ["package_name", "track"])
        missing_identity = [field for field in identity_fields
                            if not str(profile.get(field) or "").strip()]
        if missing_identity:
            raise ValueError(
                f"{provider} profile is missing: {', '.join(missing_identity)}")
        if recovery == "official_api":
            recovery_fields = (["build_number"] if provider == "app_store_connect"
                               else ["version_code"])
            missing_recovery = [field for field in recovery_fields
                                if not str(profile.get(field) or "").strip()]
            if missing_recovery:
                raise ValueError(
                    f"{provider} official submission recovery is missing: "
                    + ", ".join(missing_recovery))
        if submission_adapter == "official_api":
            if provider == "google_play":
                if str(profile.get("track") or "") != "internal":
                    raise ValueError(
                        "built-in Google Play submission is restricted to the internal track")
                if not str(profile.get("artifact_path") or "").strip():
                    raise ValueError(
                        "google_play built-in submission is missing: artifact_path")
                version_code = str(profile.get("version_code") or "")
                if not version_code.isdigit() or int(version_code) < 1:
                    raise ValueError("google_play version_code must be a positive integer")
                artifact_digest = str(profile.get("artifact_sha256") or "")
                if artifact_digest and (
                        len(artifact_digest) != 64
                        or any(char not in "0123456789abcdefABCDEF"
                               for char in artifact_digest)):
                    raise ValueError("artifact_sha256 must be 64 hexadecimal characters")
                release_status = str(
                    profile.get("google_release_status") or "completed")
                if release_status not in {"draft", "completed"}:
                    raise ValueError(
                        "google_release_status must be draft or completed")
                review_flag = profile.get("google_changes_not_sent_for_review", True)
                if not isinstance(review_flag, bool):
                    raise ValueError(
                        "google_changes_not_sent_for_review must be a boolean")
            else:
                platform = str(profile.get("platform") or "IOS").upper()
                if platform not in {"IOS", "MAC_OS", "TV_OS", "VISION_OS"}:
                    raise ValueError("unsupported App Store Connect platform")
                release_type = str(profile.get("apple_release_type") or "MANUAL")
                if release_type != "MANUAL":
                    raise ValueError(
                        "built-in App Store submission currently requires MANUAL release")
                artifact_path = str(profile.get("artifact_path") or "")
                if artifact_path and not artifact_path.lower().endswith((".ipa", ".pkg")):
                    raise ValueError("App Store artifact_path must end in .ipa or .pkg")
                artifact_digest = str(profile.get("artifact_sha256") or "")
                if artifact_digest and (
                        len(artifact_digest) != 64
                        or any(char not in "0123456789abcdefABCDEF"
                               for char in artifact_digest)):
                    raise ValueError("artifact_sha256 must be 64 hexadecimal characters")
                try:
                    parallelism = int(profile.get("apple_upload_parallelism") or 4)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "apple_upload_parallelism must be an integer") from exc
                if parallelism < 1 or parallelism > 8:
                    raise ValueError("apple_upload_parallelism must be between 1 and 8")
                for field_name in (
                        "apple_required_locales", "apple_metadata_required_fields",
                        "apple_required_screenshot_display_types"):
                    raw_value = profile.get(field_name)
                    if raw_value is not None and not isinstance(raw_value, (str, list)):
                        raise ValueError(f"{field_name} must be a comma-separated string or list")
                raw_fields = profile.get(
                    "apple_metadata_required_fields",
                    "description,supportUrl,whatsNew",
                )
                fields = ([str(value).strip() for value in raw_fields]
                          if isinstance(raw_fields, list)
                          else [value.strip() for value in str(raw_fields).split(",")])
                fields = [value for value in fields if value]
                unknown_fields = sorted(set(fields) - APPLE_VERSION_METADATA_FIELDS)
                if unknown_fields:
                    raise ValueError(
                        "unsupported apple_metadata_required_fields: "
                        + ", ".join(unknown_fields))
                goal = str(profile.get("release_goal") or "published")
                if goal != "uploaded" and not fields:
                    raise ValueError(
                        "submitted App Store delivery requires metadata fields")
                require_screenshots = profile.get("apple_require_screenshots", True)
                if not isinstance(require_screenshots, bool):
                    raise ValueError("apple_require_screenshots must be a boolean")
        try:
            interval = int(profile.get("poll_interval_seconds") or 300)
        except (TypeError, ValueError) as exc:
            raise ValueError("store poll_interval_seconds must be an integer") from exc
        if interval < 1 or interval > 86400:
            raise ValueError("store poll_interval_seconds must be between 1 and 86400")
    elif mode == "production" and not str(profile.get("verify_command") or "").strip():
        missing.append("verify_command")
    if missing:
        raise ValueError(
            f"{mode} delivery profile is missing: {', '.join(missing)}")
    return profile


def _run(command: str, workdir: str, env: dict[str, str], label: str) -> str:
    if not command.strip():
        raise DeliveryError(f"missing {label} command")
    try:
        proc = subprocess.run(
            command, shell=True, cwd=workdir, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env={**os.environ, **env},
            timeout=COMMAND_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeliveryError(f"{label} could not run: {type(exc).__name__}: {exc}") from exc
    output = (proc.stdout + proc.stderr)[-OUTPUT_LIMIT:]
    if proc.returncode:
        raise DeliveryError(f"{label} failed (exit {proc.returncode}): {output}")
    return output


def _package_version(workdir: str, source: str) -> str:
    path = (Path(workdir) / source).resolve()
    root = Path(workdir).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise DeliveryError(f"version source not found: {source}")
    try:
        payload = json.loads(path.read_text())
        version = str(payload.get("version") or "").strip().removeprefix("v")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise DeliveryError(f"invalid version source {source}: {exc}") from exc
    if not version:
        raise DeliveryError(f"version source {source} has no version")
    return version


def _json_receipt(output: str, label: str) -> dict[str, Any]:
    """Read a JSON object from the whole command output or its final JSON line."""
    candidates = [output.strip()]
    candidates.extend(line.strip() for line in reversed(output.splitlines()) if line.strip())
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    raise DeliveryError(f"{label} must emit a JSON object")


def _submission_receipt(output: str, *, commit_sha: str, version: str,
                        target: str, profile: dict[str, Any],
                        idempotency_key: str = "") -> dict[str, Any]:
    """Bind an official status query to the exact object created by deployment."""
    receipt = _json_receipt(output, "store deployment")
    provider = str(profile["provider"])
    expected = {
        "provider": provider,
        "commit_sha": commit_sha,
        "version": version,
        "target": target,
        **({"app_id": str(profile["app_id"])} if provider == "app_store_connect" else {
            "package_name": str(profile["package_name"]),
            "track": str(profile["track"]),
        }),
    }
    if idempotency_key:
        expected["idempotency_key"] = idempotency_key
    mismatches = [
        f"{field} expected {wanted!r}, got {receipt.get(field)!r}"
        for field, wanted in expected.items()
        if str(receipt.get(field) or "") != wanted
    ]
    if mismatches:
        raise DeliveryError("store submission receipt mismatch: " + "; ".join(mismatches))
    provider_id = ("app_store_version_id" if provider == "app_store_connect"
                   else "version_code")
    if not str(receipt.get(provider_id) or "").strip():
        pending_apple_upload = provider == "app_store_connect" and \
            receipt.get("phase") == "build_upload" and \
            str(receipt.get("build_upload_id") or "").strip()
        if not pending_apple_upload:
            raise DeliveryError(f"store submission receipt is missing: {provider_id}")
    return receipt


def _delivery_action_key(job_id: str, provider: str, commit_sha: str,
                         version: str, target: str) -> str:
    payload = json.dumps({
        "job_id": job_id,
        "action": "store-submit",
        "provider": provider,
        "commit_sha": commit_sha,
        "version": version,
        "target": target,
    }, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _store_submit_once(db: Db, job_id: str, profile: dict[str, Any], workdir: str,
                       env: dict[str, str], *, commit_sha: str, version: str,
                       target: str) -> tuple[str, dict[str, Any]]:
    """Run a store submitter with a stable key, then durably reuse its receipt.

    The command is responsible for implementing lookup-or-create semantics for
    BASTET_DELIVERY_IDEMPOTENCY_KEY. Requiring the same key in its receipt makes
    that contract machine-checkable; a later status retry never invokes it again.
    """
    provider = str(profile["provider"])
    key = _delivery_action_key(job_id, provider, commit_sha, version, target)
    row = db.one("SELECT * FROM delivery_actions WHERE job_id=? AND action='store-submit'",
                 (job_id,))
    if row is not None and row["idempotency_key"] != key:
        raise DeliveryError(
            "stored submission action belongs to a different release identity")
    if row is not None and row["status"] == "succeeded":
        try:
            receipt = json.loads(row["receipt_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise DeliveryError("stored submission receipt is corrupt") from exc
        checked = _submission_receipt(
            json.dumps(receipt), commit_sha=commit_sha, version=version,
            target=target, profile=profile, idempotency_key=key)
        return str(row["output"] or ""), checked
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    if row is None:
        db.write(
            "INSERT INTO delivery_actions(job_id,action,provider,idempotency_key,status,"
            "started_at) VALUES(?,'store-submit',?,?, 'running',?)",
            (job_id, provider, key, stamp))
    else:
        db.write(
            "UPDATE delivery_actions SET status='running',error='',started_at=?,"
            "finished_at=NULL WHERE job_id=? AND action='store-submit'",
            (stamp, job_id))
    try:
        recovered = None
        release = {
            "commit_sha": commit_sha,
            "version": version,
            "target": target,
            "idempotency_key": key,
        }
        if str(profile.get("submission_recovery") or "command") == "official_api":
            from .store_adapters import StoreAdapterError, official_submission_lookup

            try:
                recovered = official_submission_lookup(
                    profile, release, {**os.environ, **env}, workdir)
            except StoreAdapterError as exc:
                raise DeliveryError(
                    f"official submission recovery failed closed: {exc}") from exc
        if recovered is not None:
            output = "recovered exact submission from official provider lookup"
            receipt = _submission_receipt(
                json.dumps(recovered), commit_sha=commit_sha, version=version,
                target=target, profile=profile, idempotency_key=key)
        elif str(profile.get("submission_adapter") or "command") == "official_api":
            from .store_adapters import StoreAdapterError, official_submit

            try:
                receipt = official_submit(
                    profile, release, {**os.environ, **env}, workdir)
            except StoreAdapterError as exc:
                raise DeliveryError(f"built-in store submission failed: {exc}") from exc
            output = json.dumps(receipt)
            receipt = _submission_receipt(
                output, commit_sha=commit_sha, version=version, target=target,
                profile=profile, idempotency_key=key)
        else:
            output = _run(str(profile.get("deploy_command") or ""), workdir, {
                **env, "BASTET_DELIVERY_IDEMPOTENCY_KEY": key,
            }, "store submission")
            receipt = _submission_receipt(
                output, commit_sha=commit_sha, version=version, target=target,
                profile=profile, idempotency_key=key)
    except Exception as exc:
        db.write(
            "UPDATE delivery_actions SET status='failed',error=?,finished_at=? "
            "WHERE job_id=? AND action='store-submit'",
            (f"{type(exc).__name__}: {exc}"[-8000:],
             datetime.now(UTC).isoformat(timespec="seconds"), job_id))
        raise
    action_status = "waiting_external" if receipt.get("phase") == "build_upload" \
        else "succeeded"
    db.write(
        "UPDATE delivery_actions SET status=?,output=?,receipt_json=?,"
        "error='',finished_at=? WHERE job_id=? AND action='store-submit'",
        (action_status, output, json.dumps(receipt),
         datetime.now(UTC).isoformat(timespec="seconds"), job_id))
    return output, receipt


def _verification_receipt(output: str | dict[str, Any], *, commit_sha: str, version: str,
                          target: str, profile: dict[str, Any] | None = None,
                          allow_rejected: bool = False,
                          ) -> tuple[dict[str, Any], bool]:
    """Parse and bind provider-observed state to this exact release.

    Exit zero alone is not evidence that the provider serves the requested
    artifact. The trusted verifier must emit a JSON object, either as its whole
    output or as its final non-empty line.
    """
    if isinstance(output, dict):
        receipt = output
    else:
        try:
            receipt = _json_receipt(output, "online verification")
        except DeliveryError as exc:
            raise DeliveryError(
                "online verification must emit a JSON deployment receipt") from exc
    profile = profile or {}
    provider = str(profile.get("provider") or "web")
    expected = {
        "commit_sha": commit_sha,
        "version": version,
        "target": target,
    }
    mismatches = [
        f"{field} expected {wanted!r}, got {receipt.get(field)!r}"
        for field, wanted in expected.items()
        if str(receipt.get(field) or "") != wanted
    ]
    if mismatches:
        raise DeliveryError(
            "online deployment receipt mismatch: " + "; ".join(mismatches))
    if provider == "web":
        if receipt.get("status") != "verified":
            raise DeliveryError(
                "online deployment receipt mismatch: status expected 'verified', "
                f"got {receipt.get('status')!r}")
        return receipt, True

    if receipt.get("provider") != provider:
        raise DeliveryError(
            "store receipt provider mismatch: "
            f"expected {provider!r}, got {receipt.get('provider')!r}")
    identity_fields = (["app_id"] if provider == "app_store_connect"
                       else ["package_name", "track"])
    identity_mismatches = [
        f"{field} expected {profile.get(field)!r}, got {receipt.get(field)!r}"
        for field in identity_fields
        if str(receipt.get(field) or "") != str(profile.get(field) or "")
    ]
    if identity_mismatches:
        raise DeliveryError("store receipt identity mismatch: "
                            + "; ".join(identity_mismatches))
    milestone = str(receipt.get("milestone") or "")
    provider_status = str(receipt.get("provider_status") or "")
    if milestone == "rejected":
        if allow_rejected:
            return receipt, False
        raise DeliveryError(
            f"{provider} rejected release: {provider_status or 'no provider status'}")
    if milestone not in STORE_MILESTONES:
        raise DeliveryError(
            f"store receipt milestone must be one of {sorted(STORE_MILESTONES)} "
            "or 'rejected'")
    goal = str(profile.get("release_goal") or "published")
    return receipt, STORE_MILESTONES[milestone] >= STORE_MILESTONES[goal]


def _next_poll_at(profile: dict[str, Any]) -> str:
    seconds = int(profile.get("poll_interval_seconds") or 300)
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def execute(db: Db, job, workdir: str, contract: dict,
            *, env: dict[str, str] | None = None, emit=None) -> DeliveryResult:
    """Satisfy one explicit contract or raise without declaring the job done."""
    from . import git_push

    contract = normalize(contract)
    mode = contract["mode"]
    if mode == "none":
        raise DeliveryError("no delivery was requested")
    evidence: dict[str, Any] = {}

    parked = git_push.push_job_branch(db, job, emit=emit)
    if not parked or not parked.get("pushed"):
        reason = (parked or {}).get("detail") or (parked or {}).get("reason") \
            or "job branch was not delivered"
        raise DeliveryError(str(reason))
    evidence["branch"] = {k: parked.get(k) for k in ("branch", "remote", "at")}
    commit_sha = subprocess.run(
        ["git", "-C", workdir, "rev-parse", "HEAD"], capture_output=True,
        text=True).stdout.strip()
    if mode == "branch":
        return DeliveryResult(mode, parked["branch"], "", commit_sha, evidence)

    profile = contract.get("profile") or {}
    try:
        validate_profile(profile, mode)
    except ValueError as exc:
        raise DeliveryError(str(exc)) from exc
    target_branch = str(profile.get("target_branch") or "main").strip()
    predeploy = str(profile.get("predeploy_command") or "").strip()
    if not predeploy:
        raise DeliveryError("missing pre-deploy gate command")

    expected = str(contract.get("version") or "")
    if mode == "production":
        source = str(contract.get("version_source") or "package.json")
        actual = _package_version(workdir, source)
        if actual != expected:
            raise DeliveryError(
                f"release version mismatch: contract v{expected}, {source} says v{actual}")

    base_env = {
        **(env or {}),
        "BASTET_DELIVERY_VERSION": expected,
        "BASTET_DELIVERY_TAG": f"v{expected}" if expected else "",
        "BASTET_DELIVERY_TARGET": str(profile.get("target") or target_branch),
    }

    def prepush_gate(candidate_sha: str) -> str:
        return _run(predeploy, workdir, {
            **base_env, "BASTET_DELIVERY_COMMIT": candidate_sha,
        }, "pre-deploy gate")

    integration = git_push.integrate_job_branch(
        db, job, workdir=workdir, target_branch=target_branch,
        release_tag=f"v{expected}" if expected else "", prepush_gate=prepush_gate)
    if not integration.get("pushed"):
        raise DeliveryError(str(integration.get("detail") or "main integration failed"))
    evidence["integration"] = integration
    commit_sha = integration.get("commit_sha") or commit_sha
    evidence["predeploy"] = integration.pop("gate_output", "")

    target = str(profile.get("target") or target_branch)
    if mode == "integration":
        return DeliveryResult(mode, target, "", commit_sha, evidence)

    # Commands run on the trusted host, but they still need an unambiguous
    # identity for the exact release being deployed.  This also lets an online
    # verification command reject a stale deployment instead of merely seeing
    # HTTP 200 and declaring success.
    delivery_env = {
        **base_env,
        "BASTET_DELIVERY_COMMIT": commit_sha,
    }
    if str(profile.get("status_adapter") or "command") == "official_api":
        from .store_adapters import StoreAdapterError, official_status

        evidence["deploy"], submission = _store_submit_once(
            db, job["id"], profile, workdir, delivery_env,
            commit_sha=commit_sha, version=expected, target=target)
        evidence["submission_receipt"] = submission
        try:
            evidence["verify"] = official_status(
                profile, submission, {**os.environ, **(env or {})})
        except StoreAdapterError as exc:
            raise DeliveryError(str(exc)) from exc
    else:
        evidence["deploy"] = _run(
            str(profile.get("deploy_command") or ""), workdir, delivery_env,
            "deployment")
        evidence["verify"] = _run(
            str(profile.get("verify_command") or ""), workdir, delivery_env,
            "online verification")
    receipt, complete = _verification_receipt(
        evidence["verify"], commit_sha=commit_sha, version=expected, target=target,
        profile=profile)
    if evidence.get("submission_receipt", {}).get("phase") == "build_upload":
        complete = False
    evidence["verification_receipt"] = receipt
    return DeliveryResult(
        mode, target, expected, commit_sha, evidence, complete=complete,
        provider_status=str(receipt.get("provider_status") or ""),
        next_poll_at="" if complete else _next_poll_at(profile))


def poll(workdir: str, contract: dict, delivery_row, *,
         env: dict[str, str] | None = None, db: Db | None = None) -> DeliveryResult:
    """Poll a previously submitted asynchronous store delivery without redeploying."""
    contract = normalize(contract)
    profile = validate_profile(contract.get("profile") or {}, contract["mode"])
    provider = str(profile.get("provider") or "web")
    if provider not in STORE_PROVIDERS:
        raise DeliveryError("only store providers support asynchronous polling")
    commit_sha = str(delivery_row["commit_sha"] or "")
    version = str(delivery_row["version"] or contract.get("version") or "")
    target = str(delivery_row["target"] or profile.get("target") or "")
    if not commit_sha:
        raise DeliveryError("store delivery poll has no integrated commit receipt")
    delivery_env = {
        **(env or {}),
        "BASTET_DELIVERY_VERSION": version,
        "BASTET_DELIVERY_TAG": f"v{version}",
        "BASTET_DELIVERY_TARGET": target,
        "BASTET_DELIVERY_COMMIT": commit_sha,
    }
    evidence = json.loads(delivery_row["evidence_json"] or "{}")
    if str(profile.get("status_adapter") or "command") == "official_api":
        from .store_adapters import StoreAdapterError, official_status, official_submit

        submission = evidence.get("submission_receipt")
        if not isinstance(submission, dict):
            raise DeliveryError("store delivery has no durable submission receipt")
        if submission.get("phase") == "build_upload":
            release = {
                "commit_sha": commit_sha,
                "version": version,
                "target": target,
                "idempotency_key": str(submission.get("idempotency_key") or ""),
            }
            try:
                submission = official_submit(
                    profile, release, {**os.environ, **(env or {})}, workdir)
            except StoreAdapterError as exc:
                raise DeliveryError(f"built-in store submission failed: {exc}") from exc
            submission = _submission_receipt(
                json.dumps(submission), commit_sha=commit_sha, version=version,
                target=target, profile=profile,
                idempotency_key=str(release["idempotency_key"]))
            evidence["submission_receipt"] = submission
            evidence["deploy"] = json.dumps(submission)
            if db is not None:
                action_status = "waiting_external" if \
                    submission.get("phase") == "build_upload" else "succeeded"
                db.write(
                    "UPDATE delivery_actions SET status=?,output=?,receipt_json=?,"
                    "error='',finished_at=? WHERE job_id=? AND action='store-submit'",
                    (action_status, json.dumps(submission), json.dumps(submission),
                     datetime.now(UTC).isoformat(timespec="seconds"),
                     delivery_row["job_id"]))
        try:
            output = official_status(
                profile, submission, {**os.environ, **(env or {})})
        except StoreAdapterError as exc:
            raise DeliveryError(str(exc)) from exc
    else:
        output = _run(str(profile.get("verify_command") or ""), workdir, delivery_env,
                      "store status verification")
    receipt, complete = _verification_receipt(
        output, commit_sha=commit_sha, version=version, target=target, profile=profile)
    if evidence.get("submission_receipt", {}).get("phase") == "build_upload":
        complete = False
    evidence["verify"] = output
    evidence["verification_receipt"] = receipt
    return DeliveryResult(
        contract["mode"], target, version, commit_sha, evidence,
        complete=complete, provider_status=str(receipt.get("provider_status") or ""),
        next_poll_at="" if complete else _next_poll_at(profile))
