"""Telegram channel (SPEC §5.7): command & notification bridge.

Security posture (all mandated by the spec):
- identity = Telegram NUMERIC user id, never username (usernames can be
  renamed/squatted); ids are bound to Bastet users via one-time pairing codes
- group messages are ignored — commands come from private chats only
- sensitive actions (gate approval) go through inline-button confirmation
  referencing the concrete job id, and are attributed to the bound user
- long polling only: no public webhook endpoint (local-first)
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from ..db import Db, now

log = logging.getLogger("bastet.telegram")

POLL_TIMEOUT_S = 25
PAIR_CODE_TTL_MIN = 15

HELP_TEXT = (
    "🐈 Bastet Agent OS\n"
    "/status — jobs overview\n"
    "/jobs — recent jobs\n"
    "/approve <job_id> — decide a waiting gate\n"
    "/pair <code> — link this Telegram account\n"
    "any other message — talk to this channel's agent/LLM about the project"
)
NO_RESPONDER_TEXT = (
    "This channel has no chat responder yet. Pick an agent or a pool LLM (and a "
    "project) for it on the WebUI's 管理 tab, then send your message again."
)
MAX_TELEGRAM_TEXT = 3800     # 4096 hard limit; leave room for our own framing


def issue_pairing_code(db: Db, bastet_user_id: str, name: str) -> str:
    """One-time code the human sends to the bot as /pair <code>."""
    code = secrets.token_hex(4)
    expires = (datetime.now(UTC) + timedelta(minutes=PAIR_CODE_TTL_MIN)).isoformat(
        timespec="seconds")
    db.write("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
             (f"tg_pair:{code}", json.dumps({"user_id": bastet_user_id, "name": name,
                                             "expires": expires})))
    return code


class TelegramChannel:
    def __init__(self, db: Db, orchestrator, bus, channel_id: str, bot_token: str,
                 transport: httpx.AsyncBaseTransport | None = None,
                 home_root: str | None = None):
        self.db = db
        self.orch = orchestrator
        self.bus = bus
        self.channel_id = channel_id
        # chat attachments land under the Bastet home, next to the web ones
        self.home_root = home_root or str(getattr(orchestrator, "home", None)
                                          and orchestrator.home.root or ".")
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{bot_token}",
            timeout=httpx.Timeout(POLL_TIMEOUT_S + 10, connect=15),
            transport=transport,
        )
        self._stopping = asyncio.Event()
        self.notify_alive = False        # reported by /api/channels
        self.notify_errors = 0

    # -- config: the allowlist lives in channels.config_json -------------------

    def _config(self) -> dict:
        row = self.db.one("SELECT config_json FROM channels WHERE id=?", (self.channel_id,))
        return json.loads(row["config_json"] or "{}") if row else {}

    def _save_config(self, config: dict) -> None:
        self.db.write("UPDATE channels SET config_json=? WHERE id=?",
                      (json.dumps(config), self.channel_id))

    def _binding(self, telegram_id: int) -> dict | None:
        return self._config().get("bindings", {}).get(str(telegram_id))

    # -- main loops -------------------------------------------------------------

    async def run(self) -> None:
        notify_task = asyncio.create_task(self._notify_loop())
        try:
            await self._announce_pending()
            await self._poll_loop()
        finally:
            notify_task.cancel()
            await self._client.aclose()

    def stop(self) -> None:
        self._stopping.set()

    async def _announce_pending(self) -> None:
        """Tell people what is already blocked on them.

        A notification lost to a restart (or to the dead notify loop this fixes)
        left a job waiting forever on an approval nobody knew about. On start we
        say what is outstanding, so the gap is recoverable."""
        rows = self.db.query(
            "SELECT j.id, j.title, j.stage, j.project_id FROM jobs j "
            "WHERE j.status='blocked' AND j.archived=0 ORDER BY j.updated_at DESC "
            "LIMIT 10")
        waiting = [r for r in rows if self._awaits_human(r["id"])]
        if not waiting:
            return
        for binding in self._config().get("bindings", {}).values():
            await self._send(binding["chat_id"],
                             f"🔔 有 {len(waiting)} 個任務在等你核准：")
            for row in waiting:
                await self._send_approval_card(binding["chat_id"], row["id"])

    def _awaits_human(self, job_id: str) -> bool:
        row = self.db.one(
            "SELECT g.verdict FROM gate_results g JOIN runs r ON r.id = g.run_id "
            "WHERE r.job_id=? ORDER BY g.at DESC LIMIT 1", (job_id,))
        return bool(row and row["verdict"] == "pending")

    async def _poll_loop(self) -> None:
        offset = 0
        while not self._stopping.is_set():
            try:
                resp = await self._client.get("/getUpdates", params={
                    "offset": offset, "timeout": POLL_TIMEOUT_S,
                    "allowed_updates": '["message","callback_query"]'})
                updates = resp.json().get("result", [])
            except httpx.HTTPError as exc:
                log.warning("telegram poll error: %s", type(exc).__name__)
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                try:
                    await self.handle_update(update)
                except Exception:
                    log.exception("telegram update handling failed")

    async def _notify_loop(self) -> None:
        """One failed send used to end every future notification.

        `await self._notify(...)` unguarded meant a single HTTP error killed this
        task while the poll loop kept running, so the channel still reported
        `polling` and an approval request never reached anyone — the workflow then
        waited for a human who was never told."""
        queue = self.bus.subscribe()
        self.notify_alive = True
        try:
            while True:
                event = await queue.get()
                try:
                    await self._notify(event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.notify_errors += 1
                    log.exception("telegram notify failed for %s",
                                  event.get("type"))
        finally:
            self.notify_alive = False
            self.bus.unsubscribe(queue)

    # -- inbound ------------------------------------------------------------------

    async def handle_update(self, update: dict) -> None:
        if "message" in update:
            await self._handle_message(update["message"])
        elif "callback_query" in update:
            await self._handle_callback(update["callback_query"])

    async def _handle_message(self, message: dict) -> None:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = (message.get("text") or "").strip()
        if chat.get("type") != "private":
            return  # groups never carry commands (spec)
        telegram_id = int(sender.get("id", 0))

        if text.startswith("/pair"):
            await self._handle_pair(telegram_id, chat["id"], text)
            return

        binding = self._binding(telegram_id)
        if binding is None:
            await self._send(chat["id"], "Not paired. Run `bastet channel pair` on the "
                                         "host, then send /pair <code>.")
            return

        if text.startswith("/status"):
            await self._send(chat["id"], self._status_text())
        elif text.startswith("/jobs"):
            await self._send(chat["id"], self._jobs_text())
        elif text.startswith("/approve"):
            parts = text.split()
            if len(parts) != 2:
                await self._send(chat["id"], "usage: /approve <job_id>")
                return
            await self._send_approval_card(chat["id"], parts[1])
        elif text.startswith("/help") or text == "/start":
            await self._send(chat["id"], HELP_TEXT)
        else:
            await self._chat_turn(message, binding, chat["id"], telegram_id, text)

    # ---- chat: the second authorisation channel (SPEC §5.11) --------------------

    def _responder(self) -> tuple[str, str, str, str] | None:
        """(kind, id, scope_type, scope_id) configured for this channel."""
        config = self._config()
        responder = config.get("responder") or {}
        kind, rid = responder.get("kind"), responder.get("id")
        if not (kind and rid):
            return None
        project_id = config.get("project_id") or ""
        if project_id:
            return kind, rid, "project", project_id
        return kind, rid, "global", "*"

    async def _chat_turn(self, message: dict, binding: dict, chat_id: int,
                         telegram_id: int, text: str) -> None:
        from .. import chat as chat_mod

        target = self._responder()
        if target is None:
            await self._send(chat_id, NO_RESPONDER_TEXT)
            return
        kind, rid, scope_type, scope_id = target
        try:
            session_id = chat_mod.find_or_create_channel_session(
                self.db, channel="telegram", external_id=f"{self.channel_id}:{telegram_id}",
                scope_type=scope_type, scope_id=scope_id, responder_kind=kind,
                responder_id=rid, title=f"telegram · {binding.get('name', telegram_id)}",
                actor=f"user:{binding.get('user_id', '')}")
            attachments = await self._download_attachments(message, session_id)
            if not (text or attachments):
                return
            chat_mod.add_message(self.db, session_id, role="user", content=text,
                                 author=f"telegram:{telegram_id}",
                                 attachments=attachments)
            session = chat_mod.get_session(self.db, session_id)
            chat_mod.remember(self.db, session, "user", text)
            answer = await chat_mod.reply(self.db, self.home_root, session_id,
                                          actor=f"telegram:{telegram_id}")
        except Exception as exc:                      # never lose the user's message
            log.warning("telegram chat turn failed: %s", exc)
            await self._send(chat_id, f"⚠️ {type(exc).__name__}: {exc}"[:400])
            return
        self.bus.emit("chat.message", scope_id, session_id=session_id)
        await self._send(chat_id, (answer["content"] or "")[:MAX_TELEGRAM_TEXT])

    async def _download_attachments(self, message: dict, session_id: str) -> list[dict]:
        """Documents and photos the user sent — the chat's file intake."""
        from .. import chat as chat_mod

        files: list[dict] = []
        candidates = []
        if message.get("document"):
            candidates.append((message["document"].get("file_id"),
                               message["document"].get("file_name") or "document"))
        photos = message.get("photo") or []
        if photos:                                    # last entry = highest resolution
            candidates.append((photos[-1].get("file_id"), "photo.jpg"))
        for file_id, name in candidates:
            if not file_id:
                continue
            try:
                info = await self._client.get("/getFile", params={"file_id": file_id})
                path = ((info.json() or {}).get("result") or {}).get("file_path")
                if not path:
                    continue
                token = str(self._client.base_url).rsplit("/bot", 1)[-1]
                async with httpx.AsyncClient(timeout=60) as raw:
                    blob = await raw.get(
                        f"https://api.telegram.org/file/bot{token}/{path}")
                blob.raise_for_status()
                files.append(chat_mod.save_attachment(self.home_root, session_id,
                                                      name, blob.content))
            except Exception as exc:
                log.warning("telegram attachment download failed: %s", exc)
        return files

    async def _handle_pair(self, telegram_id: int, chat_id: int, text: str) -> None:
        parts = text.split()
        code = parts[1] if len(parts) == 2 else ""
        row = self.db.one("SELECT value FROM meta WHERE key=?", (f"tg_pair:{code}",))
        if row is None:
            await self._send(chat_id, "Invalid pairing code.")
            return
        payload = json.loads(row["value"])
        self.db.write("DELETE FROM meta WHERE key=?", (f"tg_pair:{code}",))  # one-time
        if payload["expires"] <= now():
            await self._send(chat_id, "Pairing code expired — generate a new one.")
            return
        config = self._config()
        config.setdefault("bindings", {})[str(telegram_id)] = {
            "user_id": payload["user_id"], "name": payload["name"], "chat_id": chat_id}
        self._save_config(config)
        self.db.audit(f"user:{payload['user_id']}", "channel.paired", "channel",
                      self.channel_id, {"telegram_id": telegram_id})
        self.bus.emit("channel.paired", None, channel_id=self.channel_id,
                      user=payload["name"])  # WS -> the admin page refreshes live
        await self._send(chat_id, f"✅ Paired as {payload['name']}. {HELP_TEXT}")

    def _review_checklist(self, job_id: str) -> str:
        """What the approver is being asked to check, in the message itself.

        An approval request that says only "stage X wants approval" forces the
        person to open the WebUI to learn what the acceptance criteria even
        were — on a phone, that means approvals happen blind. The card carries
        the spec's acceptance section (or its head) and the stage's own
        description, because that IS the checklist."""
        job = self.db.one("SELECT spec_md, stage, stages_snapshot_json FROM jobs "
                          "WHERE id=?", (job_id,))
        if job is None:
            return ""
        parts = []
        try:
            stages = json.loads(job["stages_snapshot_json"] or "[]")
            desc = next((s.get("desc") for s in stages
                         if s.get("name") == job["stage"]), "")
            if desc:
                parts.append(f"這一關要確認：{desc}")
        except (json.JSONDecodeError, TypeError):
            pass
        spec = job["spec_md"] or ""
        marker = spec.find("驗收")
        excerpt = spec[marker:marker + 700] if marker >= 0 else spec[:500]
        if excerpt.strip():
            parts.append(f"── 檢核項目 ──\n{excerpt.strip()}")
        return "\n".join(parts)

    async def _send_approval_card(self, chat_id: int, job_id: str) -> None:
        job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            await self._send(chat_id, f"unknown job {job_id}")
            return
        text = (f"⏸ {job['title']}\n{job_id} · stage {job['stage']} · {job['status']}\n\n"
                f"{self._review_checklist(job_id)}")
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"apv:{job_id}:yes"},
            {"text": "❌ Reject", "callback_data": f"apv:{job_id}:no"},
        ]]}
        await self._send(chat_id, text, reply_markup=keyboard,
                         purpose=f"approval-card:{job_id}")
        previews = self._preview_names(job_id)
        if previews:
            await self._send_previews(chat_id, job_id, previews)

    async def _handle_callback(self, callback: dict) -> None:
        telegram_id = int((callback.get("from") or {}).get("id", 0))
        binding = self._binding(telegram_id)
        data = callback.get("data") or ""
        chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
        await self._client.post("/answerCallbackQuery",
                                json={"callback_query_id": callback.get("id", "")})
        if binding is None:
            return
        try:
            if data.startswith("apv:"):
                _, job_id, decision = data.split(":", 2)
                approved = decision == "yes"
                outcome = self.orch.approve(job_id, approved,
                                            comment=f"via telegram by {binding['name']}",
                                            user=binding["name"])
                if chat_id:
                    verdict = "approved ✅" if approved else "rejected ❌"
                    await self._send(chat_id, f"{job_id} {verdict} → {outcome['status']}")
            elif data.startswith("rty:"):
                _, job_id = data.split(":", 1)
                outcome = self.orch.retry(job_id, user=binding["name"])
                if chat_id:
                    await self._send(chat_id,
                                     f"🔁 {job_id} 重跑「{outcome['stage']}」中…")
            elif data.startswith("itx:"):
                _, run_id, request_id, decision = data.split(":", 3)
                reply = {"behavior": "allow" if decision == "yes" else "deny"}
                await self.orch.respond(run_id, request_id, reply, user=binding["name"])
                if chat_id:
                    await self._send(chat_id,
                                     f"{run_id} → {reply['behavior']} ✔️")
        except ValueError as exc:
            if chat_id:
                await self._send(chat_id, f"⚠️ {exc}")

    # -- outbound -------------------------------------------------------------------

    async def _notify(self, event: dict) -> None:
        etype = event.get("type", "")
        keyboard = None
        if etype == "gate.pending":
            job = self.db.one("SELECT title, project_id FROM jobs WHERE id=?",
                              (event.get("job_id"),))
            previews = event.get("previews") or self._preview_names(event.get("job_id"))
            listing = (f"\n📎 核准附件 {len(previews)} 件（將逐一傳送，可直接檢視）"
                       if previews else "\n（這一關沒有附預覽 —— 判斷依據只有 diff）")
            checklist = self._review_checklist(event.get("job_id") or "")
            text = (f"⏸ 需要你核准：{job['title'] if job else event.get('job_id')}\n"
                    f"專案 {job['project_id'] if job else '?'} · "
                    f"階段 {event.get('stage')}\n{event.get('job_id')}{listing}"
                    + (f"\n\n{checklist}" if checklist else ""))
            keyboard = {"inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"apv:{event.get('job_id')}:yes"},
                {"text": "❌ Reject", "callback_data": f"apv:{event.get('job_id')}:no"},
            ]]}
            for binding in self._config().get("bindings", {}).values():
                await self._send_previews(binding["chat_id"],
                                          event.get("job_id"), previews)
        elif etype == "run.waiting_input":
            text = (f"✋ run {event.get('run_id')} asks: {event.get('kind')}\n"
                    f"{event.get('summary') or ''}")
            keyboard = {"inline_keyboard": [[
                {"text": "✅ Allow",
                 "callback_data": f"itx:{event.get('run_id')}:{event.get('request_id')}:yes"},
                {"text": "❌ Deny",
                 "callback_data": f"itx:{event.get('run_id')}:{event.get('request_id')}:no"},
            ]]}
        elif etype == "job.quota_wait":
            reset = (event.get("resume_at") or "")[11:16]
            text = (f"⏳ 額度用盡，會自己續跑 —— 不需要你做什麼\n"
                    f"{self._job_line(event)}\n"
                    f"階段：{event.get('stage')}\n"
                    f"供應商訊息：{(event.get('detail') or '')[:160]}\n"
                    f"預計 {reset} (UTC) 自動重試。等不及可以直接按重試。")
        elif etype == "agent.depleted":
            # the one failure class no automation can clear: it needs money
            text = (f"💳 Agent {event.get('agent_id')} 的付費額度用盡，已暫停對它派工\n"
                    f"{self._job_line(event)}\n"
                    f"供應商回應：{(event.get('detail') or '')[:200]}\n"
                    f"同角色的其他 agent 會自動接手，任務不會停在這裡。\n"
                    f"充值後請在「組織 → Agents」解除暫停（或直接對這張卡按重試並指定它）。")
        elif etype == "job.pm_intervention":
            deed = {"retry": "重跑該階段", "retry_other_agent": "換 agent 接手",
                    "supply_then_retry": "補充裁定後重跑",
                    "escalate": "研判需要人工，理由如下"}.get(
                        event.get("action") or "", event.get("action") or "?")
            text = (f"🤖 PM 監督介入（{event.get('pm')}，第 {event.get('cycle')}/"
                    f"{event.get('max_cycles')} 次）\n{self._job_line(event)}\n"
                    f"卡在：{event.get('stage')} → 決定：{deed}\n"
                    f"理由：{event.get('reason') or '(未說明)'}"
                    + ("\n\n卡片上有「PM 需要你的裁定」欄位：把答案寫進去按"
                       "「送出裁定並重試」，它會進到任務收件匣並讓卡片接著跑"
                       "（也可以直接按下面的重試）。"
                       if event.get("action") == "escalate"
                       else "\n不需要你做什麼 —— 處理後會自己往前跑。"))
            if event.get("action") == "escalate":
                # the human's lever on an escalated stall is RETRY (it also
                # unlatches the PM); a message that asks for a human with no
                # button sent someone hunting for an approve control that does
                # not exist
                keyboard = {"inline_keyboard": [[
                    {"text": "🔁 重試這一關",
                     "callback_data": f"rty:{event.get('job_id')}"},
                ]]}
        elif etype == "job.rework":
            text = self._rework_text(event)
        elif etype == "job.blocked":
            text = self._blocked_text(event)
            keyboard = {"inline_keyboard": [[
                {"text": "🔁 重試這一關",
                 "callback_data": f"rty:{event.get('job_id')}"},
            ]]}
        elif etype in ("job.done", "budget.exceeded", "budget.warning"):
            icon = {"job.done": "✅", "budget.exceeded": "🛑",
                    "budget.warning": "⚠️"}[etype]
            detail = event.get("reason") or event.get("stage") or ""
            text = f"{icon} {etype}: {event.get('job_id') or event.get('grant_id')} {detail}"
        else:
            return
        for binding in self._config().get("bindings", {}).values():
            await self._send(binding["chat_id"], text, reply_markup=keyboard,
                             purpose=etype)

    def _job_line(self, event: dict) -> str:
        """Which card, in which project — a job id alone means nothing to a
        person reading their phone."""
        job = self.db.one("SELECT title, project_id FROM jobs WHERE id=?",
                          (event.get("job_id"),))
        title = event.get("title") or (job["title"] if job else "") or "?"
        project = job["project_id"] if job else event.get("project_id") or "?"
        return f"{title}\n專案 {project} · {event.get('job_id')}"

    def _rework_text(self, event: dict) -> str:
        """Progress, not an alarm: the engine caught a failure and is handling
        it. Says what failed, who is fixing it, and how much rope is left."""
        who = f"「{event.get('back_to')}」"
        if event.get("role"):
            who += f"（{event.get('role')}）"
        head = ("🔧 關卡沒過，已自動退回修正"
                if not event.get("config_error")
                else "🔧 關卡指令跑不起來，已自動退回處理")
        return (f"{head}\n{self._job_line(event)}\n"
                f"沒過的關卡：{event.get('failed_stage')}"
                f"（{event.get('gate')}）\n"
                f"交回給：{who} · 第 {event.get('cycle')}/"
                f"{event.get('max_cycles')} 次返工\n"
                f"{self._detail_block(event.get('detail'))}\n"
                f"不需要你做什麼 —— 修完會自己往前跑。")

    def _blocked_text(self, event: dict) -> str:
        """The one notification that does need a human. It has to carry the
        evidence: what stage, what gate, how many attempts, and the actual
        output — reading the log on the host should not be the only way."""
        kind = "設定問題" if event.get("config_error") else "卡住了"
        spent = event.get("cycles") or 0
        tried = f"（已自動返工 {spent} 次仍未通過）" if spent else ""
        return (f"🟠 任務{kind}，需要你看一下{tried}\n"
                f"{self._job_line(event)}\n"
                f"停在：{event.get('stage')}"
                + (f"（{event.get('gate')}）" if event.get("gate") else "") + "\n"
                f"{self._detail_block(event.get('detail') or event.get('reason'))}")

    @staticmethod
    def _detail_block(detail: str | None) -> str:
        """The failure output, trimmed to what Telegram will actually deliver.

        Plain text, no markdown fences: these messages carry arbitrary compiler
        and test output, and one stray backtick or underscore in it would make
        Telegram reject the whole message — losing the notification entirely,
        which is the failure mode this card is trying to fix. The tail is kept
        rather than the head because the assertion is at the end."""
        text = (detail or "").strip()
        if not text:
            return "（沒有輸出）"
        room = 2400
        if len(text) > room:
            text = "…（前面省略）\n" + text[-room:]
        return "── 關卡輸出 ──\n" + text

    def _preview_names(self, job_id: str | None) -> list[str]:
        if not job_id or not self.home_root:
            return []
        folder = Path(self.home_root) / "artifacts" / job_id / "preview"
        if not folder.is_dir():
            return []
        return sorted(p.name for p in folder.iterdir() if p.is_file())

    async def _send_previews(self, chat_id: int, job_id: str | None,
                             names: list[str]) -> None:
        """Deliver the review package, not merely filenames.

        Images are photos, videos are playable, and reports/PDFs are documents.
        Telegram therefore shows the same evidence as the card instead of a
        promise that the real material exists somewhere in WebUI.
        """
        if not job_id or not self.home_root:
            return
        folder = Path(self.home_root) / "artifacts" / job_id / "preview"
        for name in names[:10]:
            path = folder / Path(name).name
            if not path.is_file():
                continue
            ext = path.suffix.lower()
            endpoint = "/sendPhoto"
            field = "photo"
            if ext in (".mp4", ".mov"):
                endpoint, field = "/sendVideo", "video"
            elif ext not in (".png", ".jpg", ".jpeg", ".webp"):
                endpoint, field = "/sendDocument", "document"
            await self._post_delivery(
                endpoint, purpose=f"preview:{job_id}", target=name,
                data={"chat_id": str(chat_id), "caption": name},
                files={field: (name, path.read_bytes())})

    SEND_RETRIES = 3
    SEND_BACKOFF_S = (2, 5)

    async def _post_delivery(self, endpoint: str, *, purpose: str,
                             target: str = "", **kwargs) -> bool:
        """One outbound message, retried, and accounted for either way.

        This host's route to api.telegram.org is provably flaky (the poll loop
        logs ConnectTimeout in bursts), and a one-shot send with no record left
        "did the approval evidence ever reach Telegram?" unanswerable — the
        operator said no, the log said nothing. Now every delivery is an audit
        row: notify.sent or notify.failed, with what and where."""
        last: Exception | None = None
        for attempt in range(self.SEND_RETRIES):
            try:
                response = await self._client.post(endpoint, **kwargs)
                response.raise_for_status()
                self.db.audit("channel:telegram", "notify.sent", "channel",
                              self.channel_id,
                              {"purpose": purpose, "endpoint": endpoint,
                               "target": target, "attempt": attempt + 1})
                return True
            except httpx.HTTPError as exc:
                last = exc
                if attempt < self.SEND_RETRIES - 1:
                    await asyncio.sleep(self.SEND_BACKOFF_S[
                        min(attempt, len(self.SEND_BACKOFF_S) - 1)])
        log.warning("telegram delivery failed after %d attempts (%s): %s",
                    self.SEND_RETRIES, purpose, type(last).__name__)
        self.db.audit("channel:telegram", "notify.failed", "channel",
                      self.channel_id,
                      {"purpose": purpose, "endpoint": endpoint, "target": target,
                       "error": type(last).__name__ if last else "unknown"})
        return False

    async def _send(self, chat_id: int, text: str, reply_markup: dict | None = None,
                    purpose: str = "message") -> None:
        # Telegram rejects anything over 4096 chars with a 400, and a rejected
        # notification is a notification nobody gets
        payload: dict = {"chat_id": chat_id, "text": text[:MAX_TELEGRAM_TEXT]}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self._post_delivery("/sendMessage", purpose=purpose,
                                  target=text[:60], json=payload)

    # -- summaries --------------------------------------------------------------------

    def _status_text(self) -> str:
        rows = self.db.query("SELECT status, COUNT(*) n FROM jobs GROUP BY status")
        counts = " · ".join(f"{r['status']}: {r['n']}" for r in rows) or "no jobs"
        cost = self.db.one("SELECT COALESCE(SUM(cost_usd),0) c FROM runs")
        return f"🐈 {counts}\nΣ cost ${cost['c']:.4f}"

    def _jobs_text(self) -> str:
        rows = self.db.query(
            "SELECT id, title, stage, status FROM jobs ORDER BY updated_at DESC LIMIT 8")
        if not rows:
            return "no jobs yet"
        return "\n".join(f"{r['status']} · {r['id']} · {r['title'][:40]} ({r['stage']})"
                         for r in rows)
