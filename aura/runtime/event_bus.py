from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

from .error_codes import ErrorCode
from .ids import new_id, now_ts_ms
from .protocol import Event
from .protocol import EventKind
from .protocol import EVENT_SCHEMA_VERSION
from .stores import EventLogStore

EventHandler = Callable[[Event], None]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EventFilter:
    kinds: set[str] | None = None
    session_id: str | None = None
    request_id: str | None = None

    def matches(self, event: Event) -> bool:
        if self.kinds is not None and event.kind not in self.kinds:
            return False
        if self.session_id is not None and event.session_id != self.session_id:
            return False
        if self.request_id is not None and event.request_id != self.request_id:
            return False
        return True


class EventBus:
    def __init__(self, *, event_log_store: EventLogStore | None = None, schema_version_default: str = EVENT_SCHEMA_VERSION) -> None:
        self._event_log_store = event_log_store
        self._schema_version_default = str(schema_version_default or "").strip() or EVENT_SCHEMA_VERSION
        self._lock = threading.RLock()
        self._last_sequence_by_session: dict[str, int] = {}
        self._next_sub_id = 1
        self._subs: dict[int, tuple[EventHandler, EventFilter]] = {}
        # Events that are useful for live UI but should not be persisted in the event log.
        # We only persist the final `llm_response_completed` (which points at an artifact).
        self._ephemeral_kinds = {
            EventKind.LLM_RESPONSE_DELTA.value,
            EventKind.LLM_THINKING_DELTA.value,
        }
        self._mergeable_kinds = {
            EventKind.OPERATION_PROGRESS.value,
            EventKind.TOOL_CALL_PROGRESS.value,
        }
        self._pending_merge: dict[tuple[str, str, str | None, str | None, str | None], Event] = {}

    def prime_sequence(self, *, session_id: str, last_sequence: int) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        try:
            last = int(last_sequence)
        except Exception:
            return
        if last < 0:
            return
        with self._lock:
            cur = self._last_sequence_by_session.get(sid, 0)
            if last > cur:
                self._last_sequence_by_session[sid] = last

    def next_sequence(self, *, session_id: str) -> int:
        sid = str(session_id or "").strip()
        if not sid:
            # Keep ordering stable even if a caller passes a bad session_id.
            sid = "__unknown__"
        with self._lock:
            last = self._last_sequence_by_session.get(sid, 0)
            nxt = last + 1
            self._last_sequence_by_session[sid] = nxt
            return nxt

    def _normalize_event(self, event: Event) -> Event:
        seq = event.sequence
        if not isinstance(seq, int):
            seq = self.next_sequence(session_id=event.session_id)

        schema_version = event.schema_version
        if schema_version is None or not str(schema_version).strip():
            schema_version = self._schema_version_default

        # Ensure payload is a plain dict (avoid shared mutation across emitters).
        payload = dict(event.payload or {})
        payload.setdefault("source", "unknown")
        return Event(
            kind=event.kind,
            payload=payload,
            session_id=event.session_id,
            event_id=event.event_id,
            timestamp=event.timestamp,
            sequence=seq,
            request_id=event.request_id,
            turn_id=event.turn_id,
            step_id=event.step_id,
            schema_version=str(schema_version),
        )

    def _dispatch(self, event: Event) -> None:
        for handler, filt in list(self._subs.values()):
            if filt.matches(event):
                handler(event)

    def _append_and_dispatch(self, event: Event) -> None:
        if self._event_log_store is not None:
            try:
                self._event_log_store.append(event)
            except Exception as e:
                self._notify_append_failed(event, e)
                raise EventLogAppendError(event=event, cause=e) from e
        self._dispatch(event)

    def _flush_locked(self, *, session_id: str | None = None) -> None:
        items = list(self._pending_merge.items())
        if not items:
            return

        if session_id is not None:
            items = [(k, v) for k, v in items if k[0] == session_id]
            if not items:
                return

        def _key(item: tuple[object, Event]) -> tuple[int, int, str]:
            _evt = item[1]
            seq = _evt.sequence if isinstance(_evt.sequence, int) else -1
            return (0 if seq >= 0 else 1, seq if seq >= 0 else _evt.timestamp, _evt.event_id)

        items.sort(key=_key)
        for key, event in items:
            self._append_and_dispatch(event)
            self._pending_merge.pop(key, None)

    def flush(self, *, session_id: str | None = None) -> None:
        with self._lock:
            self._flush_locked(session_id=session_id)

    def subscribe(self, handler: EventHandler, filt: EventFilter | None = None) -> int:
        with self._lock:
            sub_id = self._next_sub_id
            self._next_sub_id += 1
            self._subs[sub_id] = (handler, filt or EventFilter())
            return sub_id

    def unsubscribe(self, subscription_id: int) -> None:
        with self._lock:
            self._subs.pop(subscription_id, None)

    def publish(self, event: Event) -> Event:
        with self._lock:
            ev = self._normalize_event(event)
            if ev.kind in self._mergeable_kinds:
                merge_key = (
                    ev.session_id,
                    ev.kind,
                    ev.request_id,
                    ev.turn_id,
                    ev.step_id,
                )
                self._pending_merge[merge_key] = ev
                return ev

            self._flush_locked(session_id=ev.session_id)
            if ev.kind in self._ephemeral_kinds:
                self._dispatch(ev)
                return ev
            self._append_and_dispatch(ev)
            return ev

    def _notify_append_failed(self, event: Event, exc: BaseException) -> None:
        emergency = Event(
            kind=EventKind.OPERATION_FAILED.value,
            payload={
                "error": f"Failed to append event log: {exc}",
                "error_code": ErrorCode.EVENT_LOG_APPEND_FAILED.value,
                "failed_event": {"kind": event.kind, "event_id": event.event_id},
                "source": "event_bus",
            },
            session_id=event.session_id,
            event_id=new_id("evt"),
            timestamp=now_ts_ms(),
            request_id=event.request_id,
            turn_id=event.turn_id,
            step_id=event.step_id,
            sequence=self.next_sequence(session_id=event.session_id),
            schema_version=event.schema_version or self._schema_version_default,
        )
        with self._lock:
            for handler, filt in list(self._subs.values()):
                if not filt.matches(emergency):
                    continue
                try:
                    handler(emergency)
                except Exception:
                    logger.warning(
                        "EventBus emergency handler raised during append-failure notify for event_id=%s",
                        emergency.event_id,
                        exc_info=True,
                    )


@dataclass(frozen=True, slots=True)
class EventLogAppendError(RuntimeError):
    event: Event
    cause: BaseException

    def __str__(self) -> str:
        return f"Event log append failed for kind={self.event.kind!r} event_id={self.event.event_id!r}: {self.cause}"
