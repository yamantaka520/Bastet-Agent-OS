"""agy (Antigravity) executor, driven by a fake `agy` binary."""

import json
import os
import stat

import pytest

from bastet_agent_os.executors.agy import AgyExecutor
from bastet_agent_os.executors.base import TaskSpec

FAKE_AGY = """#!/bin/sh
{ printf 'ARGS:'; printf ' %s' "$@"; printf '\\n'; printf 'CWD:%s\\n' "$(pwd)"; } > "$FAKE_LOG"
cat "$FAKE_ENVELOPE"
exit ${FAKE_EXIT:-0}
"""


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "agy"
    script.write_text(FAKE_AGY)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "agy.log"))
    envelope = tmp_path / "envelope.json"
    monkeypatch.setenv("FAKE_ENVELOPE", str(envelope))

    def set_envelope(obj):
        envelope.write_text(json.dumps(obj))

    return set_envelope, tmp_path / "agy.log"


def spec(tmp_path, **kw) -> TaskSpec:
    return TaskSpec(**{**dict(run_id="r1", prompt="do it", workdir=str(tmp_path)), **kw})


async def drive(task):
    executor = AgyExecutor()
    handle = await executor.start(task)
    async for _ in executor.stream(handle):
        pass
    return await executor.result(handle)


async def test_success_with_usage(fake_agy, tmp_path):
    set_envelope, log = fake_agy
    set_envelope({"conversation_id": "c1", "status": "SUCCESS", "response": "did it",
                  "num_turns": 3,
                  "usage": {"input_tokens": 100, "output_tokens": 20,
                            "thinking_tokens": 15, "cache_read_tokens": 60,
                            "total_tokens": 195}})
    result = await drive(spec(tmp_path))
    assert result.status == "succeeded" and result.summary == "did it"
    assert (result.tokens_in, result.tokens_out, result.cache_read) == (100, 35, 60)
    args = log.read_text()
    assert "--dangerously-skip-permissions" in args
    # stream-json, not json: one-shot mode prints nothing until the process
    # exits, which left long stages looking dead on the board for their whole
    # life (see tests/test_liveness.py)
    assert "--output-format stream-json" in args
    assert f"CWD:{tmp_path}" in args or "CWD:/private" in args  # workdir = process cwd


async def test_error_envelope_fails_even_with_rc0(fake_agy, tmp_path):
    set_envelope, _ = fake_agy
    set_envelope({"status": "ERROR", "response": "", "error": "not in trustedWorkspaces",
                  "usage": {}})
    result = await drive(spec(tmp_path))
    assert result.status == "failed" and "trustedWorkspaces" in result.summary


async def test_review_soft_denied_tools_and_schema_verdict(fake_agy, tmp_path):
    set_envelope, log = fake_agy
    set_envelope({"status": "SUCCESS",
                  "response": json.dumps({"verdict": "reject", "reasons": ["risky"]}),
                  "usage": {"input_tokens": 5, "output_tokens": 2}})
    result = await drive(spec(tmp_path, read_only=True, expect_verdict=True))
    assert result.structured_verdict == {"verdict": "reject", "reasons": ["risky"]}
    args = log.read_text()
    assert "--json-schema" in args
    assert "--dangerously-skip-permissions" not in args  # tools stay soft-denied


async def test_gateway_path_is_refused(tmp_path):
    with pytest.raises(ValueError, match="gateway"):
        await AgyExecutor().start(spec(tmp_path, gateway_url="http://gw",
                                       run_token="brt_x"))
