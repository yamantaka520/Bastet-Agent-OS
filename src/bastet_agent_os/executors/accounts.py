"""Executor accounts: multiple logins per executor kind.

Each account owns an isolated profile directory under
~/.bastet/executor-profiles/<account_id>/, exported as the CLI's home/config
env var at run time — so two agents can drive the same executor with
different subscriptions/keys. Login itself stays an interactive step done in
the operator's own terminal (OAuth flows need a browser + keychain); Bastet
generates the exact command and reports whether the profile looks logged in.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# executor kind -> env var that relocates its config/home directory
HOME_ENV = {
    "claude-code": "CLAUDE_CONFIG_DIR",
    "claude-sdk": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
    "grok": "GROK_HOME",
    "hermes": "HERMES_HOME",  # merged with the gateway provider profile at run time
}

# kinds whose upstream has no per-directory auth (global login only)
GLOBAL_AUTH_ONLY = {"agy"}
# kinds that never need an account (credentials come from Bastet resources)
NO_ACCOUNT = {"bastet-lite"}

EXECUTOR_CATALOG = [
    {"kind": "claude-code", "name": "Claude Code (headless)", "binary": "claude"},
    {"kind": "claude-sdk", "name": "Claude Code (Agent SDK, in-run approvals)",
     "binary": "claude"},
    {"kind": "codex", "name": "OpenAI Codex CLI", "binary": "codex"},
    {"kind": "hermes", "name": "NousResearch Hermes", "binary": "hermes"},
    {"kind": "grok", "name": "xAI Grok Build", "binary": "grok"},
    {"kind": "agy", "name": "Google Antigravity", "binary": "agy"},
    {"kind": "bastet-lite", "name": "bastet-lite (built-in)", "binary": None},
]


def login_instruction(kind: str, home_dir: str) -> str:
    """The exact terminal command that logs this profile in (verified against
    each tool's official docs — interactive by design, never automated)."""
    if kind in ("claude-code", "claude-sdk"):
        return f'CLAUDE_CONFIG_DIR="{home_dir}" claude  # 進入後輸入 /login'
    if kind == "codex":
        return f'CODEX_HOME="{home_dir}" codex login'
    if kind == "grok":
        return f'GROK_HOME="{home_dir}" grok  # 首次執行自動開瀏覽器認證'
    if kind == "hermes":
        return (f'HERMES_HOME="{home_dir}" hermes setup  '
                "# 或直接把 provider keys 寫進該目錄的 config.yaml")
    if kind == "agy":
        return "agy  # Antigravity 只有全域 Google 登入，不支援多帳號目錄"
    return "（此 executor 不需要帳號 — 憑證來自 Bastet 資源池）"


def profile_status(kind: str, home_dir: str) -> str:
    """Cheap, offline signal: does the profile look configured?"""
    path = Path(home_dir)
    if not path.exists():
        return "missing"
    if any(path.iterdir()):
        return "configured"
    return "empty"


def catalog_with_availability() -> list[dict]:
    rows = []
    for entry in EXECUTOR_CATALOG:
        rows.append({
            **entry,
            "installed": bool(entry["binary"] is None
                              or shutil.which(entry["binary"])),
            "supports_accounts": entry["kind"] in HOME_ENV,
            "auth_note": ("global-only" if entry["kind"] in GLOBAL_AUTH_ONLY
                          else "resource" if entry["kind"] in NO_ACCOUNT
                          else "per-account"),
        })
    return rows


def account_env(kind: str, home_dir: str | None) -> dict[str, str]:
    """extra_env entries that pin a run to an account's profile directory."""
    if not home_dir or kind not in HOME_ENV:
        return {}
    return {HOME_ENV[kind]: home_dir}


def ensure_profile_dir(root: Path, account_id: str) -> str:
    path = root / "executor-profiles" / account_id
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return str(path)
