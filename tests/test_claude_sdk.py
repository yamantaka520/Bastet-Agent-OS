"""claude-sdk executor plumbing, driven by a stubbed Agent SDK module."""

import asyncio
import sys
import types
from dataclasses import dataclass, field

import pytest

from bastet_agent_os.executors.base import TaskSpec
from bastet_agent_os.executors.claude_sdk import ClaudeSdkExecutor


@dataclass
class StubText:
    text: str


@dataclass
class StubToolUse:
    name: str
    text: str | None = None


@dataclass
class StubAssistantMessage:
    content: list


@dataclass
class StubResultMessage:
    result: str = "done"
    is_error: bool = False
    total_cost_usd: float = 0.05
    usage: dict = field(default_factory=lambda: {"input_tokens": 10, "output_tokens": 4})


StubAssistantMessage.__name__ = "AssistantMessage"
StubResultMessage.__name__ = "ResultMessage"


@dataclass
class AllowResult:
    updated_input: dict


@dataclass
class DenyResult:
    message: str


@pytest.fixture
def stub_sdk(monkeypatch):
    """Fake claude_agent_sdk: emits text, asks permission for Bash, finishes."""
    state = {"permission_result": None}

    sdk = types.ModuleType("claude_agent_sdk")
    sdk_types = types.ModuleType("claude_agent_sdk.types")
    sdk_types.PermissionResultAllow = AllowResult
    sdk_types.PermissionResultDeny = DenyResult

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    async def query(prompt, options):
        yield StubAssistantMessage(content=[StubText("thinking about it")])
        state["permission_result"] = await options.can_use_tool(
            "Bash", {"command": "rm -rf build"}, None)
        yield StubResultMessage()

    sdk.ClaudeAgentOptions = ClaudeAgentOptions
    sdk.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", sdk_types)
    return state


def spec(tmp_path) -> TaskSpec:
    return TaskSpec(run_id="r1", prompt="do it", workdir=str(tmp_path))


async def test_permission_pauses_then_allow_resumes(stub_sdk, tmp_path):
    executor = ClaudeSdkExecutor()
    handle = await executor.start(spec(tmp_path))
    events = []

    async def consume():
        async for event in executor.stream(handle):
            events.append(event)
            if event.type == "interaction_request":
                await executor.respond(handle, event.data["request_id"],
                                       {"behavior": "allow"})

    await asyncio.wait_for(consume(), timeout=5)
    result = await executor.result(handle)
    assert result.status == "succeeded"
    assert result.cost_usd == 0.05 and result.tokens_in == 10
    kinds = [e.type for e in events]
    assert "interaction_request" in kinds and "progress" in kinds
    assert isinstance(stub_sdk["permission_result"], AllowResult)


async def test_permission_deny(stub_sdk, tmp_path):
    executor = ClaudeSdkExecutor()
    handle = await executor.start(spec(tmp_path))

    async def consume():
        async for event in executor.stream(handle):
            if event.type == "interaction_request":
                await executor.respond(handle, event.data["request_id"],
                                       {"behavior": "deny", "message": "nope"})

    await asyncio.wait_for(consume(), timeout=5)
    await executor.result(handle)
    assert isinstance(stub_sdk["permission_result"], DenyResult)
    assert stub_sdk["permission_result"].message == "nope"


async def test_respond_unknown_request_raises(stub_sdk, tmp_path):
    executor = ClaudeSdkExecutor()
    handle = await executor.start(spec(tmp_path))
    with pytest.raises(ValueError):
        await executor.respond(handle, "perm_nope", {"behavior": "allow"})
    await executor.cancel(handle)


async def test_missing_sdk_gives_helpful_error(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    executor = ClaudeSdkExecutor()
    with pytest.raises(ValueError, match="pip install claude-agent-sdk"):
        await executor.start(spec(tmp_path))
