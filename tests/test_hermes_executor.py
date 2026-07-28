"""hermes executor, driven by a fake `hermes` binary on PATH."""

import os
import stat

import pytest

from bastet_agent_os.executors.base import TaskSpec
from bastet_agent_os.executors.hermes import HermesExecutor, write_profile

FAKE_HERMES = """#!/bin/sh
# record argv and env for assertions, then behave like `hermes -z`
{
  printf 'ARGS:'; printf ' %s' "$@"; printf '\\n'
  printf 'HERMES_HOME:%s\\n' "$HERMES_HOME"
  printf 'TOKEN:%s\\n' "$BASTET_RUN_TOKEN"
  printf 'CWD:%s\\n' "$(pwd)"
} > "$FAKE_LOG"
echo "final reply from hermes"
exit ${FAKE_EXIT:-0}
"""


@pytest.fixture
def fake_hermes(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "hermes"
    script.write_text(FAKE_HERMES)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "hermes.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    return log


def spec(tmp_path, **kw) -> TaskSpec:
    defaults = dict(run_id="r1", prompt="fix the bug", workdir=str(tmp_path),
                    gateway_url="http://127.0.0.1:8890", run_token="brt_secret",
                    llm={"flavor": "openai", "model": "qwen-max"})
    return TaskSpec(**{**defaults, **kw})


async def drive(executor, task):
    handle = await executor.start(task)
    async for _ in executor.stream(handle):
        pass
    return await executor.result(handle), handle


async def test_oneshot_success_and_wiring(fake_hermes, tmp_path):
    result, handle = await drive(HermesExecutor(), spec(tmp_path))
    assert result.status == "succeeded"
    assert "final reply from hermes" in result.summary

    log = fake_hermes.read_text()
    assert "-z fix the bug" in log
    assert "--provider bastet -m qwen-max" in log
    assert "TOKEN:brt_secret" in log                       # run token via env, not argv
    assert f"CWD:{tmp_path}" in log or "CWD:/private" in log

    config = (tmp_path / "._bastet" / "hermes-home" / "config.yaml").read_text()
    assert "base_url: http://127.0.0.1:8890/v1" in config
    assert "api_key_env: BASTET_RUN_TOKEN" in config
    assert "brt_secret" not in config                      # token never written to disk


async def test_nonzero_exit_is_failure(fake_hermes, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "2")
    result, _ = await drive(HermesExecutor(), spec(tmp_path))
    assert result.status == "failed"


async def test_read_only_refused(tmp_path):
    with pytest.raises(ValueError, match="read-only"):
        await HermesExecutor().start(spec(tmp_path, read_only=True))


async def test_requires_openai_flavor_gateway(tmp_path):
    with pytest.raises(ValueError, match="openai-flavor"):
        await HermesExecutor().start(
            spec(tmp_path, llm={"flavor": "anthropic", "model": "x"}))
    with pytest.raises(ValueError, match="gateway"):
        await HermesExecutor().start(spec(tmp_path, gateway_url=None, run_token=None))


def test_profile_writer(tmp_path):
    write_profile(tmp_path / "home", "http://gw:1", "m1")
    text = (tmp_path / "home" / "config.yaml").read_text()
    assert "api_mode: chat_completions" in text and "model: m1" in text
