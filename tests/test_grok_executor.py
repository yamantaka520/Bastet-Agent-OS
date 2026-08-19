"""grok executor, driven by a fake `grok` binary emitting NDJSON."""

import json
import os
import stat

import pytest

from bastet_agent_os.executors.base import TaskSpec
from bastet_agent_os.executors.grok import GrokExecutor

FAKE_GROK = """#!/bin/sh
{ printf 'ARGS:'; printf ' %s' "$@"; printf '\\n'
  printf 'BASE:%s\\n' "$GROK_MODELS_BASE_URL"
  printf 'KEY:%s\\n' "$XAI_API_KEY"; } > "$FAKE_LOG"
cat "$FAKE_EVENTS"
exit ${FAKE_EXIT:-0}
"""


@pytest.fixture
def fake_grok(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "grok"
    script.write_text(FAKE_GROK)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "grok.log"))
    events = tmp_path / "events.ndjson"
    monkeypatch.setenv("FAKE_EVENTS", str(events))

    def set_output(lines):
        events.write_text("\n".join(json.dumps(line) if isinstance(line, dict) else line
                                    for line in lines) + "\n")

    return set_output, tmp_path / "grok.log"


def spec(tmp_path, **kw) -> TaskSpec:
    return TaskSpec(**{**dict(run_id="r1", prompt="do it", workdir=str(tmp_path)), **kw})


async def drive(task):
    executor = GrokExecutor()
    handle = await executor.start(task)
    async for _ in executor.stream(handle):
        pass
    return await executor.result(handle), handle


async def test_streaming_success(fake_grok, tmp_path):
    set_output, log = fake_grok
    set_output([
        {"type": "thought", "text": "hmm"},
        {"type": "text", "text": "patched the bug"},
        {"type": "end", "stopReason": "EndTurn", "sessionId": "sess42"},
    ])
    result, handle = await drive(spec(tmp_path))
    assert result.status == "succeeded"
    assert "patched the bug" in result.summary
    assert handle.session_id == "sess42"
    args = log.read_text()
    assert "--always-approve" in args and "--output-format streaming-json" in args
    assert "--no-auto-update" in args


async def test_error_event_fails(fake_grok, tmp_path):
    set_output, _ = fake_grok
    set_output([{"type": "error", "message": "auth expired"}])
    result, _ = await drive(spec(tmp_path))
    assert result.status == "failed" and "auth expired" in result.summary


async def test_review_uses_json_schema_verdict(fake_grok, tmp_path):
    set_output, log = fake_grok
    set_output([{"text": json.dumps({"verdict": "approve", "reasons": ["clean"]}),
                 "stopReason": "EndTurn", "sessionId": "s1"}])
    result, _ = await drive(spec(tmp_path, read_only=True, expect_verdict=True))
    assert result.structured_verdict == {"verdict": "approve", "reasons": ["clean"]}
    args = log.read_text()
    assert "--tools read_file,grep,list_dir" in args   # real read-only toolset
    assert "--json-schema" in args and "--always-approve" not in args


async def test_gateway_path_env(fake_grok, tmp_path):
    set_output, log = fake_grok
    set_output([{"type": "end", "stopReason": "EndTurn", "sessionId": "s"}])
    result, _ = await drive(spec(tmp_path, gateway_url="http://127.0.0.1:8890",
                                 run_token="brt_secret",
                                 llm={"flavor": "openai", "model": "grok-code-fast-1"}))
    log_text = log.read_text()
    assert "BASE:http://127.0.0.1:8890/v1" in log_text
    assert "KEY:brt_secret" in log_text                # token via env, never argv
    assert "brt_secret" not in log_text.split("BASE:")[0]
    assert "-m grok-code-fast-1" in log_text


async def test_gateway_needs_openai_flavor(tmp_path):
    with pytest.raises(ValueError, match="openai-flavor"):
        await GrokExecutor().start(spec(tmp_path, gateway_url="http://gw",
                                        run_token="x",
                                        llm={"flavor": "anthropic", "model": "m"}))
