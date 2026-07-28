"""LLM Gateway (SPEC §5.2): an authenticated, metering pass-through proxy.

Every request authenticates with a run token; upstream credentials never leave
this process. Usage lands in usage_ledger per request. Errors are masked —
never echo upstream auth material.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import run_tokens, secrets_store
from .db import Db, new_id, now
from .governance import QuotaError, Reservations, resolve_grant
from .pricing import PriceBook, Usage
from .usage_extract import (
    SseUsageAccumulator,
    anthropic_usage,
    inject_stream_options,
    openai_usage,
)

log = logging.getLogger("bastet.gateway")

FLAVOR_PATHS = {
    "openai": "/v1/chat/completions",
    "anthropic": "/v1/messages",
}
# Hop-by-hop / auth headers we never forward from the client.
STRIP_REQUEST_HEADERS = {
    "host", "authorization", "x-api-key", "content-length", "connection",
    "accept-encoding",
}
STRIP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection"}


@dataclass
class GatewayContext:
    db: Db
    prices: PriceBook
    reservations: Reservations


def _auth(ctx: GatewayContext, request: Request) -> dict | None:
    """Resolve the run token (Authorization: Bearer or x-api-key) to a run row."""
    token = ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.headers.get("x-api-key", "").strip()
    if not token:
        return None
    run_id = run_tokens.verify(ctx.db, token)
    if run_id is None:
        return None
    return ctx.db.one(
        "SELECT r.*, j.project_id FROM runs r JOIN jobs j ON j.id = r.job_id WHERE r.id=?",
        (run_id,),
    )


def _record(ctx: GatewayContext, run: dict, resource: dict, model: str | None,
            usage: Usage, provider_request_id: str | None, complete: bool) -> None:
    cost = ctx.prices.cost_usd(model or "", usage)
    ctx.db.write(
        "INSERT INTO usage_ledger(id, run_id, resource_id, model, provider_request_id, "
        "tokens_in, tokens_out, cache_read, cache_write, cost_usd, at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (new_id("ldg"), run["id"], resource["id"], model, provider_request_id,
         usage.tokens_in, usage.tokens_out, usage.cache_read, usage.cache_write,
         cost, now()),
    )
    ctx.db.audit(
        actor=f"run:{run['id']}", action="gateway.request",
        target_type="resource", target_id=resource["id"],
        detail={"model": model, "tokens_in": usage.tokens_in,
                "tokens_out": usage.tokens_out, "cache_read": usage.cache_read,
                "cache_write": usage.cache_write, "cost_usd": round(cost, 6),
                "complete": complete},
    )


def build_router(ctx: GatewayContext, upstream_transport: httpx.AsyncBaseTransport | None = None) -> APIRouter:
    router = APIRouter()
    client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=15), transport=upstream_transport)

    async def proxy(request: Request, flavor: str) -> Response:
        run = _auth(ctx, request)
        if run is None:
            return JSONResponse({"error": "invalid or expired run token"}, status_code=401)
        if run["status"] not in ("queued", "running", "waiting_input"):
            return JSONResponse({"error": "run is not active"}, status_code=401)
        if not run["resource_id"]:
            return JSONResponse({"error": "run has no LLM resource assigned"}, status_code=403)

        resource = ctx.db.one(
            "SELECT * FROM resources WHERE id=? AND enabled=1", (run["resource_id"],)
        )
        if resource is None or resource["kind"] != "llm":
            return JSONResponse({"error": "resource unavailable"}, status_code=403)
        if resource["api_flavor"] != flavor:
            return JSONResponse(
                {"error": f"resource speaks {resource['api_flavor']}, not {flavor}"},
                status_code=400,
            )

        grant = resolve_grant(ctx.db, resource["id"], run["project_id"], run["agent_id"])
        if grant is None:
            return JSONResponse({"error": "no grant covers this run"}, status_code=403)
        try:
            ctx.reservations.admit(ctx.db, grant)
        except QuotaError as exc:
            ctx.db.audit(f"run:{run['id']}", "gateway.quota_block", "grant", grant.id,
                         {"reason": str(exc)})
            return JSONResponse({"error": str(exc)}, status_code=429)

        try:
            body = json.loads(await request.body())
        except json.JSONDecodeError:
            ctx.reservations.settle(grant)
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if flavor == "openai":
            body = inject_stream_options(body)

        try:
            api_key = secrets_store.resolve(resource["secret_ref"])
        except secrets_store.SecretError as exc:
            ctx.reservations.settle(grant)
            return JSONResponse({"error": f"resource credential error: {exc}"}, status_code=502)
        ctx.db.audit(f"run:{run['id']}", "secret.resolve", "resource", resource["id"],
                     {"ref_scheme": (resource["secret_ref"] or "").split(":", 1)[0]})

        upstream_url = resource["endpoint"].rstrip("/") + FLAVOR_PATHS[flavor]
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in STRIP_REQUEST_HEADERS}
        if flavor == "openai":
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            headers["x-api-key"] = api_key
            headers.setdefault("anthropic-version", "2023-06-01")

        streaming = bool(body.get("stream"))
        req = client.build_request("POST", upstream_url, json=body, headers=headers)

        if not streaming:
            try:
                resp = await client.send(req)
            except httpx.HTTPError as exc:
                ctx.reservations.settle(grant)
                return JSONResponse({"error": f"upstream error: {type(exc).__name__}"},
                                    status_code=502)
            try:
                if resp.status_code == 200:
                    payload = resp.json()
                    usage = openai_usage(payload) if flavor == "openai" else anthropic_usage(payload)
                    _record(ctx, run, resource, payload.get("model"), usage,
                            payload.get("id"), complete=True)
            finally:
                ctx.reservations.settle(grant)
            content_headers = {k: v for k, v in resp.headers.items()
                               if k.lower() not in STRIP_RESPONSE_HEADERS}
            return Response(resp.content, status_code=resp.status_code, headers=content_headers)

        async def stream_body():
            acc = SseUsageAccumulator(flavor)
            buffer = ""
            try:
                resp = await client.send(req, stream=True)
                if resp.status_code != 200:
                    detail = (await resp.aread())[:2048]
                    await resp.aclose()
                    yield b"data: " + json.dumps(
                        {"error": {"message": f"upstream status {resp.status_code}",
                                   "detail": detail.decode(errors="replace")}}
                    ).encode() + b"\n\n"
                    return
                async for chunk in resp.aiter_bytes():
                    buffer += chunk.decode(errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        acc.feed_line(line)
                    yield chunk
                await resp.aclose()
            except httpx.HTTPError as exc:
                # client saw a broken stream; account what we have as partial
                log.warning("upstream stream error: %s", type(exc).__name__)
            finally:
                _record(ctx, run, resource, acc.model or body.get("model"), acc.usage,
                        None, complete=acc.complete)
                ctx.reservations.settle(grant)

        return StreamingResponse(stream_body(), media_type="text/event-stream")

    # ---- media endpoints (SPEC §5.3: image/tts/stt resource kinds, M4) --------
    # A run's token is bound to ONE llm resource; media calls pick their
    # resource per request via the X-Bastet-Resource header (id or name).
    # Governance is identical: grant required, flat per-call cost from the
    # resource's config_json.cost_per_call lands in the ledger.

    MEDIA_ENDPOINTS = {
        "/v1/images/generations": "image",
        "/v1/audio/speech": "tts",
        "/v1/audio/transcriptions": "stt",
    }

    async def media_proxy(request: Request, path: str) -> Response:
        run = _auth(ctx, request)
        if run is None:
            return JSONResponse({"error": "invalid or expired run token"}, status_code=401)
        if run["status"] not in ("queued", "running", "waiting_input"):
            return JSONResponse({"error": "run is not active"}, status_code=401)

        want_kind = MEDIA_ENDPOINTS[path]
        ref = request.headers.get("x-bastet-resource", "").strip()
        if not ref:
            return JSONResponse({"error": "X-Bastet-Resource header required"},
                                status_code=400)
        resource = ctx.db.one(
            "SELECT * FROM resources WHERE (id=? OR name=?) AND kind=? AND enabled=1",
            (ref, ref, want_kind))
        if resource is None:
            return JSONResponse({"error": f"no enabled {want_kind} resource {ref!r}"},
                                status_code=403)

        grant = resolve_grant(ctx.db, resource["id"], run["project_id"], run["agent_id"])
        if grant is None:
            return JSONResponse({"error": "no grant covers this resource"}, status_code=403)
        try:
            ctx.reservations.admit(ctx.db, grant)
        except QuotaError as exc:
            return JSONResponse({"error": str(exc)}, status_code=429)

        try:
            api_key = secrets_store.resolve(resource["secret_ref"])
        except secrets_store.SecretError as exc:
            ctx.reservations.settle(grant)
            return JSONResponse({"error": f"resource credential error: {exc}"}, status_code=502)
        ctx.db.audit(f"run:{run['id']}", "secret.resolve", "resource", resource["id"],
                     {"ref_scheme": (resource["secret_ref"] or "").split(":", 1)[0]})

        body_bytes = await request.body()
        headers = {"Authorization": f"Bearer {api_key}"}
        if request.headers.get("content-type"):
            headers["Content-Type"] = request.headers["content-type"]
        try:
            resp = await client.post(resource["endpoint"].rstrip("/") + path,
                                     content=body_bytes, headers=headers)
        except httpx.HTTPError as exc:
            ctx.reservations.settle(grant)
            return JSONResponse({"error": f"upstream error: {type(exc).__name__}"},
                                status_code=502)
        try:
            if resp.status_code == 200:
                config = json.loads(resource["config_json"] or "{}")
                calls = 1
                model = None
                if "json" in (request.headers.get("content-type") or ""):
                    try:
                        payload = json.loads(body_bytes)
                        calls = int(payload.get("n") or 1)
                        model = payload.get("model")
                    except (json.JSONDecodeError, ValueError):
                        pass
                cost = float(config.get("cost_per_call") or 0) * calls
                ctx.db.write(
                    "INSERT INTO usage_ledger(id, run_id, resource_id, model, cost_usd, at) "
                    "VALUES(?,?,?,?,?,?)",
                    (new_id("ldg"), run["id"], resource["id"],
                     model or resource["name"], cost, now()))
                ctx.db.audit(f"run:{run['id']}", "gateway.request", "resource",
                             resource["id"], {"media": want_kind, "calls": calls,
                                              "cost_usd": round(cost, 6)})
        finally:
            ctx.reservations.settle(grant)
        content_headers = {k: v for k, v in resp.headers.items()
                           if k.lower() not in STRIP_RESPONSE_HEADERS}
        return Response(resp.content, status_code=resp.status_code, headers=content_headers)

    @router.post("/v1/images/generations")
    async def images_endpoint(request: Request):
        return await media_proxy(request, "/v1/images/generations")

    @router.post("/v1/audio/speech")
    async def speech_endpoint(request: Request):
        return await media_proxy(request, "/v1/audio/speech")

    @router.post("/v1/audio/transcriptions")
    async def transcription_endpoint(request: Request):
        return await media_proxy(request, "/v1/audio/transcriptions")

    @router.post("/v1/chat/completions")
    async def openai_endpoint(request: Request):
        return await proxy(request, "openai")

    @router.post("/v1/messages")
    async def anthropic_endpoint(request: Request):
        return await proxy(request, "anthropic")

    @router.get("/v1/health")
    async def health():
        return {"ok": True}

    return router
