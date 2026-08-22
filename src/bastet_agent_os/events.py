"""In-process event bus (SPEC §5.10).

One typed event stream shared by the WS API (M2) and, later, channels (M4).
Single-process by design (control plane + gateway share the process), so a
simple fan-out of asyncio queues is enough.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

log = logging.getLogger("bastet.events")

# Every type the engine may publish. This is a registry, not a filter: an
# unlisted type is still delivered, with a warning — dropping it would silently
# disable whatever depended on it, which is worse than a noisy log. The list had
# gone stale by eight types (including `job.rework`), so every rework logged
# `unknown event type` on a healthy system. tests/test_events.py fails if the code
# emits a literal type that is not listed here.
EVENT_TYPES = {
    # jobs
    "job.created", "job.stage_changed", "job.done", "job.blocked", "job.cancelled",
    "job.rework", "job.resumed", "job.retried", "job.archived", "job.deleted",
    "job.supplied", "job.pushed", "job.push_failed", "job.quota_wait",
    "job.pm_intervention",
    # runs
    "run.queued", "run.started", "run.waiting_input", "run.finished",
    "run.progress", "run.stalled_interrupted",
    # gates (emitted as gate.<verdict>)
    "gate.pending", "gate.passed", "gate.failed",
    # projects
    "project.status", "project.deleted", "room.message",
    # everything else
    "chat.message",
    "budget.warning", "budget.exceeded",
    "resource.health_changed",
    "agent.depleted",
    "channel.paired",
}

QUEUE_LIMIT = 500  # slow subscribers drop oldest events rather than block the engine


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def emit(self, event_type: str, project_id: str | None = None, **payload) -> None:
        if event_type not in EVENT_TYPES:
            log.warning("unknown event type %r", event_type)
        event = {
            "type": event_type,
            "project_id": project_id,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            **payload,
        }
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:  # drop oldest, keep the stream alive
                    queue.get_nowait()
                    queue.put_nowait(event)
                except asyncio.QueueEmpty:
                    pass

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)


def dumps(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False)
