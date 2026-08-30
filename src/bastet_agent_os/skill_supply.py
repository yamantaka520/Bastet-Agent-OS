"""Verified Skill packages and workflow capability resolution.

Legacy source-only Skill resources remain useful as prompt assets.  A Skill
with ``skill_id`` is a managed execution capability: it must be installed,
healthy, digest-verified, compatible with the selected executor, and granted
to the project before a stage declaring ``requires: [skill:<id>]`` may run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def effective_path(config: dict[str, Any]) -> Path | None:
    raw = (config.get("skill_target") or config.get("skill_source") or "").strip()
    if not raw or raw.startswith(("http://", "https://", "git@")):
        return None
    return Path(raw).expanduser().resolve()


def digest_path(path: Path) -> str:
    """Stable sha256 for one file or a directory tree (metadata excluded)."""
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
    else:
        for item in sorted(p for p in path.rglob("*") if p.is_file()):
            if any(part in {".git", "__pycache__"} for part in item.parts):
                continue
            digest.update(item.relative_to(path).as_posix().encode())
            digest.update(b"\0")
            digest.update(item.read_bytes())
            digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def normalise_digest(value: str) -> str:
    value = (value or "").strip().lower()
    return value if not value or value.startswith("sha256:") else f"sha256:{value}"


def compatible(config: dict[str, Any], executor_type: str) -> bool:
    declared = [part.strip() for part in
                (config.get("compatible_executors") or "*").split(",") if part.strip()]
    return "*" in declared or executor_type in declared


def capability_id(value: str) -> str | None:
    return value.split(":", 1)[1].strip() if value.startswith("skill:") else None


def contract_complete(config: dict[str, Any]) -> bool:
    return all((config.get(key) or "").strip() for key in (
        "skill_id", "skill_version", "skill_source", "skill_target",
        "skill_digest", "compatible_executors", "install_command"))


def resolve(db, project_id: str, team_id: str, executor_type: str,
            requirement: str):
    """Return (available, provider, detail) for one managed Skill requirement."""
    wanted = capability_id(requirement)
    if wanted is None:
        raise ValueError(f"not a Skill capability: {requirement}")
    from .resource_access import visible

    candidates = []
    for row in visible(db, project_id, team_id):
        if row["kind"] != "skill":
            continue
        config = json.loads(row["config_json"] or "{}")
        if (config.get("skill_id") or "").strip() == wanted:
            candidates.append((row, config))
    if not candidates:
        return False, "resource-pool", (
            f"no enabled, granted Skill resource supplies {wanted!r}; "
            "create/grant it in Resources, then install and test it")
    reasons = []
    for row, config in candidates:
        if not contract_complete(config):
            reasons.append(f"{row['name']}: managed Skill contract is incomplete")
            continue
        if not compatible(config, executor_type):
            reasons.append(f"{row['name']}: incompatible with {executor_type}")
            continue
        install = config.get("install") or {}
        test = config.get("test") or {}
        if install.get("status") != "installed":
            reasons.append(f"{row['name']}: not installed")
            continue
        if test.get("status") != "ok":
            reasons.append(f"{row['name']}: health is {test.get('status', 'unknown')}")
            continue
        expected = normalise_digest(config.get("skill_digest") or "")
        actual = normalise_digest(test.get("digest") or install.get("digest") or "")
        if expected and actual != expected:
            reasons.append(f"{row['name']}: verified digest does not match declaration")
            continue
        return True, f"resource:{row['id']}", (
            f"{row['name']} {config.get('skill_version') or 'unversioned'} "
            f"is installed and healthy for {executor_type}; digest {actual or 'unrecorded'}")
    return False, "resource-pool", "; ".join(reasons)


def usable(config: dict[str, Any], executor_type: str = "") -> bool:
    """Whether a managed Skill may be advertised to an executor."""
    if not (config.get("skill_id") or "").strip():
        return True  # legacy prompt asset, never satisfies a requires contract
    if not contract_complete(config):
        return False
    if executor_type and not compatible(config, executor_type):
        return False
    return ((config.get("install") or {}).get("status") == "installed"
            and (config.get("test") or {}).get("status") == "ok")
