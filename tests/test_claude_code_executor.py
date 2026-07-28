"""claude-code executor result mapping — including the is_error gotcha."""

import asyncio

from bastet_agent_os.executors.base import TaskSpec
from bastet_agent_os.executors.claude_code import ClaudeCodeExecutor, ClaudeCodeHandle


def _result_for(event: dict | None, **flags):
    handle = ClaudeCodeHandle(task=TaskSpec(run_id="r", prompt="p", workdir="."))
    handle.result_event = event
    for k, v in flags.items():
        setattr(handle, k, v)
    return asyncio.run(ClaudeCodeExecutor().result(handle))


def test_success_result():
    r = _result_for({"subtype": "success", "is_error": False, "result": "done",
                     "total_cost_usd": 0.12,
                     "usage": {"input_tokens": 5, "output_tokens": 9,
                               "cache_read_input_tokens": 100,
                               "cache_creation_input_tokens": 7}})
    assert r.status == "succeeded"
    assert (r.tokens_in, r.tokens_out, r.cache_read, r.cache_write) == (5, 9, 100, 7)
    assert r.cost_usd == 0.12 and r.precision == "reported"


def test_error_result_despite_success_subtype():
    # Claude Code emits subtype "success" even for errors (e.g. "Not logged in");
    # is_error is the authoritative flag — a false "succeeded" here is a real bug
    r = _result_for({"subtype": "success", "is_error": True,
                     "result": "Not logged in", "usage": {}})
    assert r.status == "failed"
    assert "Not logged in" in r.summary


def test_missing_result_event_is_failure():
    assert _result_for(None).status == "failed"


def test_timeout_and_cancel_mark_estimated():
    assert _result_for(None, timed_out=True).status == "timeout"
    r = _result_for(None, cancelled=True)
    assert r.status == "cancelled" and r.precision == "estimated"
