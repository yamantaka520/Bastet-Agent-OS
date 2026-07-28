"""OS service integration: run `bastet serve` at boot/login with auto-restart.

Per platform (no root/admin required):
  Linux    systemd USER unit (~/.config/systemd/user/bastet.service),
           Restart=always; `loginctl enable-linger` makes it boot-time
           rather than login-time
  macOS    launchd LaunchAgent (~/Library/LaunchAgents/com.bastet.serve.plist),
           RunAtLoad + KeepAlive
  Windows  Task Scheduler task at logon with restart-on-failure settings
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "bastet"
LAUNCHD_LABEL = "com.bastet.serve"
WINDOWS_TASK = "BastetAgentOS"


def bastet_binary() -> str:
    """Absolute path of the `bastet` entry point that is running right now."""
    candidate = Path(sys.argv[0]).resolve()
    if candidate.name.startswith("bastet"):
        return str(candidate)
    return str(Path(sys.executable).with_name("bastet"))


def systemd_unit(binary: str) -> str:
    return f"""[Unit]
Description=Bastet Agent OS control plane
After=network-online.target

[Service]
ExecStart={binary} serve
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def launchd_plist(binary: str, log_path: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{binary}</string><string>serve</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log_path}</string>
  <key>StandardErrorPath</key><string>{log_path}</string>
</dict>
</plist>
"""


def windows_install_ps(binary: str) -> str:
    """PowerShell: logon task with restart-on-failure (the Windows analogue
    of Restart=always)."""
    return f"""$action = New-ScheduledTaskAction -Execute '{binary}' -Argument 'serve'
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName '{WINDOWS_TASK}' -Action $action `
  -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName '{WINDOWS_TASK}'
Write-Output 'installed'
"""


def _systemd_env() -> dict:
    """systemctl --user needs the session bus; SSH/su shells often lack the
    env vars even though the bus exists — reconstruct them from the uid."""
    import os

    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                   f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    return env


def _run(cmd: list[str]) -> tuple[bool, str]:
    env = _systemd_env() if cmd and cmd[0] in ("systemctl", "loginctl") else None
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output


def install() -> str:
    binary = bastet_binary()
    if sys.platform.startswith("linux"):
        unit_path = Path.home() / ".config/systemd/user" / f"{SERVICE_NAME}.service"
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(systemd_unit(binary))
        for cmd in (["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "--now", SERVICE_NAME]):
            ok, out = _run(cmd)
            if not ok:
                raise RuntimeError(f"{' '.join(cmd)} failed: {out}")
        hint = ""
        ok, _ = _run(["loginctl", "enable-linger"])
        if not ok:
            hint = ("\n注意：`loginctl enable-linger` 未成功 — 服務目前是「登入後啟動」；"
                    "要開機即啟動請手動執行一次（可能需要管理者授權）。")
        return f"systemd user service 已啟用（{unit_path}）{hint}"

    if sys.platform == "darwin":
        log_path = str(Path.home() / ".bastet" / "service.log")
        plist_path = Path.home() / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(launchd_plist(binary, log_path))
        _run(["launchctl", "unload", "-w", str(plist_path)])  # idempotent reinstall
        ok, out = _run(["launchctl", "load", "-w", str(plist_path)])
        if not ok:
            raise RuntimeError(f"launchctl load failed: {out}")
        return f"launchd LaunchAgent 已啟用（{plist_path}）"

    if sys.platform == "win32":
        ok, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        windows_install_ps(binary)])
        if not ok:
            raise RuntimeError(f"Register-ScheduledTask failed: {out}")
        return (f"Windows 排程工作 {WINDOWS_TASK} 已建立（登入啟動、失敗後每分鐘自動重啟）")

    raise RuntimeError(f"unsupported platform: {sys.platform}")


def uninstall() -> str:
    if sys.platform.startswith("linux"):
        _run(["systemctl", "--user", "disable", "--now", SERVICE_NAME])
        unit_path = Path.home() / ".config/systemd/user" / f"{SERVICE_NAME}.service"
        unit_path.unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
        return "systemd user service 已移除"
    if sys.platform == "darwin":
        plist_path = Path.home() / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        _run(["launchctl", "unload", "-w", str(plist_path)])
        plist_path.unlink(missing_ok=True)
        return "launchd LaunchAgent 已移除"
    if sys.platform == "win32":
        _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
              f"Unregister-ScheduledTask -TaskName '{WINDOWS_TASK}' -Confirm:$false"])
        return f"Windows 排程工作 {WINDOWS_TASK} 已移除"
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def status() -> str:
    if sys.platform.startswith("linux"):
        _, out = _run(["systemctl", "--user", "status", SERVICE_NAME, "--no-pager"])
        return out or "unknown"
    if sys.platform == "darwin":
        ok, out = _run(["launchctl", "list", LAUNCHD_LABEL])
        return out if ok else "not loaded"
    if sys.platform == "win32":
        _, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                       f"(Get-ScheduledTask -TaskName '{WINDOWS_TASK}').State"])
        return out or "not installed"
    return f"unsupported platform: {sys.platform}"
