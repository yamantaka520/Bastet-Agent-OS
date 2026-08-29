"""OpenClaw executor driven by a fake stable JSON-envelope binary."""

import json
import os
import stat

import pytest

from bastet_agent_os.executors.base import TaskSpec
from bastet_agent_os.executors.openclaw import OpenClawExecutor

FAKE_OPENCLAW = """#!/bin/sh
{
  printf 'ARGS:'; printf ' %s' "$@"; printf '\n'
  printf 'HOME:%s\n' "$OPENCLAW_HOME"
} > "$FAKE_LOG"
[ -z "$FAKE_SLEEP" ] || sleep "$FAKE_SLEEP"
cat "$FAKE_RESULT"
exit ${FAKE_EXIT:-0}
"""


@pytest.fixture
def fake_openclaw(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "openclaw"
    binary.write_text(FAKE_OPENCLAW)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "openclaw.log"
    result = tmp_path / "result.json"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_RESULT", str(result))

    def output(payload):
        result.write_text(json.dumps(payload) + "\n")

    return output, log


def spec(tmp_path, **kw):
    return TaskSpec(**{**dict(run_id="r1", prompt="implement it",
                              workdir=str(tmp_path)), **kw})


async def drive(task):
    executor = OpenClawExecutor()
    handle = await executor.start(task)
    events = [event async for event in executor.stream(handle)]
    return await executor.result(handle), handle, events


async def test_agent_exec_success_reports_usage_and_uses_isolated_state(
        fake_openclaw, tmp_path, monkeypatch):
    output, log = fake_openclaw
    profile = tmp_path / "openclaw-account"
    monkeypatch.setenv("OPENCLAW_HOME", str(profile))
    output({"ok": True, "status": "ok", "final": "tests pass",
            "usage": {"input": 120, "output": 8, "total": 128},
            "costUsd": 0.0021, "sessionId": "s1"})

    result, handle, events = await drive(spec(tmp_path, context_text="rules"))

    assert result.status == "succeeded" and result.summary == "tests pass"
    assert (result.tokens_in, result.tokens_out, result.cost_usd) == (120, 8, 0.0021)
    assert any(event.type == "activity" for event in events)
    text = log.read_text()
    assert "agent exec" in text and "--json" in text and "--isolated" in text
    assert "--code-mode code" in text and "--cwd" in text
    assert f"HOME:{profile}" in text
    assert "<context>\nrules\n</context>" in handle.prompt_file.read_text()


async def test_error_envelope_outranks_missing_final(fake_openclaw, tmp_path,
                                                     monkeypatch):
    output, _ = fake_openclaw
    output({"ok": False, "status": "error",
            "error": {"message": "auth expired", "kind": "auth"}})
    monkeypatch.setenv("FAKE_EXIT", "1")

    result, _, _ = await drive(spec(tmp_path))

    assert result.status == "failed" and "auth expired" in result.summary


async def test_read_only_and_gateway_are_refused_before_process_start(tmp_path):
    with pytest.raises(ValueError, match="read-only"):
        await OpenClawExecutor().start(spec(tmp_path, read_only=True))
    with pytest.raises(ValueError, match="Gateway"):
        await OpenClawExecutor().start(
            spec(tmp_path, gateway_url="http://gw", run_token="x"))


async def test_handle_is_restart_serializable_and_cancel_kills_process_group(
        fake_openclaw, tmp_path, monkeypatch):
    output, _ = fake_openclaw
    output({"ok": True, "status": "ok", "final": "late"})
    monkeypatch.setenv("FAKE_SLEEP", "60")
    executor = OpenClawExecutor()
    handle = await executor.start(spec(tmp_path))

    json.dumps(handle.state())
    await executor.cancel(handle)
    result = await executor.result(handle)

    assert result.status == "cancelled"
    assert handle.process.returncode is not None
