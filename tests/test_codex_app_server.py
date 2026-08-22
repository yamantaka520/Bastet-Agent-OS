"""Codex app-server stdio JSON-RPC lifecycle and interaction contract."""

import json
import os
import stat

import pytest

from bastet_agent_os.executors.base import TaskSpec
from bastet_agent_os.executors.codex_app_server import CodexAppServerExecutor

FAKE_SERVER = r'''#!/usr/bin/env python3
import json, os, sys
log = open(os.environ["FAKE_APP_LOG"], "a", buffering=1)
for line in sys.stdin:
    message = json.loads(line)
    log.write(json.dumps(message) + "\n")
    method = message.get("method")
    if method == "initialize":
        print(json.dumps({"id": message["id"], "result": {"serverInfo": {}}}), flush=True)
    elif method == "thread/start":
        print(json.dumps({"id": message["id"], "result": {"thread": {"id": "th1"}}}), flush=True)
    elif method == "turn/start":
        print(json.dumps({"id": message["id"], "result": {"turn": {"id": "tu1"}}}), flush=True)
        print(json.dumps({"id": 41, "method": "item/commandExecution/requestApproval",
                          "params": {"command": "pytest"}}), flush=True)
    elif message.get("id") == 41 and "result" in message:
        print(json.dumps({"method": "item/agentMessage/delta", "params": {"delta": "all done"}}), flush=True)
        print(json.dumps({"method": "thread/tokenUsage/updated", "params": {
            "tokenUsage": {"inputTokens": 100, "cachedInputTokens": 60, "outputTokens": 20}}}), flush=True)
        print(json.dumps({"method": "turn/completed", "params": {
            "turn": {"id": "tu1", "status": "completed"}}}), flush=True)
'''


@pytest.fixture
def fake_app_server(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(FAKE_SERVER)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "app-server.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_APP_LOG", str(log))
    return log


async def test_app_server_thread_turn_approval_and_usage(fake_app_server, tmp_path):
    executor = CodexAppServerExecutor()
    task = TaskSpec(run_id="r1", prompt="do it", context_text="only relevant context",
                    workdir=str(tmp_path), timeout_s=10)
    handle = await executor.start(task)
    seen = []
    async for event in executor.stream(handle):
        seen.append(event)
        if event.type == "interaction_request":
            await executor.respond(handle, event.data["request_id"], {"approved": True})
    result = await executor.result(handle)

    assert result.status == "succeeded" and result.summary == "all done"
    assert (result.tokens_in, result.tokens_out, result.cache_read) == (40, 20, 60)
    assert any(event.type == "interaction_request" for event in seen)
    messages = [json.loads(line) for line in fake_app_server.read_text().splitlines()]
    assert [message.get("method") for message in messages[:4]] == [
        "initialize", "initialized", "thread/start", "turn/start"]
    turn = next(message["params"] for message in messages
                if message.get("method") == "turn/start")
    assert "only relevant context" in turn["input"][0]["text"]
    approval = next(message for message in messages if message.get("id") == 41)
    assert approval["result"]["decision"] == "accept"


async def test_app_server_rejects_unimplemented_gateway_path(tmp_path):
    executor = CodexAppServerExecutor()
    with pytest.raises(ValueError, match="gateway"):
        await executor.start(TaskSpec(run_id="r1", prompt="x", workdir=str(tmp_path),
                                      gateway_url="http://gateway"))

