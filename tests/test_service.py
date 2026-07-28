"""Service artifacts: unit/plist/task content (no system calls)."""

from bastet_agent_os.service import launchd_plist, systemd_unit, windows_install_ps


def test_systemd_unit_restarts_always():
    unit = systemd_unit("/opt/venv/bin/bastet")
    assert "ExecStart=/opt/venv/bin/bastet serve" in unit
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit  # user service, not system


def test_launchd_plist_keepalive():
    plist = launchd_plist("/usr/local/bin/bastet", "/tmp/log")
    assert "<string>/usr/local/bin/bastet</string>" in plist
    assert "<key>KeepAlive</key><true/>" in plist
    assert "<key>RunAtLoad</key><true/>" in plist


def test_windows_task_restart_on_failure():
    ps = windows_install_ps(r"C:\venv\Scripts\bastet.exe")
    assert "New-ScheduledTaskTrigger -AtLogOn" in ps
    assert "-RestartCount 999" in ps          # the Restart=always analogue
    assert "-RestartInterval" in ps
    assert "BastetAgentOS" in ps
