from __future__ import annotations

import asyncio
from pathlib import Path

from aura.runtime.engine import RunResult
from aura.runtime.protocol import EventKind, Op, OpKind
from web.backend.aura_web.runtime import WebRuntime


class _BlockingEngine:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[Op] = []

    async def arun(self, op: Op, *, timeout_s=None, cancel=None) -> RunResult:
        _ = timeout_s
        _ = cancel
        self.calls.append(op)
        self.started.set()
        await self.release.wait()
        return RunResult(status="completed", run_id=op.request_id, session_id=op.session_id)


async def _wait_until(predicate, *, timeout_s: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        if predicate():
            return
        if loop.time() >= deadline:
            raise TimeoutError("predicate did not become true in time")
        await asyncio.sleep(0.01)


def test_web_runtime_tracks_background_session_op_and_busy_state(monkeypatch, tmp_path: Path) -> None:
    async def _run() -> None:
        runtime = WebRuntime(project_root=tmp_path)
        session_id = runtime.ensure_session(session_id=None)
        engine = _BlockingEngine()
        monkeypatch.setattr(runtime, "engine_for_session", lambda *, session_id: engine)

        first = Op(
            kind=OpKind.CHAT.value,
            payload={"text": "hello"},
            session_id=session_id,
            request_id="req-1",
            timestamp=runtime.now_ts_ms(),
            turn_id="turn-1",
        )
        started, info = runtime.start_op_for_session(session_id=session_id, op=first)
        assert started is True
        assert info.request_id == "req-1"

        await asyncio.wait_for(engine.started.wait(), timeout=1.0)
        active = runtime.active_op_for_session(session_id)
        assert active is not None
        assert active.request_id == "req-1"

        second = Op(
            kind=OpKind.COMPACT.value,
            payload={},
            session_id=session_id,
            request_id="req-2",
            timestamp=runtime.now_ts_ms(),
            turn_id="turn-2",
        )
        started_second, busy_info = runtime.start_op_for_session(session_id=session_id, op=second)
        assert started_second is False
        assert busy_info.request_id == "req-1"

        engine.release.set()
        await _wait_until(lambda: runtime.active_op_for_session(session_id) is None)
        assert [op.request_id for op in engine.calls] == ["req-1"]

    asyncio.run(_run())


def test_web_runtime_emits_operation_failed_when_background_op_crashes(monkeypatch, tmp_path: Path) -> None:
    async def _run() -> None:
        runtime = WebRuntime(project_root=tmp_path)
        session_id = runtime.ensure_session(session_id=None)

        def _boom(*, session_id: str):
            raise RuntimeError(f"boom:{session_id}")

        monkeypatch.setattr(runtime, "engine_for_session", _boom)

        op = Op(
            kind=OpKind.CHAT.value,
            payload={"text": "hello"},
            session_id=session_id,
            request_id="req-crash",
            timestamp=runtime.now_ts_ms(),
            turn_id="turn-crash",
        )
        started, _info = runtime.start_op_for_session(session_id=session_id, op=op)
        assert started is True

        await _wait_until(lambda: runtime.active_op_for_session(session_id) is None)

        events = list(runtime.event_log_store.read(session_id))
        failures = [ev for ev in events if ev.kind == EventKind.OPERATION_FAILED.value and ev.request_id == "req-crash"]
        assert failures
        assert failures[-1].payload.get("op_kind") == OpKind.CHAT.value
        assert failures[-1].payload.get("error_code") is not None
        assert failures[-1].payload.get("error") == "Operation crashed before completion."

    asyncio.run(_run())
