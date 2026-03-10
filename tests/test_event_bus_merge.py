from __future__ import annotations

from pathlib import Path
from typing import Iterator

from aura.runtime.event_bus import EventBus
from aura.runtime.protocol import Event, EventKind


class _DummyEventLogStore:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def append(self, event: Event) -> None:
        self.events.append(event)

    def read(self, session_id: str, since_event_id: str | None = None) -> Iterator[Event]:
        return iter(())

    def export_bundle(self, session_id: str, output_dir: Path) -> Path:
        return output_dir


def _event(
    *,
    kind: str,
    event_id: str,
    session_id: str = "sess_1",
    request_id: str = "req_1",
    turn_id: str = "turn_1",
    step_id: str = "step_1",
    timestamp: int = 1,
    payload: dict[str, object] | None = None,
) -> Event:
    return Event(
        kind=kind,
        payload=dict(payload or {}),
        session_id=session_id,
        event_id=event_id,
        timestamp=timestamp,
        request_id=request_id,
        turn_id=turn_id,
        step_id=step_id,
    )


def test_mergeable_progress_events_are_deduped_until_flushed() -> None:
    store = _DummyEventLogStore()
    bus = EventBus(event_log_store=store)

    bus.publish(
        _event(
            kind=EventKind.OPERATION_PROGRESS.value,
            event_id="evt_1",
            payload={"progress": 1},
        )
    )
    bus.publish(
        _event(
            kind=EventKind.OPERATION_PROGRESS.value,
            event_id="evt_2",
            payload={"progress": 2},
            timestamp=2,
        )
    )

    assert store.events == []

    bus.publish(
        _event(
            kind=EventKind.OPERATION_COMPLETED.value,
            event_id="evt_3",
            payload={"ok": True},
            timestamp=3,
        )
    )

    assert [event.kind for event in store.events] == [
        EventKind.OPERATION_PROGRESS.value,
        EventKind.OPERATION_COMPLETED.value,
    ]
    merged = store.events[0]
    assert merged.event_id == "evt_2"
    assert merged.payload.get("progress") == 2


def test_flush_can_target_single_session() -> None:
    store = _DummyEventLogStore()
    bus = EventBus(event_log_store=store)

    bus.publish(
        _event(
            kind=EventKind.OPERATION_PROGRESS.value,
            event_id="evt_a",
            session_id="sess_a",
            request_id="req_a",
            turn_id="turn_a",
            step_id="step_a",
            payload={"progress": "a"},
        )
    )
    bus.publish(
        _event(
            kind=EventKind.OPERATION_PROGRESS.value,
            event_id="evt_b",
            session_id="sess_b",
            request_id="req_b",
            turn_id="turn_b",
            step_id="step_b",
            payload={"progress": "b"},
        )
    )

    bus.flush(session_id="sess_a")
    assert len(store.events) == 1
    assert store.events[0].session_id == "sess_a"

    bus.flush()
    assert len(store.events) == 2
    assert store.events[1].session_id == "sess_b"
