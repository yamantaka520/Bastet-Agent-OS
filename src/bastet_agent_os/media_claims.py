"""Durable background retrieval for asynchronous media providers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from . import resource_kinds, secrets_store
from .db import Db, new_id, now

MEDIA_KINDS = {"image", "video", "music", "tts", "stt", "model3d"}
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
TERMINAL = {"fetched", "failed"}
POLL_S = 2.0
FETCH_LEASE_S = 120


class MediaClaimError(ValueError):
    pass


def _csv(value: Any, default: tuple[str, ...] = ()) -> set[str]:
    if isinstance(value, list):
        parts = value
    else:
        parts = str(value or "").split(",")
    found = {str(part).strip().lower() for part in parts if str(part).strip()}
    return found or set(default)


def _field(payload: Any, path: str) -> Any:
    current = payload
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _safe_destination(workdir: str, destination: str) -> Path:
    if not workdir or not Path(workdir).is_dir():
        raise MediaClaimError("run worktree is unavailable")
    relative = Path(str(destination or ""))
    if not destination or relative.is_absolute() or ".." in relative.parts:
        raise MediaClaimError("media destination must be a relative worktree path")
    root = Path(workdir).resolve()
    target = (root / relative).resolve(strict=False)
    if not target.is_relative_to(root) or target == root:
        raise MediaClaimError("media destination escapes the run worktree")
    return target


def register(db: Db, run, resource, *, provider_task_id: str,
             destination: str, expires_at: str = "") -> dict:
    if resource["kind"] not in MEDIA_KINDS:
        raise MediaClaimError("async claims require a media resource")
    config = json.loads(resource["config_json"] or "{}")
    status_path = str(config.get("async_status_path") or "").strip()
    if "{task_id}" not in status_path or not status_path.startswith("/"):
        raise MediaClaimError(
            "resource async_status_path must start with / and contain {task_id}")
    provider_task_id = str(provider_task_id or "").strip()
    if not TASK_ID.fullmatch(provider_task_id):
        raise MediaClaimError("provider task id has an unsafe shape")
    _safe_destination(run["workdir"] or "", destination)
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                raise ValueError
        except ValueError as exc:
            raise MediaClaimError("expires_at must be a future ISO-8601 timestamp") from exc
    claim_id = new_id("med")
    stamp = now()
    try:
        db.write(
            "INSERT INTO media_claims(id,run_id,job_id,resource_id,provider_task_id,"
            "destination,status,next_poll_at,expires_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'pending',?,?,?,?)",
            (claim_id, run["id"], run["job_id"], resource["id"], provider_task_id,
             destination, stamp, expires_at or None, stamp, stamp))
    except Exception:
        existing = db.one(
            "SELECT * FROM media_claims WHERE run_id=? AND resource_id=? "
            "AND provider_task_id=? AND destination=?",
            (run["id"], resource["id"], provider_task_id, destination))
        if existing is None:
            raise
        return dict(existing)
    db.audit(f"run:{run['id']}", "media.claimed", "media_claim", claim_id,
             {"job_id": run["job_id"], "resource_id": resource["id"],
              "destination": destination})
    return dict(db.one("SELECT * FROM media_claims WHERE id=?", (claim_id,)))


def pending_for_run(db: Db, run_id: str) -> list[dict]:
    return [dict(row) for row in db.query(
        "SELECT * FROM media_claims WHERE run_id=? AND status NOT IN ('fetched','failed')",
        (run_id,))]


def park_if_pending(db: Db, job, run_id: str, stage: str, *, emit=None) -> bool:
    claims = pending_for_run(db, run_id)
    if not claims:
        return False
    destinations = [claim["destination"] for claim in claims]
    note = ("非同步媒體仍由 Bastet 背景擷取；完成後會自動重跑本階段核驗。等待："
            + ", ".join(destinations))
    db.write("UPDATE runs SET status='waiting_external',finished_at=NULL WHERE id=?",
             (run_id,))
    db.write("UPDATE jobs SET status='blocked',rework_note=?,updated_at=? WHERE id=?",
             (note, now(), job["id"]))
    db.write("UPDATE job_stage_nodes SET status='blocked',updated_at=? "
             "WHERE job_id=? AND stage=?", (now(), job["id"], stage))
    db.audit("orchestrator", "media.waiting", "job", job["id"],
             {"run_id": run_id, "stage": stage, "claims": len(claims),
              "destinations": destinations})
    if emit:
        emit("media.waiting", job["project_id"], job_id=job["id"], run_id=run_id,
             title=job["title"], stage=stage, claims=len(claims))
    return True


class MediaClaimWorker:
    def __init__(self, db: Db, orch, bus=None, *, transport=None):
        self.db = db
        self.orch = orch
        self.bus = bus
        self.transport = transport

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                import logging
                logging.getLogger("bastet.media").exception("media claim sweep failed")
            await asyncio.sleep(POLL_S)

    async def run_once(self) -> int:
        rows = [dict(row) for row in self.db.query(
            "SELECT * FROM media_claims WHERE (status='pending' AND "
            "(next_poll_at IS NULL OR next_poll_at<=?)) OR (status='fetching' AND "
            "julianday(updated_at)<=julianday('now',?)) ORDER BY created_at LIMIT 8",
            (now(), f"-{FETCH_LEASE_S} seconds"))]
        if not rows:
            return 0
        await asyncio.gather(*(self._claim(row) for row in rows))
        return len(rows)

    async def _claim(self, claim: dict) -> None:
        recovered = claim["status"] == "fetching"
        changed = self.db.write(
            "UPDATE media_claims SET status='fetching',attempts=attempts+1,updated_at=? "
            "WHERE id=? AND (status='pending' OR (status='fetching' AND "
            "julianday(updated_at)<=julianday('now',?)))",
            (now(), claim["id"], f"-{FETCH_LEASE_S} seconds")).rowcount
        if not changed:
            return
        if recovered:
            self.db.audit("media-fetcher", "media.claim_recovered", "media_claim",
                          claim["id"], {"previous_updated_at": claim["updated_at"]})
        claim = dict(self.db.one("SELECT * FROM media_claims WHERE id=?", (claim["id"],)))
        resource = self.db.one("SELECT * FROM resources WHERE id=? AND enabled=1",
                               (claim["resource_id"],))
        try:
            if resource is None:
                raise MediaClaimError("media resource is missing or disabled")
            await self._poll_and_fetch(claim, resource)
        except Exception as exc:
            error = (f"provider network error: {type(exc).__name__}"
                     if isinstance(exc, httpx.RequestError) else str(exc))
            self._retry_or_fail(
                claim, resource, error, terminal=isinstance(exc, MediaClaimError))
        await self._finish_run_if_settled(claim["run_id"])

    async def _poll_and_fetch(self, claim: dict, resource) -> None:
        config = json.loads(resource["config_json"] or "{}")
        path = str(config["async_status_path"]).replace(
            "{task_id}", quote(claim["provider_task_id"], safe=""))
        status_url = urljoin(resource["endpoint"].rstrip("/") + "/", path.lstrip("/"))
        if urlparse(status_url).netloc != urlparse(resource["endpoint"]).netloc:
            raise MediaClaimError("async status URL escaped the resource host")
        secret = secrets_store.resolve(
            secrets_store.expand(self.db, resource["secret_ref"] or ""))
        header, value = resource_kinds.auth_header_pair(config, secret)
        async with httpx.AsyncClient(
                transport=self.transport, timeout=30, follow_redirects=False) as client:
            response = await client.get(status_url, headers={header: value})
            if response.status_code >= 400:
                message = f"status endpoint returned HTTP {response.status_code}"
                if response.status_code < 500 and response.status_code not in (408, 429):
                    raise MediaClaimError(message)
                raise RuntimeError(message)
            payload = response.json()
            provider_status = str(_field(
                payload, config.get("async_status_field") or "status") or "").lower()
            success = _csv(config.get("async_success_values"),
                           ("succeeded", "completed", "success"))
            failures = _csv(config.get("async_failure_values"),
                            ("failed", "error", "cancelled", "canceled"))
            if provider_status in failures:
                raise MediaClaimError(f"provider task ended as {provider_status}")
            if provider_status not in success:
                self._schedule(claim, config, provider_status)
                return
            result_url = str(_field(
                payload, config.get("async_result_url_field") or "output.url") or "")
            parsed = urlparse(result_url)
            allowed = {urlparse(resource["endpoint"]).hostname or ""}
            allowed |= _csv(config.get("async_download_hosts"))
            if parsed.scheme not in ("http", "https") or not parsed.hostname \
                    or parsed.hostname.lower() not in {host.lower() for host in allowed}:
                raise MediaClaimError("result URL host is not allow-listed")
            max_bytes = min(500 * 1024 * 1024, max(1, int(
                config.get("async_max_bytes") or 100 * 1024 * 1024)))
            content = bytearray()
            async with client.stream("GET", result_url) as download:
                if download.status_code >= 400:
                    message = f"result download returned HTTP {download.status_code}"
                    if download.status_code < 500 and download.status_code not in (408, 429):
                        raise MediaClaimError(message)
                    raise RuntimeError(message)
                declared = int(download.headers.get("content-length") or 0)
                if declared > max_bytes:
                    raise MediaClaimError(f"media result exceeds {max_bytes} bytes")
                async for chunk in download.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise MediaClaimError(f"media result exceeds {max_bytes} bytes")
                mime = download.headers.get("content-type", "").split(";", 1)[0]
            run = self.db.one("SELECT workdir FROM runs WHERE id=?", (claim["run_id"],))
            target = _safe_destination(run["workdir"] if run else "",
                                       claim["destination"])
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = ""
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                    handle.write(content)
                    temporary = handle.name
                os.replace(temporary, target)
                temporary = ""
            finally:
                if temporary:
                    Path(temporary).unlink(missing_ok=True)
            digest = hashlib.sha256(content).hexdigest()
            self.db.write(
                "UPDATE media_claims SET status='fetched',provider_status=?,bytes=?,"
                "sha256=?,mime=?,error='',finished_at=?,updated_at=? WHERE id=?",
                (provider_status, len(content), digest, mime, now(), now(),
                 claim["id"]))
            self.db.audit("media-fetcher", "media.fetched", "media_claim", claim["id"],
                          {"destination": claim["destination"], "bytes": len(content),
                           "sha256": digest, "provider_status": provider_status})

    def _schedule(self, claim: dict, config: dict, provider_status: str) -> None:
        interval = min(3600, max(1, int(config.get("async_poll_interval_seconds") or 10)))
        next_poll = (datetime.now(UTC) + timedelta(seconds=interval)).isoformat(
            timespec="seconds")
        self.db.write("UPDATE media_claims SET status='pending',provider_status=?,"
                      "next_poll_at=?,updated_at=? WHERE id=?",
                      (provider_status, next_poll, now(), claim["id"]))

    def _retry_or_fail(self, claim: dict, resource, error: str, *, terminal: bool) -> None:
        config = json.loads(resource["config_json"] or "{}") if resource else {}
        max_attempts = min(100_000, max(1, int(config.get("async_max_attempts") or 720)))
        expires = claim.get("expires_at") or ""
        expired = False
        if expires:
            try:
                expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                expired = expiry <= datetime.now(UTC)
            except ValueError:
                expired = True
        if not terminal and claim["attempts"] < max_attempts and not expired:
            self._schedule(claim, config, claim.get("provider_status") or "")
            self.db.write("UPDATE media_claims SET error=? WHERE id=?",
                          (error[:1000], claim["id"]))
            return
        self.db.write("UPDATE media_claims SET status='failed',error=?,finished_at=?,"
                      "updated_at=? WHERE id=?", (error[:1000], now(), now(), claim["id"]))
        self.db.audit("media-fetcher", "media.failed", "media_claim", claim["id"],
                      {"error": error[:500], "attempts": claim["attempts"]})

    async def _finish_run_if_settled(self, run_id: str) -> None:
        claims = [dict(row) for row in self.db.query(
            "SELECT * FROM media_claims WHERE run_id=?", (run_id,))]
        if not claims or any(claim["status"] not in TERMINAL for claim in claims):
            return
        run = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if run is None or run["status"] != "waiting_external":
            return
        job = self.db.one("SELECT * FROM jobs WHERE id=?", (run["job_id"],))
        failed = [claim for claim in claims if claim["status"] == "failed"]
        if failed:
            detail = "; ".join(f"{c['destination']}: {c['error']}" for c in failed)[:1500]
            self.db.write("UPDATE runs SET status='failed',error=?,finished_at=? WHERE id=?",
                          (detail, now(), run_id))
            self.db.write("UPDATE jobs SET rework_note=?,updated_at=? WHERE id=?",
                          (f"非同步媒體擷取失敗：{detail}", now(), job["id"]))
            if self.bus:
                self.bus.emit("media.failed", job["project_id"], job_id=job["id"],
                              run_id=run_id, title=job["title"], detail=detail)
            return
        destinations = [claim["destination"] for claim in claims]
        self.db.write("UPDATE runs SET status='succeeded',finished_at=? WHERE id=?",
                      (now(), run_id))
        self.db.write("UPDATE jobs SET rework_note=?,updated_at=? WHERE id=?",
                      ("背景媒體已保存，請核驗後繼續：" + ", ".join(destinations),
                       now(), job["id"]))
        if self.bus:
            self.bus.emit("media.fetched", job["project_id"], job_id=job["id"],
                          run_id=run_id, title=job["title"], files=destinations)
        if job["status"] == "blocked":
            try:
                self.orch.retry(job["id"], user="server:media-fetcher",
                                refresh_workflow=True)
            except Exception as exc:
                self.db.audit("media-fetcher", "media.resume_failed", "job", job["id"],
                              {"error": str(exc)[:500], "files": destinations})
