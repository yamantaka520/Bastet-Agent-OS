"""Pi executor driven by a fake JSONL-emitting binary."""

import json
import os
import stat

import pytest

from bastet_agent_os.executors.base import TaskSpec
from bastet_agent_os.executors.pi_agent import PiExecutor

FAKE_PI = """#!/bin/sh
{
  printf 'ARGS:'; printf ' %s' "$@"; printf '\n'
  printf 'PROFILE:%s\n' "$PI_CODING_AGENT_DIR"
  printf 'TOKEN:%s\n' "$BASTET_RUN_TOKEN"
} > "$FAKE_LOG"
[ -z "$FAKE_SLEEP" ] || sleep "$FAKE_SLEEP"
cat "$FAKE_EVENTS"
exit ${FAKE_EXIT:-0}
"""


@pytest.fixture
def fake_pi(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "pi"
    binary.write_text(FAKE_PI)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "pi.log"
    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_EVENTS", str(events))

    def output(*rows):
        events.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    return output, log


def spec(tmp_path, **kw):
    return TaskSpec(**{**dict(run_id="r1", prompt="fix it",
                              workdir=str(tmp_path)), **kw})


async def drive(task):
    executor = PiExecutor()
    handle = await executor.start(task)
    events = [event async for event in executor.stream(handle)]
    return await executor.result(handle), handle, events


async def test_direct_jsonl_run_reports_progress_usage_and_tools(
        fake_pi, tmp_path, monkeypatch):
    output, log = fake_pi
    profile = tmp_path / "account"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(profile))
    usage = {"input": 100, "output": 20, "cacheRead": 4, "cacheWrite": 2,
             "cost": {"total": 0.012}}
    output(
        {"type": "session", "id": "pi-session"},
        {"type": "message_update", "usage": usage,
         "assistantMessageEvent": {"type": "text_delta", "delta": "working"}},
        {"type": "tool_execution_start", "toolName": "read", "toolCallId": "t1"},
        {"type": "message_end", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "done"}],
            "usage": usage, "stopReason": "stop"}},
    )

    result, handle, events = await drive(spec(tmp_path))

    assert result.status == "succeeded" and result.summary == "done"
    assert (result.tokens_in, result.tokens_out, result.cache_read,
            result.cache_write, result.cost_usd) == (100, 20, 4, 2, 0.012)
    assert handle.session_id == "pi-session"
    assert any(event.type == "progress" for event in events)
    assert any(event.type == "tool_call_summary" for event in events)
    text = log.read_text()
    assert "--mode json" in text and "--no-session" in text
    assert "--no-approve" in text and "--no-context-files" in text
    assert "--tools read,bash,edit,write,grep,find,ls" in text
    assert f"PROFILE:{profile}" in text


async def test_read_only_uses_a_real_tool_allowlist_and_parses_verdict(
        fake_pi, tmp_path):
    output, log = fake_pi
    output({"type": "message_end", "message": {
        "role": "assistant",
        "content": [{"type": "text", "text":
                     '{"verdict":"approve","reasons":["clean"]}'}],
        "usage": {}, "stopReason": "stop"}})

    result, _, _ = await drive(spec(tmp_path, read_only=True, expect_verdict=True))

    assert result.structured_verdict == {"verdict": "approve", "reasons": ["clean"]}
    assert "--tools read,grep,find,ls" in log.read_text()


async def test_gateway_profile_uses_an_env_reference_not_the_secret(fake_pi, tmp_path):
    output, log = fake_pi
    output({"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "ok"}],
        "usage": {}, "stopReason": "stop"}})
    task = spec(tmp_path, gateway_url="http://127.0.0.1:8890",
                run_token="brt_secret",
                llm={"flavor": "openai", "model": "gpt-test"})

    result, _, _ = await drive(task)

    assert result.status == "succeeded"
    profile = tmp_path / "._bastet" / "pi-agent" / "models.json"
    text = profile.read_text()
    assert '"baseUrl": "http://127.0.0.1:8890/v1"' in text
    assert '"apiKey": "$BASTET_RUN_TOKEN"' in text
    assert "brt_secret" not in text
    logged = log.read_text()
    assert "--provider bastet --model gpt-test" in logged
    assert "TOKEN:brt_secret" in logged


async def test_gateway_contract_rejects_partial_or_unknown_routes(tmp_path):
    with pytest.raises(ValueError, match="both URL and run token"):
        await PiExecutor().start(spec(tmp_path, gateway_url="http://gw"))
    with pytest.raises(ValueError, match="openai/anthropic"):
        await PiExecutor().start(spec(
            tmp_path, gateway_url="http://gw", run_token="x",
            llm={"flavor": "google", "model": "m"}))


async def test_handle_is_restart_serializable_and_cancel_kills_process_group(
        fake_pi, tmp_path, monkeypatch):
    output, _ = fake_pi
    output()
    monkeypatch.setenv("FAKE_SLEEP", "60")
    executor = PiExecutor()
    handle = await executor.start(spec(tmp_path))

    json.dumps(handle.state())
    await executor.cancel(handle)
    result = await executor.result(handle)

    assert result.status == "cancelled"
    assert handle.process.returncode is not None
