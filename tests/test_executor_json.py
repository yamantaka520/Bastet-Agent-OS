"""Reading a CLI's JSON, in the shapes CLIs actually emit.

Both failures on the stuck live job came from one assumption: that every JSON
value arrives as one object per line. grok's `--output-format json` pretty-prints
across many lines, so the verdict was never found ("no structured verdict",
gate rejects) and a bare string line inside an array crashed the stream with
AttributeError.
"""

import json

import pytest

from bastet_agent_os.executors.base import last_json_object, parse_event
from bastet_agent_os.workflow import evaluate_gate, parse_stages

# exactly what grok printed on the validation host
GROK_PRETTY = """{
  "text": "{\\"verdict\\":\\"approve\\",\\"reasons\\":[\\"版型符合規格\\"]}",
  "stopReason": "EndTurn",
  "sessionId": "019fb603-88a8-7152-bb0c-356ac4583fdb",
  "usage": {
    "input_tokens": 2550,
    "output_tokens": 147
  }
}
"""


def test_parse_event_only_yields_objects():
    assert parse_event('{"type":"text","text":"hi"}') == {"type": "text", "text": "hi"}
    assert parse_event(b'{"type":"end"}') == {"type": "end"}
    # the line that crashed a real run: an array element in pretty-printed JSON
    assert parse_event('    "版型符合規格"') is None
    assert parse_event("[1, 2]") is None
    assert parse_event("42") is None
    assert parse_event("not json at all") is None
    assert parse_event("") is None
    assert parse_event(b"\xff\xfe invalid utf8") is None


def test_last_json_object_reads_pretty_printed_output():
    payload = last_json_object(GROK_PRETTY)
    assert payload is not None, "pretty-printed JSON must be found"
    assert payload["stopReason"] == "EndTurn"
    verdict = json.loads(payload["text"])
    assert verdict["verdict"] == "approve"        # the verdict we were missing


def test_last_json_object_handles_the_other_real_shapes():
    # line-delimited: the last object wins
    assert last_json_object('{"a":1}\n{"b":2}\n')["b"] == 2
    # wrapped in prose / fences
    assert last_json_object('sure!\n```json\n{"verdict":"reject"}\n```\ndone')["verdict"] \
        == "reject"
    # a bare array or string is not an object
    assert last_json_object('["a","b"]') is None
    assert last_json_object("") is None
    assert last_json_object("   ") is None


def test_a_rejected_review_quotes_what_the_reviewer_said(tmp_path):
    """'no structured verdict' with no evidence sent us hunting a logic bug when
    the reviewer was simply not signed in."""
    stage = parse_stages([{"name": "review", "gate": "agent-review"}])[0]
    out = evaluate_gate(stage, str(tmp_path), None,
                        reviewer_output='{"type":"error","message":"Not signed in."}')
    assert out.verdict == "failed"
    assert "Not signed in" in out.detail
    silent = evaluate_gate(stage, str(tmp_path), None)
    assert "沒有任何輸出" in silent.detail


@pytest.mark.parametrize("name", ["grok", "agy", "codex", "claude_code"])
def test_no_executor_parses_json_without_a_type_check(name):
    """The guard belongs in parse_event/last_json_object, not per executor."""
    from pathlib import Path
    source = Path(f"src/bastet_agent_os/executors/{name}.py").read_text()
    assert "json.loads(line)" not in source
    assert 'json.loads(raw.decode(errors="replace"))' not in source


def test_a_verdict_packaged_any_way_is_still_read():
    """Live finding: grok returned two verdict objects back to back, so a strict
    json.loads on the inner text failed with "Extra data" and the gate reported
    "no verdict" even though the reviewer had answered."""
    doubled = ('{"verdict":"reject","reasons":["not done yet"]}'
               '{"verdict":"reject","reasons":["still not done"]}')
    data = last_json_object(doubled)
    assert data is not None and data["verdict"] == "reject"
    assert data["reasons"] == ["still not done"]        # the last word wins

    fenced = '```json\n{"verdict":"approve","reasons":[]}\n```'
    assert last_json_object(fenced)["verdict"] == "approve"
    chatty = 'Looks fine to me.\n{"verdict":"approve"}\nHope that helps!'
    assert last_json_object(chatty)["verdict"] == "approve"


