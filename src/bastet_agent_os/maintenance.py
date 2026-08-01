"""Keeping the moving parts current: Bastet, AMOS, and the executor CLIs.

Bastet orchestrates other people's tools, so "is it up to date?" is a question
about a dozen things installed in different ways — pip into our venv, official
shell installers, npm. This module answers it per component, and applies an
update only when asked. Nothing self-updates: an orchestrator that silently
changes the agents underneath a running project is not something you can reason
about.

Every check reports what it actually knows. When a component cannot tell us its
available version (an installer script with no version endpoint), we say
`unknown` rather than implying it is current.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any

from .db import now

CHECK_TIMEOUT_S = 90
UPDATE_TIMEOUT_S = 900

# pip-installed into Bastet's own venv. `in_process` means the running server
# imports it, so a new version only takes effect after a restart — pytest is
# spawned as a subprocess by gate commands, so it takes effect immediately.
PIP_COMPONENTS = [
    # released on PyPI since 0.19.0 — so the index comparison is real and the
    # state stops being `unknown`; updates install the released version, not
    # whatever main happens to be
    {"id": "bastet-agent-os", "label": "Bastet Agent OS", "kind": "pip",
     "package": "bastet-agent-os", "in_process": True},
    {"id": "agent-memory-os", "label": "Agent Memory OS", "kind": "pip",
     "package": "agent-memory-os", "extras": "[full]", "in_process": True},
    {"id": "claude-agent-sdk", "label": "Claude Agent SDK", "kind": "pip",
     "package": "claude-agent-sdk", "in_process": True},
    # AMOS's semantic recall backend. It arrives with agent-memory-os[full], but
    # it is a separate package that can silently be absent — and when it is,
    # recall quietly degrades to lexical matching with no error anywhere. Listing
    # it is the only way to see which mode you are actually running in.
    {"id": "turbovec", "label": "turbovec（AMOS 語意向量索引）", "kind": "pip",
     "package": "turbovec", "in_process": True},
    {"id": "numpy", "label": "numpy（語意索引相依）", "kind": "pip",
     "package": "numpy", "in_process": True},
    {"id": "pytest", "label": "pytest（工作流測試關卡）", "kind": "pip",
     "package": "pytest"},
]

# executor CLIs, each with the vendor's own installer
CLI_COMPONENTS = [
    {"id": "claude", "label": "Claude Code", "kind": "cli", "program": "claude",
     "version_args": ["--version"],
     "update": "curl -fsSL https://claude.ai/install.sh | bash"},
    {"id": "codex", "label": "Codex CLI", "kind": "cli", "program": "codex",
     "version_args": ["--version"],
     "update": "curl -fsSL https://chatgpt.com/codex/install.sh | bash"},
    {"id": "grok", "label": "Grok Build CLI", "kind": "cli", "program": "grok",
     "version_args": ["--version"],
     "update": "curl -fsSL https://x.ai/cli/install.sh | bash"},
    {"id": "agy", "label": "Google Antigravity (agy)", "kind": "cli",
     "program": "agy", "version_args": ["--version"],
     "update": "curl -fsSL https://antigravity.google/cli/install.sh | bash"},
    {"id": "hermes", "label": "Hermes Agent", "kind": "cli", "program": "hermes",
     "version_args": ["--version"],
     "update": "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"},
]

COMPONENTS = PIP_COMPONENTS + CLI_COMPONENTS
BY_ID = {c["id"]: c for c in COMPONENTS}


def _run(command: list[str] | str, timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(command, shell=isinstance(command, str),
                              capture_output=True, text=True, timeout=timeout,
                              env={**os.environ})
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _pip() -> list[str]:
    return [sys.executable, "-m", "pip"]


def _version_of(program: str, args: list[str]) -> str | None:
    path = shutil.which(program)
    if not path:
        return None
    code, output = _run([path, *args], 30)
    if code != 0:
        return "installed"          # it exists but will not say which version
    match = re.search(r"\d+\.\d+(?:\.\d+)?(?:[-.\w]+)?", output)
    return match.group(0) if match else (output.strip().splitlines() or ["installed"])[0]


def check(component_id: str) -> dict[str, Any]:
    """Installed vs available for one component. Never guesses."""
    spec = BY_ID.get(component_id)
    if spec is None:
        raise ValueError(f"unknown component {component_id!r}")
    row: dict[str, Any] = {"id": spec["id"], "label": spec["label"],
                           "kind": spec["kind"], "checked_at": now()}
    if spec["kind"] == "pip":
        code, output = _run([*_pip(), "index", "versions", spec["package"]],
                            CHECK_TIMEOUT_S)
        installed = None
        got, show = _run([*_pip(), "show", spec["package"]], 45)
        if got == 0:
            match = re.search(r"^Version:\s*(\S+)", show, re.M)
            installed = match.group(1) if match else None
        available = None
        if code == 0:
            match = re.search(r"LATEST:\s*(\S+)", output) or \
                re.search(r"Available versions:\s*(\S+?)[,\s]", output)
            available = match.group(1) if match else None
        row.update({"installed": installed, "available": available,
                    "source": spec.get("source") or spec["package"]})
        if installed is None:
            row["state"] = "missing"
        elif available and available != installed:
            row["state"] = "outdated"
        elif available:
            row["state"] = "current"
        else:
            # a git source has no index to compare against; saying "current"
            # would be a guess, so we do not
            row["state"] = "unknown"
        return row

    installed = _version_of(spec["program"], spec["version_args"])
    row.update({"installed": installed, "available": None,
                "source": spec["update"],
                "state": "missing" if installed is None else "unknown"})
    return row


def check_all() -> list[dict[str, Any]]:
    return [check(spec["id"]) for spec in COMPONENTS]


def update(db, component_id: str, actor: str) -> dict[str, Any]:
    """Apply an update. Admin-only, audited, and never implicit."""
    spec = BY_ID.get(component_id)
    if spec is None:
        raise ValueError(f"unknown component {component_id!r}")
    before = check(component_id)
    if spec["kind"] == "pip":
        target = spec.get("source") or f"{spec['package']}{spec.get('extras', '')}"
        command: list[str] | str = [*_pip(), "install", "--upgrade", target]
    else:
        command = spec["update"]
    db.audit(actor, "maintenance.update.start", "component", component_id,
             {"command": command if isinstance(command, str) else " ".join(command)})
    code, output = _run(command, UPDATE_TIMEOUT_S)
    after = check(component_id)
    status = "updated" if code == 0 else "failed"
    if code == 0 and before.get("installed") == after.get("installed"):
        status = "unchanged"        # ran fine, nothing new — say that plainly
    db.audit(actor, f"maintenance.update.{status}", "component", component_id,
             {"exit_code": code, "from": before.get("installed"),
              "to": after.get("installed"), "log_tail": output[-500:]})
    return {"id": component_id, "status": status, "exit_code": code,
            "from": before.get("installed"), "to": after.get("installed"),
            "log": output[-4000:], "component": after,
            "restart_required": status == "updated"
                                and bool(spec.get("in_process"))}


def update_all(db, actor: str) -> dict[str, Any]:
    results = [update(db, spec["id"], actor) for spec in COMPONENTS]
    return {"results": results,
            "updated": [r["id"] for r in results if r["status"] == "updated"],
            "failed": [r["id"] for r in results if r["status"] == "failed"],
            "restart_required": any(r["restart_required"] for r in results)}


def semantic_status() -> dict[str, Any]:
    """Is AMOS recalling by meaning, or just by keyword?

    turbovec + numpy turn on vector recall. When they are missing AMOS does not
    complain — it falls back to lexical matching, so recall keeps "working" and
    quietly stops finding anything that does not share words with the query.
    That distinction is invisible unless something says it out loud."""
    try:
        from agent_memory_os.providers.turbovec import semantic_backend_available
        available = bool(semantic_backend_available())
    except Exception:
        available = False
    return {"semantic": available,
            "mode": "vector" if available else "lexical",
            "install": "pip install 'agent-memory-os[semantic]'"}


def amos_web(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Where the full AMOS console lives, and how to start it if it does not.

    Bastet's memory tab is a lens; AMOS ships a complete UI and pointing at it
    beats reimplementing it badly."""
    configured = (cfg or {}).get("amos_web_url") or os.environ.get("AMOS_WEB_URL", "")
    return {"url": configured,
            "command": "agent-memory-web --host 0.0.0.0 --port 8000",
            "installed": bool(shutil.which("agent-memory-web")
                              or (json.dumps(sys.path) and
                                  shutil.which("agent-memory-web")))}
