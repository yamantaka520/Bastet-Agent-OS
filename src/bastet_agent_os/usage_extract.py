"""Usage extraction from OpenAI- and Anthropic-flavor responses (SPEC §5.2.2).

Pure functions/accumulators so accounting is unit-testable without a live
upstream. Cache tokens are kept separate from plain input tokens — they are
priced differently and Claude Code uses prompt caching heavily.
"""

from __future__ import annotations

import json

from .pricing import Usage


def openai_usage(payload: dict) -> Usage:
    """Usage from a non-stream OpenAI response body (or final stream chunk)."""
    u = payload.get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    return Usage(
        tokens_in=int(u.get("prompt_tokens") or 0) - cached,
        tokens_out=int(u.get("completion_tokens") or 0),
        cache_read=cached,
        cache_write=0,
    )


def responses_usage(payload: dict) -> Usage:
    """Usage from an OpenAI Responses API response object (codex's wire API).

    Responses counts cached tokens INSIDE input_tokens (details block) and
    reasoning tokens INSIDE output_tokens — no double counting here."""
    u = payload.get("usage") or {}
    cached = int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    return Usage(
        tokens_in=max(0, int(u.get("input_tokens") or 0) - cached),
        tokens_out=int(u.get("output_tokens") or 0),
        cache_read=cached,
        cache_write=0,
    )


def anthropic_usage(payload: dict) -> Usage:
    """Usage from a non-stream Anthropic response body."""
    u = payload.get("usage") or {}
    return Usage(
        tokens_in=int(u.get("input_tokens") or 0),
        tokens_out=int(u.get("output_tokens") or 0),
        cache_read=int(u.get("cache_read_input_tokens") or 0),
        cache_write=int(u.get("cache_creation_input_tokens") or 0),
    )


class SseUsageAccumulator:
    """Feed raw SSE lines from a streamed response; read .usage at the end.

    OpenAI: usage arrives only in the final chunk, and only when the request
    was sent with stream_options.include_usage (the gateway injects it).
    Anthropic: input/cache tokens arrive in message_start, cumulative output
    tokens in message_delta events.
    """

    def __init__(self, flavor: str):
        self.flavor = flavor
        self.usage = Usage()
        self.model: str | None = None
        self.complete = False  # saw a final usage marker; else cost is partial

    def feed_line(self, line: str) -> None:
        line = line.strip()
        if not line.startswith("data:"):
            return
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return
        if self.flavor == "openai":
            self._feed_openai(obj)
        elif self.flavor == "openai-responses":
            self._feed_responses(obj)
        else:
            self._feed_anthropic(obj)

    def _feed_openai(self, obj: dict) -> None:
        if obj.get("model"):
            self.model = obj["model"]
        if obj.get("usage"):
            self.usage = openai_usage(obj)
            self.complete = True

    def _feed_responses(self, obj: dict) -> None:
        # Responses SSE: the final `response.completed` event carries the
        # full response object, usage included
        if obj.get("type") == "response.completed":
            response = obj.get("response") or {}
            if response.get("model"):
                self.model = response["model"]
            self.usage = responses_usage(response)
            self.complete = True

    def _feed_anthropic(self, obj: dict) -> None:
        kind = obj.get("type")
        if kind == "message_start":
            message = obj.get("message") or {}
            if message.get("model"):
                self.model = message["model"]
            u = message.get("usage") or {}
            self.usage.tokens_in = int(u.get("input_tokens") or 0)
            self.usage.cache_read = int(u.get("cache_read_input_tokens") or 0)
            self.usage.cache_write = int(u.get("cache_creation_input_tokens") or 0)
        elif kind == "message_delta":
            u = obj.get("usage") or {}
            if u.get("output_tokens") is not None:
                self.usage.tokens_out = int(u["output_tokens"])  # cumulative
                self.complete = True


def inject_stream_options(body: dict) -> dict:
    """OpenAI streams omit usage unless stream_options.include_usage is set;
    the gateway rewrites the request body to demand it (SPEC §5.2.2)."""
    if body.get("stream"):
        opts = dict(body.get("stream_options") or {})
        opts["include_usage"] = True
        body = {**body, "stream_options": opts}
    return body
