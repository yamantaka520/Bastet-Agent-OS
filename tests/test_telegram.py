"""Telegram channel: pairing, allowlist, inline approval, notifications."""

import json

import httpx
import pytest
from fake_executor import SCRIPT, add_template, req

from bastet_agent_os.channels.telegram import TelegramChannel, issue_pairing_code
from bastet_agent_os.events import EventBus
from bastet_agent_os.executors.base import RunResult


class FakeTelegram:
    """Captures outbound Bot API calls."""

    def __init__(self):
        self.sent: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            self.sent.append({"method": request.url.path.rsplit("/", 1)[-1], **body})
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.MockTransport(handler)

    def texts(self) -> list[str]:
        return [m.get("text", "") for m in self.sent if m["method"] == "sendMessage"]


@pytest.fixture
def channel(orch, seeded):
    seeded.write("INSERT INTO channels(id, kind, config_json, secret_ref, enabled) "
                 "VALUES('chn1','telegram','{}','env:X',1)")
    fake = FakeTelegram()
    ch = TelegramChannel(seeded, orch, EventBus(), "chn1", "TOKEN",
                         transport=fake.transport())
    return ch, fake, seeded


def message(text, telegram_id=111, chat_type="private", chat_id=555):
    return {"message": {"chat": {"type": chat_type, "id": chat_id},
                        "from": {"id": telegram_id}, "text": text}}


async def pair(ch, db, telegram_id=111):
    code = issue_pairing_code(db, "usr_x", "manfred")
    await ch.handle_update(message(f"/pair {code}", telegram_id=telegram_id))


async def test_pairing_binds_numeric_id(channel):
    ch, fake, db = channel
    await pair(ch, db)
    config = json.loads(db.one("SELECT config_json FROM channels WHERE id='chn1'")
                        ["config_json"])
    assert config["bindings"]["111"]["name"] == "manfred"
    assert any("Paired as manfred" in t for t in fake.texts())


async def test_pairing_code_is_one_time(channel):
    ch, fake, db = channel
    code = issue_pairing_code(db, "usr_x", "manfred")
    await ch.handle_update(message(f"/pair {code}"))
    await ch.handle_update(message(f"/pair {code}", telegram_id=222))
    assert any("Invalid pairing code" in t for t in fake.texts())
    config = json.loads(db.one("SELECT config_json FROM channels WHERE id='chn1'")
                        ["config_json"])
    assert "222" not in config.get("bindings", {})


async def test_unpaired_user_gets_nothing_but_pairing_hint(channel):
    ch, fake, db = channel
    await ch.handle_update(message("/status"))
    assert any("Not paired" in t for t in fake.texts())
    assert not any("Σ cost" in t for t in fake.texts())


async def test_group_messages_are_ignored(channel):
    ch, fake, db = channel
    await pair(ch, db)
    fake.sent.clear()
    await ch.handle_update(message("/status", chat_type="group"))
    assert fake.sent == []  # groups never carry commands


async def test_status_and_jobs_for_paired_user(channel):
    ch, fake, db = channel
    await pair(ch, db)
    await ch.handle_update(message("/status"))
    await ch.handle_update(message("/jobs"))
    assert any("Σ cost" in t for t in fake.texts())
    assert any("job1" in t for t in fake.texts())


async def test_approve_flow_end_to_end(channel, orch, seeded):
    ch, fake, db = channel
    await pair(ch, db)

    add_template(seeded, "gated", [{"name": "plan", "gate": "human-approve"}])
    SCRIPT.append(RunResult(status="succeeded", summary="the plan"))
    job_id = orch.dispatch(req(template_id="gated"))
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "blocked"

    # /approve shows the card with inline buttons referencing the job id
    await ch.handle_update(message(f"/approve {job_id}"))
    card = [m for m in fake.sent if m["method"] == "sendMessage" and m.get("reply_markup")][-1]
    buttons = card["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == f"apv:{job_id}:yes"

    # pressing Approve resolves the gate, attributed to the bound user
    await ch.handle_update({"callback_query": {
        "id": "cq1", "from": {"id": 111}, "data": f"apv:{job_id}:yes",
        "message": {"chat": {"id": 555}}}})
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
    gate = seeded.one("SELECT * FROM gate_results WHERE reviewer_kind='user'")
    assert gate["reviewer_id"] == "manfred"


async def test_callback_from_unpaired_user_is_ignored(channel, orch, seeded):
    ch, fake, db = channel
    add_template(seeded, "gated", [{"name": "plan", "gate": "human-approve"}])
    SCRIPT.append(RunResult(status="succeeded"))
    job_id = orch.dispatch(req(template_id="gated"))
    await orch.wait_idle()

    await ch.handle_update({"callback_query": {
        "id": "cq1", "from": {"id": 999}, "data": f"apv:{job_id}:yes",
        "message": {"chat": {"id": 555}}}})
    await orch.wait_idle()
    assert seeded.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "blocked"


async def test_gate_pending_notification_has_buttons(channel):
    ch, fake, db = channel
    await pair(ch, db)
    fake.sent.clear()
    await ch._notify({"type": "gate.pending", "job_id": "job_z", "stage": "plan"})
    sent = fake.sent[-1]
    assert "approval needed" in sent["text"]
    assert sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "apv:job_z:yes"