@pytest.mark.parametrize("name", ["grok", "codex", "agy"])
def test_verdict_extraction_uses_the_tolerant_parser(name):
    from pathlib import Path
    source = Path(f"src/bastet_agent_os/executors/{name}.py").read_text()
    verdict_block = source[source.index("verdict"):]
    assert "last_json_object(" in verdict_block


# ---- oversized output lines --------------------------------------------------

def test_the_stream_limit_is_large_enough_for_real_tool_output():
    """asyncio's StreamReader defaults to 64 KiB per line, and every CLI executor
    reads `stream-json` a line at a time. A single line carrying a file read or a
    test log overruns that, and the run dies with "Separator is found, but chunk
    is longer than limit" — which is exactly what killed a live CatsWalker stage
    after minutes of real work."""
    from bastet_agent_os.executors.base import STREAM_LIMIT

    assert STREAM_LIMIT >= 8 * 1024 * 1024


def test_every_cli_executor_passes_the_limit_to_its_subprocess():
    """One executor left on the default is one executor that still dies."""
    import inspect

    from bastet_agent_os.executors import agy, claude_code, codex, grok, hermes

    for module in (claude_code, codex, grok, agy, hermes):
        source = inspect.getsource(module)
        assert "create_subprocess_exec" in source, module.__name__
        assert "limit=STREAM_LIMIT" in source, (
            f"{module.__name__} still uses asyncio's default 64 KiB line limit")


def test_every_cli_executor_survives_a_line_over_the_limit():
    """Belt and braces: even above STREAM_LIMIT, losing one progress line must
    beat losing the run. asyncio discards the offending line and the stream keeps
    working, so the loop only has to not treat ValueError as fatal."""
    import inspect

    from bastet_agent_os.executors import agy, claude_code, codex, grok, hermes

    for module in (claude_code, codex, grok, agy, hermes):
        source = inspect.getsource(module)
        assert "except ValueError:" in source, (
            f"{module.__name__} lets an oversized line kill the run")


@pytest.mark.asyncio
async def test_asyncio_discards_the_bad_line_and_the_next_one_arrives():
    """The assumption the guard rests on, pinned: skipping is only safe because
    the reader clears the oversized data itself."""
    import asyncio

    reader = asyncio.StreamReader(limit=64)
    reader.feed_data(b"x" * 500 + b"\n" + b'{"type":"result","ok":true}' + b"\n")
    reader.feed_eof()

    with pytest.raises(ValueError):
        await reader.readline()

    assert await reader.readline() == b'{"type":"result","ok":true}\n'


def subprocess_kwargs(module) -> set[str]:
    """The keyword names actually passed to create_subprocess_exec.

    Parsed, not grepped: the mistake this guards against was a trailing comment
    swallowing the next kwarg, which leaves the text in the file and only the AST
    knows it is no longer an argument."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_subprocess_exec"):
            return {kw.arg for kw in node.keywords if kw.arg}
    raise AssertionError(f"{module.__name__} has no create_subprocess_exec call")


def test_every_cli_executor_runs_in_the_job_workdir_with_its_env():
    """A regression guard from a real slip: adding the stream limit with a
    trailing comment swallowed `cwd=task.workdir` (and `env=`) on the same line in
    three executors. agy and grok had tests that caught it; codex did not, so it
    would have shipped running every run in the *server's* directory with none of
    the injected credentials."""
    from bastet_agent_os.executors import agy, claude_code, codex, grok, hermes

    for module in (claude_code, codex, grok, agy, hermes):
        passed = subprocess_kwargs(module)
        assert "cwd" in passed, f"{module.__name__} does not run in the job workdir"
        assert "env" in passed, (
            f"{module.__name__} passes no env — injected credentials and executor "
            f"profiles would be missing")
        assert "limit" in passed, (
            f"{module.__name__} still uses asyncio's default 64 KiB line limit")
