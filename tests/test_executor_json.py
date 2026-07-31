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
