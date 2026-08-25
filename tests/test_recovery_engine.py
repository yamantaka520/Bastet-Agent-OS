"""Regression coverage for the 2026-08-25 live timeout/recovery incident."""

from bastet_agent_os.executors.base import ProgressDeadline
from bastet_agent_os.workflow import parse_stages, rework_target_for
from bastet_agent_os.workflow_presets import PRESETS


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def test_real_progress_renews_soft_timeout_but_hard_ceiling_remains():
    clock = Clock()
    deadline = ProgressDeadline(3600, clock=clock)
    clock.value = 3590
    deadline.note_progress()
    assert deadline.remaining() == 1800

    clock.value = 7190
    deadline.note_progress()
    assert deadline.remaining() == 10
    clock.value = 7200
    assert deadline.remaining() == 0


def test_silence_does_not_renew_the_timeout():
    clock = Clock()
    deadline = ProgressDeadline(3600, clock=clock)
    clock.value = 3600
    assert deadline.remaining() == 0


def test_web_review_rework_is_pinned_to_implementation():
    preset = next(p for p in PRESETS if p["id"] == "web-dev")
    stages = parse_stages(preset["stages"])
    review = next(i for i, stage in enumerate(stages)
                  if stage.name == "響應式與無障礙檢查")
    implement = next(i for i, stage in enumerate(stages)
                     if stage.name == "頁面實作")
    for attempt in range(6):
        assert rework_target_for(stages, review, attempt) == implement
    assert stages[implement].timeout_s == 7200
