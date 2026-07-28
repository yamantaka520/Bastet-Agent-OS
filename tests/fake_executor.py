"""Shared fake executor + helpers for workflow/governance tests."""

import json
from dataclasses import dataclass, field

from bastet_agent_os.executors.base import RunResult, TaskSpec, register_builtin
from bastet_agent_os.orchestrator import DispatchRequest

SCRIPT: list = []  # each item: RunResult or callable(TaskSpec) -> RunResult


@dataclass
class FakeHandle:
    task: TaskSpec
    result: RunResult | None = None
    events: list = field(default_factory=list)

    def state(self) -> dict:
        return {"fake": True}


@register_builtin
class FakeExecutor:
    kind = "fake"
    capabilities = {"code", "review"}

    async def start(self, task: TaskSpec) -> FakeHandle:
        item = SCRIPT.pop(0)
        result = item(task) if callable(item) else item
        return FakeHandle(task=task, result=result)

    async def stream(self, handle: FakeHandle):
        for event in handle.events:
            yield event

    async def respond(self, handle, request_id, reply):
        pass

    async def cancel(self, handle):
        pass

    async def result(self, handle: FakeHandle) -> RunResult:
        return handle.result


def add_template(db, name: str, stages: list[dict]) -> None:
    db.write("INSERT INTO workflow_templates(id, name, version, stages_json) VALUES(?,?,1,?)",
             (name, name, json.dumps(stages)))


def req(**kw) -> DispatchRequest:
    defaults = dict(project_id="proj1", prompt="do the thing", title="t",
                    agent_id="fakebot", use_worktree=False)
    return DispatchRequest(**{**defaults, **kw})
