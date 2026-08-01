"""EVENT_TYPES is a registry, and it has to stay in step with the code.

The bus delivers an unlisted type anyway — dropping it would silently disable
whatever depended on it — but it logs `unknown event type` every time. The list
had drifted by eight types, including `job.rework`, so a perfectly healthy rework
loop wrote a warning to the log on every hand-back. These tests keep the registry
honest, which is the only reason it is worth having.
"""

import re
from pathlib import Path

import pytest

from bastet_agent_os.events import EVENT_TYPES, EventBus

SRC = Path(__file__).resolve().parent.parent / "src" / "bastet_agent_os"


def emitted_literals() -> set[str]:
    """Every event type the code publishes as a literal string."""
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")   # CI Windows is cp1252
        found |= set(re.findall(r'(?:_emit|bus\.emit)\(\s*"([a-z_.]+)"', text))
    return found


def test_every_emitted_event_type_is_declared():
    """The whole failure mode in one assertion."""
    missing = emitted_literals() - EVENT_TYPES

    assert not missing, (
        f"these types are emitted but not in EVENT_TYPES, so the bus drops them: "
        f"{sorted(missing)}")


def test_the_types_notifications_depend_on_are_declared():
    """Named explicitly because these are the ones a person waits for."""
    for event_type in ("gate.pending", "job.blocked", "job.rework", "job.done",
                       "run.waiting_input"):
        assert event_type in EVENT_TYPES, event_type


def test_an_unknown_type_is_still_delivered_but_complains(caplog):
    """Deliberate: a new event type that nobody registered is a bookkeeping bug,
    not a reason to lose the event. The warning is how it gets noticed."""
    bus = EventBus()
    queue = bus.subscribe()

    with caplog.at_level("WARNING"):
        bus.emit("job.invented", project_id="p1")

    assert not queue.empty()                     # the subscriber still got it
    assert "unknown event type" in caplog.text   # and the log says so
    bus.unsubscribe(queue)


@pytest.mark.asyncio
async def test_a_declared_type_reaches_a_subscriber():
    bus = EventBus()
    queue = bus.subscribe()

    bus.emit("job.rework", project_id="p1", job_id="job1", cycle=1)

    event = await queue.get()
    assert (event["type"], event["job_id"], event["cycle"]) == ("job.rework", "job1", 1)
    bus.unsubscribe(queue)
