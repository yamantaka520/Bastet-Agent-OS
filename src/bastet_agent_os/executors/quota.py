"""Provider-side quota probes: remaining allowance + reset time per account.

These use UNDOCUMENTED endpoints (the official CLIs expose usage only in
their interactive TUIs; feature requests upstream are open/declined), so
they may break without notice — the UI labels them accordingly.

  Claude (Pro/Max OAuth): GET api.anthropic.com/api/oauth/usage
  Codex (ChatGPT plan):   GET chatgpt.com/backend-api/codex/usage
  Grok / Antigravity / Hermes Portal: no non-interactive interface exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

TIMEOUT = httpx.Timeout(10, connect=5)

UNSUPPORTED = {"grok": "上游無非互動額度介面（TUI /usage 專用）",
               "agy": "上游無非互動額度介面",
               "hermes": "Portal 無額度查詢介面",
               "bastet-lite": "額度由 Bastet grants 管理（資源頁）"}


def fetch_quota(kind: str, home_dir: str | None) -> dict:
    """Normalized: {windows: [{label, used_percent, resets_at}], plan} |
    {error} | {unsupported}."""
    if kind in UNSUPPORTED:
        return {"unsupported": UNSUPPORTED[kind]}
    try:
        if kind in ("claude-code", "claude-sdk"):
            return _claude_quota(home_dir)
        if kind == "codex":
            return _codex_quota(home_dir)
    except httpx.HTTPError as exc:
        return {"error": f"查詢失敗：{type(exc).__name__}"}
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        return {"error": f"憑證讀取失敗：{type(exc).__name__}"}
    return {"unsupported": "未知 executor"}


def _claude_quota(home_dir: str | None) -> dict:
    creds_path = Path(home_dir or "~/.claude").expanduser() / ".credentials.json"
    if not creds_path.exists():
        return {"error": "找不到 OAuth 憑證（此帳號尚未登入，或憑證存於 macOS Keychain）"}
    creds = json.loads(creds_path.read_text())
    token = (creds.get("claudeAiOauth") or {}).get("accessToken")
    if not token:
        return {"error": "憑證檔無 OAuth token（API key 模式無帳號額度）"}
    resp = httpx.get("https://api.anthropic.com/api/oauth/usage",
                     headers={"Authorization": f"Bearer {token}",
                              "anthropic-beta": "oauth-2025-04-20"},
                     timeout=TIMEOUT)
    if resp.status_code != 200:
        return {"error": f"上游回應 {resp.status_code}"}
    data = resp.json()
    windows = []
    for key, label in (("five_hour", "5 小時"), ("seven_day", "7 天"),
                       ("seven_day_opus", "7 天 Opus"),
                       ("seven_day_sonnet", "7 天 Sonnet")):
        window = data.get(key)
        if window:
            windows.append({"label": label,
                            "used_percent": round(float(window.get("utilization") or 0)
                                                  * 100, 1),
                            "resets_at": window.get("resets_at")})
    return {"windows": windows, "plan": "Claude 訂閱",
            "note": "非官方介面，可能隨時失效"}


def _codex_quota(home_dir: str | None) -> dict:
    auth_path = Path(home_dir or "~/.codex").expanduser() / "auth.json"
    if not auth_path.exists():
        return {"error": "找不到憑證（此帳號尚未 codex login）"}
    auth = json.loads(auth_path.read_text())
    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token")
    account_id = tokens.get("account_id") or auth.get("account_id")
    if not access:
        return {"error": "auth.json 無 access token"}
    headers = {"Authorization": f"Bearer {access}"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    resp = httpx.get("https://chatgpt.com/backend-api/codex/usage",
                     headers=headers, timeout=TIMEOUT)
    if resp.status_code != 200:
        return {"error": f"上游回應 {resp.status_code}"}
    data = resp.json()
    rate = data.get("rate_limit") or {}
    windows = []
    for key, label in (("primary_window", "主要視窗"),
                       ("secondary_window", "次要視窗")):
        window = rate.get(key)
        if window:
            windows.append({"label": label,
                            "used_percent": round(float(window.get("used_percent")
                                                        or 0), 1),
                            "resets_at": window.get("reset_at")})
    return {"windows": windows, "plan": data.get("plan_type") or "ChatGPT 方案",
            "note": "非官方介面，可能隨時失效"}
