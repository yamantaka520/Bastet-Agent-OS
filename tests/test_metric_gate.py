"""Metric gates turn structured measurements into deterministic evidence."""

import pytest

from bastet_agent_os.workflow import StageDef, evaluate_gate, parse_stages


def test_metric_threshold_passes_and_records_the_exact_receipt(tmp_path):
    stage = StageDef(name="performance", gate="metric-threshold", gate_config={
        "command": "printf '%s\\n' '{\"metric\":\"fps\",\"value\":58.5,\"unit\":\"fps\"}'",
        "operator": ">=", "threshold": 55,
    })
    outcome = evaluate_gate(stage, str(tmp_path), None)
    assert outcome.verdict == "passed"
    assert '"value": 58.5' in outcome.detail
    assert '"threshold": 55' in outcome.detail


def test_metric_threshold_fails_honestly(tmp_path):
    stage = StageDef(name="bundle", gate="metric-threshold", gate_config={
        "command": "printf '%s\\n' '{\"value\":401}'",
        "metric": "bundle_kb", "operator": "<=", "threshold": 350,
    })
    assert evaluate_gate(stage, str(tmp_path), None).verdict == "failed"


@pytest.mark.parametrize("config", [
    {"operator": ">=", "threshold": 1},
    {"command": "true", "operator": "!=", "threshold": 1},
    {"command": "true", "operator": ">=", "threshold": "one"},
])
def test_metric_threshold_configuration_is_strict(config):
    with pytest.raises(ValueError):
        parse_stages([{"name": "metric", "gate": "metric-threshold",
                       "gate_config": config}])


def test_metric_threshold_rejects_unstructured_output(tmp_path):
    stage = StageDef(name="metric", gate="metric-threshold", gate_config={
        "command": "echo 55", "operator": ">=", "threshold": 50,
    })
    outcome = evaluate_gate(stage, str(tmp_path), None)
    assert outcome.verdict == "failed"
    assert outcome.config_error
