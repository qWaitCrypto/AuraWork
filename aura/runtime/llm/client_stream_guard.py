from __future__ import annotations

import threading
import time
from typing import Any

from .errors import CancellationToken


def _maybe_close_stream(stream: Any) -> None:
    # Best-effort close: various SDKs wrap the underlying HTTP response object differently.
    close = getattr(stream, "close", None)
    if callable(close):
        close()
        return
    aclose = getattr(stream, "aclose", None)
    if callable(aclose):
        try:
            aclose()
        except Exception:
            pass
        return
    for attr in ("response", "_response", "http_response", "_http_response"):
        inner = getattr(stream, attr, None)
        if inner is None:
            continue
        close2 = getattr(inner, "close", None)
        if callable(close2):
            try:
                close2()
            except Exception:
                pass
            return


def _start_cancel_closer(cancel: CancellationToken | None, stream: Any) -> threading.Event | None:
    if cancel is None:
        return None
    stop = threading.Event()

    def _run() -> None:
        while not stop.is_set():
            if cancel.cancelled:
                try:
                    _maybe_close_stream(stream)
                except Exception:
                    pass
                return
            stop.wait(0.05)

    t = threading.Thread(target=_run, name="aura-llm-stream-cancel", daemon=True)
    t.start()
    return stop


def _start_stream_idle_watchdog(
    *,
    stream: Any,
    cancel: CancellationToken | None,
    first_event_timeout_s: float | None,
    idle_timeout_s: float | None,
) -> tuple[threading.Event, threading.Event, callable, callable]:
    """
    Best-effort guard for buggy "streaming" endpoints that never send a terminal event / never close.

    - `first_event_timeout_s`: max time waiting for the first stream item.
    - `idle_timeout_s`: max time between stream items after the first item.

    When triggered, calls `stream.close()` and sets `timed_out`.
    """
    stop = threading.Event()
    timed_out = threading.Event()
    lock = threading.Lock()
    last_progress = [time.monotonic()]
    saw_any = [False]
    phase: list[str | None] = [None]

    def tick() -> None:
        with lock:
            last_progress[0] = time.monotonic()
            saw_any[0] = True

    def timed_out_phase() -> str | None:
        with lock:
            return phase[0]

    def _run() -> None:
        started = time.monotonic()
        while not stop.is_set():
            if cancel is not None and cancel.cancelled:
                return
            now = time.monotonic()
            with lock:
                last = last_progress[0]
                any_seen = saw_any[0]
            if (not any_seen) and first_event_timeout_s is not None and (now - started) >= first_event_timeout_s:
                with lock:
                    phase[0] = "first_event"
                timed_out.set()
                try:
                    _maybe_close_stream(stream)
                except Exception:
                    pass
                return
            if any_seen and idle_timeout_s is not None and (now - last) >= idle_timeout_s:
                with lock:
                    phase[0] = "idle"
                timed_out.set()
                try:
                    _maybe_close_stream(stream)
                except Exception:
                    pass
                return
            stop.wait(0.05)

    t = threading.Thread(target=_run, name="aura-llm-stream-idle", daemon=True)
    t.start()
    return stop, timed_out, tick, timed_out_phase

