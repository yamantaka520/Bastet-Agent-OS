"""Durable, deterministic job delivery.

An Agent finishing its prose/code stage is not proof that anything reached a
remote branch, target branch, or production. Delivery contracts are executed by
Bastet's trusted host process and leave an immutable receipt. A required delivery that
fails keeps the card blocked and preserves its worktree for repair/retry.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import Db

MODES = {"none", "branch", "integration", "production"}
COMMAND_TIMEOUT_S = 1800
OUTPUT_LIMIT = 8000


class DeliveryError(RuntimeError):
    pass


@dataclass
class DeliveryResult:
    mode: str
    target: str
    version: str
    commit_sha: str
    evidence: dict[str, Any]


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
    required_commands = ["predeploy_command"]
    if mode == "production":
        required_commands.extend(["deploy_command", "verify_command"])
    missing = [key for key in required_commands
               if not str(profile.get(key) or "").strip()]
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


def _verification_receipt(output: str, *, commit_sha: str, version: str,
                          target: str) -> dict[str, Any]:
    """Parse and bind provider-observed state to this exact release.

    Exit zero alone is not evidence that the provider serves the requested
    artifact. The trusted verifier must emit a JSON object, either as its whole
    output or as its final non-empty line.
    """
    candidates = [output.strip()]
    candidates.extend(line.strip() for line in reversed(output.splitlines())
                      if line.strip())
    receipt = None
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            receipt = value
            break
    if receipt is None:
        raise DeliveryError(
            "online verification must emit a JSON deployment receipt")
    expected = {
        "status": "verified",
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
    return receipt


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
    evidence["deploy"] = _run(
        str(profile.get("deploy_command") or ""), workdir, delivery_env, "deployment")
    evidence["verify"] = _run(
        str(profile.get("verify_command") or ""), workdir, delivery_env,
        "online verification")
    evidence["verification_receipt"] = _verification_receipt(
        evidence["verify"], commit_sha=commit_sha, version=expected, target=target)
    return DeliveryResult(mode, target, expected, commit_sha, evidence)
