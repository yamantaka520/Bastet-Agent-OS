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

# model lists: curated from each tool's current lineup; the empty choice means
# "official default" (no --model flag passed)
EXECUTOR_CATALOG = [
    {"kind": "claude-code", "name": "Claude Code (headless)", "binary": "claude",
     "config_dir": "~/.claude",
     "models": ["sonnet", "opus", "haiku"]},
    {"kind": "claude-sdk", "name": "Claude Code (Agent SDK, in-run approvals)",
     "binary": "claude", "config_dir": "~/.claude",
     "models": ["sonnet", "opus", "haiku"]},
    {"kind": "codex", "name": "OpenAI Codex CLI", "binary": "codex",
     "config_dir": "~/.codex",
     "models": ["gpt-5.1-codex", "gpt-5.1-codex-mini"]},
    {"kind": "hermes", "name": "NousResearch Hermes", "binary": "hermes",
     "config_dir": "~/.hermes",
     "models": []},   # model comes from the gateway resource routing
    {"kind": "grok", "name": "xAI Grok Build", "binary": "grok",
     "config_dir": "~/.grok",
     "models": ["grok-code-fast-1", "grok-4", "grok-4-1-fast", "grok-3"]},
    {"kind": "agy", "name": "Google Antigravity", "binary": "agy",
     "config_dir": "~/.gemini",
     "models": ["gemini-3.6-flash-high", "gemini-3.6-flash-medium",
                "gemini-3.6-flash-low", "gemini-3.1-pro-high",
                "gemini-3.1-pro-low", "claude-sonnet-4-6",
                "claude-opus-4-6-thinking", "gpt-oss-120b-medium"]},
    {"kind": "bastet-lite", "name": "bastet-lite (built-in)", "binary": None,
     "config_dir": None,
     "models": []},   # model comes from the gateway resource routing
]


def login_command(kind: str, home_dir: str | None) -> tuple[dict[str, str], list[str]] | None:
    """(env, argv) that runs this executor's login flow — device-code /
    URL-paste variants preferred so the flow survives a web terminal.
    home_dir=None means the global (default-profile) login."""
    env = {}
    if home_dir and kind in HOME_ENV:
        env = {HOME_ENV[kind]: home_dir}
    if kind in ("claude-code", "claude-sdk"):
        return env, ["claude", "/login"]
    if kind == "codex":
        # verified against codex 0.145.0: the flag is --device-auth
        # (docs still say --device-code, which the binary rejects)
        return env, ["codex", "login", "--device-auth"]
    if kind == "grok":
        return env, ["grok", "login", "--device-auth"]
    if kind == "agy":
        return {}, ["agy"]  # full TUI — the wizard is a real terminal (xterm.js)
    if kind == "hermes":
        return env, ["hermes", "setup"]
    return None


# kinds whose login TUI must not use the alternate screen in a web terminal
STRIP_ALT_SCREEN = {"agy"}


def login_instruction(kind: str, home_dir: str | None) -> str:
    """Human-readable command line for running the login in a terminal."""
    command = login_command(kind, home_dir)
    if command is None:
        return "（此 executor 不需要帳號 — 憑證來自 Bastet 資源池）"
    env, argv = command
    prefix = " ".join(f'{k}="{v}"' for k, v in env.items())
    line = (prefix + " " if prefix else "") + " ".join(argv)
    notes = {
        "codex": "  # device code：用手機或任何瀏覽器輸入代碼即可",
        "grok": "  # device auth：無需本機瀏覽器",
        "agy": "  # 全域 Google OAuth（Antigravity 不支援多帳號目錄）",
        "hermes": "  # 供應商/模型設定精靈",
    }
    if kind in ("claude-code", "claude-sdk"):
        return line.replace(" /login", "") + "  # 進入後輸入 /login"
    return line + notes.get(kind, "")


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
        installed = bool(entry["binary"] is None or shutil.which(entry["binary"]))
        # "configured" = the CLI's default config/auth dir exists with content;
        # the one-click installer installs everything, so login/setup state is
        # the signal users actually need
        config_dir = entry.get("config_dir")
        if config_dir is None:
            configured = True  # bastet-lite: credentials come from resources
        else:
            path = Path(config_dir).expanduser()
            configured = path.exists() and any(path.iterdir())
        rows.append({
            **entry,
            "installed": installed,
            "configured": installed and configured,
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
