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
    "/pair <code> — link this Telegram account"
)


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
                 transport: httpx.AsyncBaseTransport | None = None):
        self.db = db
        self.orch = orchestrator
        self.bus = bus
        self.channel_id = channel_id
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{bot_token}",
            timeout=httpx.Timeout(POLL_TIMEOUT_S + 10, connect=15),
            transport=transport,
        )
        self._stopping = asyncio.Event()

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
            await self._poll_loop()
        finally:
            notify_task.cancel()
            await self._client.aclose()

    def stop(self) -> None:
        self._stopping.set()

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
        queue = self.bus.subscribe()
        try:
            while True:
                event = await queue.get()
                await self._notify(event)
        finally:
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
        else:
            await self._send(chat["id"], HELP_TEXT)

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
        await self._send(chat_id, f"✅ Paired as {payload['name']}. {HELP_TEXT}")

    async def _send_approval_card(self, chat_id: int, job_id: str) -> None:
        job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            await self._send(chat_id, f"unknown job {job_id}")
            return
        text = (f"⏸ {job['title']}\n{job_id} · stage {job['stage']} · {job['status']}\n\n"
                f"{(job['spec_md'] or '')[:400]}")
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"apv:{job_id}:yes"},
            {"text": "❌ Reject", "callback_data": f"apv:{job_id}:no"},
        ]]}
        await self._send(chat_id, text, reply_markup=keyboard)

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
        if etype == "gate.pending":
            text = (f"⏸ approval needed: {event.get('job_id')} "
                    f"(stage {event.get('stage')})")
            keyboard = {"inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"apv:{event.get('job_id')}:yes"},
                {"text": "❌ Reject", "callback_data": f"apv:{event.get('job_id')}:no"},
            ]]}
        elif etype == "run.waiting_input":
            text = (f"✋ run {event.get('run_id')} asks: {event.get('kind')}\n"
                    f"{event.get('summary') or ''}")
            keyboard = {"inline_keyboard": [[
                {"text": "✅ Allow",
                 "callback_data": f"itx:{event.get('run_id')}:{event.get('request_id')}:yes"},
                {"text": "❌ Deny",
                 "callback_data": f"itx:{event.get('run_id')}:{event.get('request_id')}:no"},
            ]]}
        elif etype in ("job.done", "job.blocked", "budget.exceeded", "budget.warning"):
            icon = {"job.done": "✅", "job.blocked": "🟠",
                    "budget.exceeded": "🛑", "budget.warning": "⚠️"}[etype]
            detail = event.get("reason") or event.get("stage") or ""
            text = f"{icon} {etype}: {event.get('job_id') or event.get('grant_id')} {detail}"
            keyboard = None
        else:
            return
        for binding in self._config().get("bindings", {}).values():
            await self._send(binding["chat_id"], text, reply_markup=keyboard)

    async def _send(self, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        payload: dict = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            await self._client.post("/sendMessage", json=payload)
        except httpx.HTTPError as exc:
            log.warning("telegram send failed: %s", type(exc).__name__)

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
