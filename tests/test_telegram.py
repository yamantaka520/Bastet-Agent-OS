"""Telegram channel: pairing, allowlist, inline approval, notifications."""

import asyncio
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
    # the card names the work: a job id alone tells the approver nothing
    assert "需要你核准" in sent["text"] and "job_z" in sent["text"]
    assert sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "apv:job_z:yes"


async def test_review_package_sends_images_video_and_documents(channel, tmp_path):
    ch, _, _ = channel
    folder = tmp_path / "artifacts" / "job_z" / "preview"
    folder.mkdir(parents=True)
    (folder / "screen.png").write_bytes(b"png")
    (folder / "walk.mp4").write_bytes(b"video")
    (folder / "report.pdf").write_bytes(b"pdf")
    ch.home_root = str(tmp_path)
    calls = []

    class Capture:
        async def post(self, endpoint, **kwargs):
            calls.append((endpoint, kwargs))
            return httpx.Response(200, json={"ok": True},
                                  request=httpx.Request("POST", endpoint))

    ch._client = Capture()
    await ch._send_previews(555, "job_z", ["screen.png", "walk.mp4", "report.pdf"])

    assert [endpoint for endpoint, _ in calls] == [
        "/sendPhoto", "/sendVideo", "/sendDocument"]
    assert [next(iter(call[1]["files"])) for call in calls] == [
        "photo", "video", "document"]


