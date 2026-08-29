"""Execution capabilities are contracts, not optimistic prompt text."""

import asyncio
import subprocess
import threading
import time

import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os import execution_capabilities as caps
from bastet_agent_os import orchestrator as orchestrator_mod
from bastet_agent_os.executors.base import RunResult
from bastet_agent_os.workflow import GateOutcome, evaluate_gate, parse_stages


def test_stage_capabilities_round_trip_and_validate():
    stage = parse_stages([{
        "name": "e2e", "gate": "tests-pass",
        "gate_config": {"command": "npm run test:e2e"},
        "requires": ["browser.playwright", "browser.playwright"],
    }])[0]
    assert stage.requires == ["browser.playwright"]
    assert stage.to_dict()["requires"] == ["browser.playwright"]
    with pytest.raises(ValueError, match="requires"):
        parse_stages([{"name": "bad", "requires": "browser.playwright"}])


def test_browser_crash_is_classified_as_capability_not_acceptance():
    text = "[ERROR] Crashpad setsockopt: Operation not permitted; chromium SIGTRAP"
    assert caps.classify_failure(text) == \
        "capability_unavailable:browser.playwright"
    assert caps.classify_failure("AssertionError: expected 30 fps, got 29") == ""


def test_missing_model_api_key_is_non_retryable_executor_configuration():
    assert caps.classify_failure(
        "No API key found for the selected model. Use /login to log into a provider"
    ) == "executor_unconfigured:llm_credentials"


def test_trusted_gate_marks_browser_launch_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 133, "", "Chrome Crashpad setsockopt: Operation not permitted SIGTRAP"))
    stage = parse_stages([{"name": "e2e", "gate": "tests-pass",
                           "gate_config": {"command": "npm run test:e2e"}}])[0]
    outcome = evaluate_gate(stage, str(tmp_path), None)
    assert outcome.verdict == "failed"
    assert outcome.failure_kind == "capability_unavailable:browser.playwright"


@pytest.mark.asyncio
async def test_missing_preflight_blocks_before_agent_without_spending_rework(
        orch, seeded, monkeypatch):
    add_template(seeded, "browser", [{"name": "e2e", "gate": "auto",
                                      "max_retries": 2,
                                      "requires": ["browser.playwright"]}])
    monkeypatch.setattr(caps, "probe_required", lambda required: [
        caps.CapabilityStatus("browser.playwright", False, "bastet-host",
                              "Chromium SIGTRAP")])

    job_id = orch.dispatch(req(template_id="browser"))
    await orch.wait_idle()

    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert (job["status"], job["rework_count"]) == ("blocked", 0)
    assert seeded.one("SELECT COUNT(*) n FROM runs WHERE job_id=?", (job_id,))["n"] == 1
    assert not SCRIPT, "the Agent must not run when its stage contract is unsatisfied"
    assert seeded.one("SELECT id FROM audit_log WHERE action='capability.unavailable' "
                      "AND target_id=?", (job_id,))
    room = seeded.one("SELECT content FROM room_messages ORDER BY rowid DESC LIMIT 1")
    assert room and "停止原路重跑" in room["content"]


@pytest.mark.asyncio
async def test_host_precheck_evidence_is_injected_into_reviewer_prompt(
        orch, seeded, monkeypatch):
    add_template(seeded, "review", [{
        "name": "review", "gate": "agent-review", "read_only": True,
        "requires": ["browser.playwright"],
        "gate_config": {"precheck_command": "printf trusted-e2e-ok"},
    }])
    monkeypatch.setattr(caps, "probe_required", lambda required: [
        caps.CapabilityStatus("browser.playwright", True, "bastet-host", "ok")])
    captured = {}

    def review(task):
        captured["prompt"] = task.prompt
        return RunResult(status="succeeded", summary="reviewed",
                         structured_verdict={"verdict": "approve", "reasons": [],
                                             "comments": []})
    SCRIPT.append(review)

    job_id = orch.dispatch(req(template_id="review"))
    await orch.wait_idle()

    assert "trusted-e2e-ok" in captured["prompt"]
    assert "Bastet 主機 precheck 證據" in captured["prompt"]
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"


@pytest.mark.asyncio
async def test_host_precheck_never_blocks_the_control_plane_loop(
        orch, seeded, monkeypatch):
    add_template(seeded, "slow-precheck", [{
        "name": "review", "gate": "agent-review", "read_only": True,
        "gate_config": {"precheck_command": "slow-e2e"},
    }])
    entered = threading.Event()

    def slow_gate(*_args, **_kwargs):
        entered.set()
        time.sleep(0.3)
        return GateOutcome("passed", "trusted")

    monkeypatch.setattr(orchestrator_mod, "evaluate_gate", slow_gate)
    SCRIPT.append(lambda _task: RunResult(
        status="succeeded", summary="reviewed",
        structured_verdict={"verdict": "approve", "reasons": [], "comments": []}))

    job_id = orch.dispatch(req(template_id="slow-precheck"))
    while not entered.is_set():
        await asyncio.sleep(0.005)
    started = asyncio.get_running_loop().time()
    await asyncio.sleep(0.02)
    assert asyncio.get_running_loop().time() - started < 0.1
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"


@pytest.mark.asyncio
async def test_agent_browser_sandbox_failure_does_not_use_stage_retries(
        orch, seeded):
    add_template(seeded, "browser", [{"name": "implement", "gate": "auto",
                                      "max_retries": 2}])
    SCRIPT.append(RunResult(
        status="failed",
        summary="Chrome Crashpad setsockopt Operation not permitted; SIGTRAP"))

    job_id = orch.dispatch(req(template_id="browser"))
    await orch.wait_idle()

    assert seeded.one("SELECT COUNT(*) n FROM runs WHERE job_id=?", (job_id,))["n"] == 1
    job = seeded.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert (job["status"], job["rework_count"]) == ("blocked", 0)


def test_web_preset_declares_trusted_browser_requirement():
    from bastet_agent_os.workflow_presets import PRESETS
    preset = next(item for item in PRESETS if item["id"] == "web-dev")
    e2e = next(item for item in preset["stages"] if item["name"] == "E2E 測試")
    assert e2e["requires"] == ["browser.playwright"]
