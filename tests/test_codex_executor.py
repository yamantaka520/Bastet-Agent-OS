"""codex executor, driven by a fake `codex` binary emitting JSONL events."""

import json
import os
import stat

import pytest

from bastet_agent_os.executors.base import TaskSpec
from bastet_agent_os.executors.codex import CodexExecutor

FAKE_CODEX = """#!/bin/sh
{ printf 'ARGS:'; printf ' %s' "$@"; printf '\\n'; } > "$FAKE_LOG"
cat "$FAKE_EVENTS"
if [ -n "$FAKE_LAST_MESSAGE" ]; then
  # mimic -o: write the final message file at the path given after -o
  out=""
  prev=""
  for a in "$@"; do
    if [ "$prev" = "-o" ]; then out="$a"; fi
    prev="$a"
  done
  [ -n "$out" ] && printf '%s' "$FAKE_LAST_MESSAGE" > "$out"
fi
exit ${FAKE_EXIT:-0}
"""


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(FAKE_CODEX)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "codex.log"))
    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("FAKE_EVENTS", str(events))

    def set_events(items):
        events.write_text("\n".join(json.dumps(i) for i in items) + "\n")

    return set_events, tmp_path / "codex.log"


def spec(tmp_path, **kw) -> TaskSpec:
    return TaskSpec(**{**dict(run_id="r1", prompt="do it", workdir=str(tmp_path)), **kw})


async def drive(task):
    executor = CodexExecutor()
    handle = await executor.start(task)
    async for _ in executor.stream(handle):
        pass
    return await executor.result(handle)


async def test_success_with_usage(fake_codex, tmp_path):
    set_events, log = fake_codex
    set_events([
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "all done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60,
                                             "cache_write_input_tokens": 10,
                                             "output_tokens": 20,
                                             "reasoning_output_tokens": 5}},
    ])
    result = await drive(spec(tmp_path))
    assert result.status == "succeeded" and result.summary == "all done"
    assert (result.tokens_in, result.tokens_out) == (40, 25)  # cached split out
    assert (result.cache_read, result.cache_write) == (60, 10)

    args = log.read_text()
    assert "--sandbox workspace-write" in args and "--json" in args
    assert "--ephemeral" in args


async def test_turn_failed_marks_failure(fake_codex, tmp_path):
    set_events, _ = fake_codex
    set_events([
        {"type": "turn.failed", "error": {"message": "model exploded"}},
    ])
    result = await drive(spec(tmp_path))
    assert result.status == "failed" and "model exploded" in result.summary


async def test_review_uses_output_schema_verdict(fake_codex, tmp_path, monkeypatch):
    set_events, log = fake_codex
    set_events([
        {"type": "item.completed", "item": {"type": "agent_message",
                                            "text": '{"verdict":"reject"}'}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
    ])
    monkeypatch.setenv("FAKE_LAST_MESSAGE",
                       json.dumps({"verdict": "reject", "reasons": ["no tests"]}))
    result = await drive(spec(tmp_path, read_only=True, expect_verdict=True))
    assert result.structured_verdict == {"verdict": "reject", "reasons": ["no tests"]}
    args = log.read_text()
    assert "--sandbox read-only" in args and "--output-schema" in args


async def test_gateway_path_wires_responses_provider(fake_codex, tmp_path):
    set_events, log = fake_codex
    set_events([
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])
    result = await drive(spec(tmp_path, gateway_url="http://127.0.0.1:8890",
                              run_token="brt_secret",
                              llm={"flavor": "openai", "model": "gpt-5.1-codex"}))
    assert result.status == "succeeded"
    args = log.read_text()
    assert 'model_providers.bastet.base_url="http://127.0.0.1:8890/v1"' in args
    assert 'model_providers.bastet.wire_api="responses"' in args
    assert 'model_provider="bastet"' in args
    assert "brt_secret" not in args  # run token travels via env, never argv


async def test_gateway_path_needs_openai_flavor(tmp_path):
    with pytest.raises(ValueError, match="openai-flavor"):
        await CodexExecutor().start(spec(tmp_path, gateway_url="http://gw",
                                         run_token="brt_x",
                                         llm={"flavor": "anthropic", "model": "x"}))


def test_verdict_schema_satisfies_strict_structured_outputs():
    """Live failure: OpenAI rejected every codex review with
    `invalid_json_schema` — strict mode requires `required` to list EVERY key in
    `properties` at every object level. The card died before the model saw a
    token. This walks the whole schema so a future field addition cannot
    reintroduce it."""
    from bastet_agent_os.executors.codex import VERDICT_SCHEMA

    def check(node, path="$"):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                keys = sorted(node["properties"].keys())
                assert sorted(node.get("required", [])) == keys, (
                    f"{path}: strict mode needs required == all property keys; "
                    f"got {node.get('required')} vs {keys}")
                assert node.get("additionalProperties") is False, path
            for key, value in node.items():
                check(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                check(value, f"{path}[{i}]")

    check(VERDICT_SCHEMA)