async def test_one_failed_notification_does_not_kill_the_channel(channel):
    """The live failure: an unguarded await in the notify loop meant a single send
    error ended every future notification, while the channel still reported
    `polling` — so an approval request reached nobody and the job waited forever."""
    ch, fake, db = channel
    delivered: list[str] = []
    attempts = {"n": 0}

    async def flaky(chat_id, text, reply_markup=None, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("telegram 502")
        delivered.append(text)

    ch._send = flaky
    ch._save_config({"bindings": {"111": {"user_id": "u1", "name": "root",
                                          "chat_id": 555}}})
    task = asyncio.get_running_loop().create_task(ch._notify_loop())
    await asyncio.sleep(0.05)
    assert ch.notify_alive is True

    ch.bus.emit("job.done", project_id="proj1", job_id="job_a")   # this one fails
    await asyncio.sleep(0.05)
    ch.bus.emit("job.done", project_id="proj1", job_id="job_b")   # this must arrive
    await asyncio.sleep(0.05)

    assert ch.notify_errors == 1
    assert ch.notify_alive is True                       # still listening
    assert any("job_b" in text for text in delivered)
    task.cancel()


async def test_pending_approvals_are_re_announced_on_start(channel):
    """A notification lost to a restart left work waiting on an approval nobody
    knew about; saying what is outstanding makes that recoverable."""
    from bastet_agent_os.db import now as _now

    ch, fake, db = channel
    ch._save_config({"bindings": {"111": {"user_id": "u1", "name": "root",
                                          "chat_id": 555}}})
    db.write("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, stage, "
             "status, created_at, updated_at) VALUES('jw','proj1','[]','等核准的事',"
             "'上線核准','blocked',?,?)", (_now(), _now()))
    db.write("INSERT INTO runs(id, job_id, stage, agent_id, executor_type, status) "
             "VALUES('rw','jw','上線核准','ag1','fake','succeeded')")
    db.write("INSERT INTO gate_results(id, run_id, gate_type, verdict, reviewer_kind, "
             "reviewer_id, detail_md, at) VALUES('gw','rw','human-approve','pending',"
             "'agent','x','waiting',?)", (_now(),))

    await ch._announce_pending()
    texts = " ".join(fake.texts())
    assert "等你核准" in texts and "等核准的事" in texts


async def test_nothing_is_announced_when_nothing_waits(channel):
    ch, fake, db = channel
    ch._save_config({"bindings": {"111": {"user_id": "u1", "name": "root",
                                          "chat_id": 555}}})
    await ch._announce_pending()
    assert fake.texts() == []


async def test_rework_notification_says_what_failed_and_who_is_fixing_it(channel):
    """The complaint: a notification that a thing broke, with no way to tell
    what. A rework message has to carry the failing output and make clear that
    nobody needs to intervene."""
    ch, fake, db = channel
    ch._save_config({"bindings": {"1": {"user_id": "u1", "name": "m", "chat_id": 42}}})
    db.write("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, spec_md, "
             "stage, status, created_at, updated_at) VALUES('jobrw','proj1','[]',"
             "'貓咪散步預約','spec','實作','in_progress',datetime('now'),datetime('now'))")

    await ch._notify({
        "type": "job.rework", "job_id": "jobrw", "title": "貓咪散步預約",
        "failed_stage": "整合測試", "gate": "tests-pass", "back_to": "實作",
        "role": "backend-engineer", "cycle": 1, "max_cycles": 3,
        "config_error": False,
        "detail": "FAILED tests/test_booking.py::test_confirm - AssertionError: "
                  "expected 200 got 500",
    })

    text = fake.texts()[-1]
    assert "貓咪散步預約" in text                 # which card
    assert "proj1" in text                        # which project
    assert "整合測試" in text and "tests-pass" in text
    assert "實作" in text and "backend-engineer" in text   # who is fixing it
    assert "1/3" in text                          # how much rope is left
    assert "test_confirm" in text                 # the actual failure
    assert "不需要你做什麼" in text                # it is progress, not an alarm


async def test_blocked_notification_carries_the_output_and_a_retry_button(channel):
    ch, fake, db = channel
    ch._save_config({"bindings": {"1": {"user_id": "u1", "name": "m", "chat_id": 42}}})
    db.write("INSERT INTO jobs(id, project_id, stages_snapshot_json, title, spec_md, "
             "stage, status, created_at, updated_at) VALUES('job2','proj1','[]',"
             "'E2E 上線','spec','E2E 測試','blocked',datetime('now'),datetime('now'))")

    await ch._notify({
        "type": "job.blocked", "job_id": "job2", "title": "E2E 上線",
        "stage": "E2E 測試", "gate": "tests-pass", "cycles": 3,
        "config_error": True, "reason": "設定問題",
        "detail": 'npm ERR! Missing script: "test:e2e"',
    })

    sent = [m for m in fake.sent if m["method"] == "sendMessage"][-1]
    assert "E2E 上線" in sent["text"]
    assert "設定問題" in sent["text"]
    assert "已自動返工 3 次" in sent["text"]      # what was already tried
    assert 'Missing script: "test:e2e"' in sent["text"]
    buttons = sent["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "rty:job2"   # a way forward, in place


async def test_long_output_is_trimmed_to_what_telegram_accepts(channel):
    """A 400 from Telegram loses the whole notification, which is worse than a
    trimmed one."""
    ch, fake, db = channel
    ch._save_config({"bindings": {"1": {"user_id": "u1", "name": "m", "chat_id": 42}}})
    await ch._notify({"type": "job.blocked", "job_id": "nope", "title": "big",
                      "stage": "s", "detail": "x" * 50_000, "reason": "boom"})

    text = fake.texts()[-1]
    assert len(text) <= 3800
    assert "（前面省略）" in text                  # the tail is what was kept
    assert text.rstrip().endswith("x")


# ---- delivery accounting (the "did it actually reach Telegram?" question) --------

class FlakyTelegram(FakeTelegram):
    """Fails the first N posts the way this host's network actually does."""

    def __init__(self, fail_first: int):
        super().__init__()
        self.fail_first = fail_first
        self.attempts = 0

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.attempts += 1
            if self.attempts <= self.fail_first:
                raise httpx.ConnectTimeout("boom")
            body = json.loads(request.content) if request.content else {}
            self.sent.append({"method": request.url.path.rsplit("/", 1)[-1], **body})
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.MockTransport(handler)


def _flaky_channel(orch, seeded, fail_first):
    seeded.write("INSERT INTO channels(id, kind, config_json, secret_ref, enabled) "
                 "VALUES('chn2','telegram','{}','env:X',1)")
    fake = FlakyTelegram(fail_first)
    ch = TelegramChannel(seeded, orch, EventBus(), "chn2", "TOKEN",
                         transport=fake.transport())
    ch.SEND_BACKOFF_S = (0, 0)          # no real sleeps in tests
    return ch, fake


async def test_a_flaky_network_is_retried_and_the_delivery_is_on_record(orch, seeded):
    ch, fake = _flaky_channel(orch, seeded, fail_first=2)
    await ch._send(555, "approval evidence", purpose="gate.pending")
    assert fake.texts() == ["approval evidence"], "third attempt must succeed"
    audit = seeded.one("SELECT detail_json FROM audit_log WHERE action='notify.sent'")
    detail = json.loads(audit["detail_json"])
    assert detail["purpose"] == "gate.pending" and detail["attempt"] == 3


async def test_delivery_failure_is_a_fact_not_a_log_line(orch, seeded):
    ch, fake = _flaky_channel(orch, seeded, fail_first=99)
    await ch._send(555, "never arrives", purpose="gate.pending")
    audit = seeded.one("SELECT detail_json FROM audit_log WHERE action='notify.failed'")
    detail = json.loads(audit["detail_json"])
    assert detail["error"] == "ConnectTimeout"
    assert seeded.one("SELECT 1 AS x FROM audit_log WHERE action='notify.sent'") is None


async def test_approval_card_carries_the_checklist(channel):
    """The approver must see WHAT to check without opening the WebUI."""
    ch, fake, db = channel
    db.write("UPDATE jobs SET spec_md=? WHERE id='job1'",
             ("範圍：實作登入頁。\n驗收條件：\n1. 手機版按鈕可點\n2. 錯誤訊息可讀",))
    db.write("UPDATE jobs SET stages_snapshot_json=? WHERE id='job1'",
             (json.dumps([{"name": "work", "role": "pm", "gate": "human-approve",
                           "desc": "確認頁面結構與視覺方向"}]),))
    await ch._send_approval_card(555, "job1")
    text = fake.texts()[-1]
    assert "確認頁面結構與視覺方向" in text     # the stage's own description
    assert "驗收條件" in text and "手機版按鈕可點" in text
