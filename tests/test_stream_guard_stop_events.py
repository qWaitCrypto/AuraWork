from __future__ import annotations

import threading

from aura.runtime.llm.client_stream_guard import _start_cancel_closer, _start_stream_idle_watchdog
from aura.runtime.llm.errors import CancellationToken


class _DummyStream:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_stream_guard_returns_stop_events_with_set_method() -> None:
    stream = _DummyStream()
    cancel = CancellationToken()

    stop_closer = _start_cancel_closer(cancel, stream)
    wd_stop, wd_timed_out, wd_tick, wd_phase = _start_stream_idle_watchdog(
        stream=stream,
        cancel=cancel,
        first_event_timeout_s=1.0,
        idle_timeout_s=1.0,
    )

    assert isinstance(stop_closer, threading.Event)
    assert isinstance(wd_stop, threading.Event)
    assert isinstance(wd_timed_out, threading.Event)
    assert callable(wd_tick)
    assert callable(wd_phase)

    # Regression guard: callers should use `.set()`, not call the Event object.
    stop_closer.set()
    wd_stop.set()
