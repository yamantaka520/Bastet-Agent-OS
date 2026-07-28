"""In-run interaction plumbing: orchestrator pause/respond + Telegram buttons."""

import asyncio
import json
from dataclasses import dataclass

import pytest
from fake_executor import req
from test_telegram import FakeTelegram, message

from bastet_agent_os.channels.telegram import TelegramChannel, issue_pairing_code
from bastet_agent_os.events import EventBus
from bastet_agent_os.executors.base import RunEvent, RunResult, TaskSpec, register_builtin


@dataclass
class InteractiveHandle:
    task: TaskSpec
    future: asyncio.Future = None
    reply: dict | None = None

    def state(self):
        return {"interactive": True}


@register_builtin
class InteractiveFakeExecutor:
    """Asks one permission question, then succeeds/fails on the answer."""

    kind = "interactive-fake"
    capabilities = {"code", "interactive"}

    async def start(self, task: TaskSpec) -> InteractiveHandle:
        handle = InteractiveHandle(task=task)
        handle.future = asyncio.get_running_loop().create_future()
        return handle

    async def stream(self, handle):
        yield RunEvent("interaction_request", {
            "request_id": "q1", "kind": "permission_request",
            "payload": {"tool": "Bash", "input": "rm -rf build"}})
        handle.reply = await asyncio.wait_for(handle.future, timeout=5)

    async def respond(self, handle, request_id, reply):
        assert request_id == "q1"
        handle.future.set_result(reply)

    async def cancel(self, handle):
        pass

    async def result(self, handle) -> RunResult:
        allowed = handle.reply and handle.reply.get("behavior") == "allow"
        return RunResult(status="succeeded" if allowed else "failed",
                         summary="ran" if allowed else "denied")


@pytest.fixture
def iorch(orch, seeded):
    seeded.write("INSERT INTO agents(id, amos_agent_id, name, executor_type, created_at, "
                 "updated_at) VALUES('ibot','ibot','I','interactive-fake',datetime('now'),"
                 "datetime('now'))")
    return orch


async def test_interaction_pauses_and_respond_resumes(iorch, seeded):
    job_id = iorch.dispatch(req(agent_id="ibot"))
    for _ in range(50):  # wait until the run parks in waiting_input
        await asyncio.sleep(0.01)
        run = seeded.one("SELECT * FROM runs WHERE job_id=?", (job_id,))
        if run and run["status"] == "waiting_input":
            break
    assert run["status"] == "waiting_input"
    pending = seeded.one("SELECT * FROM run_interactions WHERE run_id=?", (run["id"],))
    assert pending["status"] == "pending" and pending["kind"] == "permission_request"

    await iorch.respond(run["id"], "q1", {"behavior": "allow"}, user="manfred")
    await iorch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    answered = seeded.one("SELECT * FROM run_interactions WHERE run_id=?", (run["id"],))
    assert answered["status"] == "answered"
    assert json.loads(answered["reply_json"]) == {"behavior": "allow"}


async def test_respond_to_dead_run_raises(iorch):
    with pytest.raises(ValueError):
        await iorch.respond("run_ghost", "q1", {"behavior": "allow"})


async def test_telegram_interaction_buttons(iorch, seeded):
    seeded.write("INSERT INTO channels(id, kind, config_json, secret_ref, enabled) "
                 "VALUES('chn1','telegram','{}','env:X',1)")
    fake = FakeTelegram()
    channel = TelegramChannel(seeded, iorch, EventBus(), "chn1", "TOKEN",
                              transport=fake.transport())
    code = issue_pairing_code(seeded, "usr_x", "manfred")
    await channel.handle_update(message(f"/pair {code}"))

    # a waiting_input event renders Allow/Deny buttons
    await channel._notify({"type": "run.waiting_input", "run_id": "run_z",
                           "request_id": "q9", "kind": "permission_request",
                           "summary": "Bash: rm -rf build"})
    card = fake.sent[-1]
    assert card["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "itx:run_z:q9:yes"

    # pressing Deny on a live run resolves the interaction through the orchestrator
    job_id = iorch.dispatch(req(agent_id="ibot"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        run = seeded.one("SELECT * FROM runs WHERE job_id=?", (job_id,))
        if run and run["status"] == "waiting_input":
            break
    await channel.handle_update({"callback_query": {
        "id": "cq", "from": {"id": 111}, "data": f"itx:{run['id']}:q1:no",
        "message": {"chat": {"id": 555}}}})
    await iorch.wait_idle()
    run = seeded.one("SELECT * FROM runs WHERE id=?", (run["id"],))
    assert run["status"] == "failed"  # denied => the fake executor fails the run