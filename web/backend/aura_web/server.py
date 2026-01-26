from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from aura.runtime.event_bus import EventFilter
from aura.runtime.protocol import EventKind, Op, OpKind
from aura.runtime.validate import validate_project_session

from .runtime import WebRuntime


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


@dataclass(slots=True)
class SessionHub:
    session_id: str
    runtime: WebRuntime
    clients: set[WebSocket] = field(default_factory=set)
    _sub_id: int | None = None
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _loop: asyncio.AbstractEventLoop | None = None

    async def add(self, ws: WebSocket) -> None:
        self.clients.add(ws)
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None
        if self._sub_id is None:
            self._sub_id = self.runtime.event_bus.subscribe(
                self._on_event,
                EventFilter(session_id=self.session_id),
            )

    async def remove(self, ws: WebSocket) -> None:
        self.clients.discard(ws)
        if not self.clients and self._sub_id is not None:
            try:
                self.runtime.event_bus.unsubscribe(self._sub_id)
            except Exception:
                pass
            self._sub_id = None

    def _on_event(self, ev) -> None:
        # EventBus calls subscribers synchronously; schedule async sends.
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(asyncio.create_task, self.broadcast({"type": "event", "event": ev.to_dict()}))

    async def broadcast(self, msg: dict[str, Any]) -> None:
        if not self.clients:
            return
        data = _json(msg)
        dead: list[WebSocket] = []
        async with self._send_lock:
            sockets = list(self.clients)

        async def _send_one(ws: WebSocket) -> tuple[WebSocket, Exception | None]:
            try:
                await ws.send_text(data)
                return (ws, None)
            except Exception as e:
                return (ws, e)

        results = await asyncio.gather(*(_send_one(ws) for ws in sockets), return_exceptions=False)
        for ws, err in results:
            if err is not None:
                dead.append(ws)

        if dead:
            async with self._send_lock:
                for ws in dead:
                    self.clients.discard(ws)


def build_app(*, project_root: Path) -> FastAPI:
    runtime = WebRuntime(project_root=project_root)
    hubs: dict[str, SessionHub] = {}

    app = FastAPI(title="Aura Web Surface", version="0.1")
    app.state.runtime = runtime

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, Any]:
        return {
            "project_root": str(runtime.project_root),
            "model_profiles": runtime.list_model_profiles(),
            "sessions": [s.to_dict() for s in runtime.list_sessions()],
        }

    @app.post("/api/sessions")
    def create_session() -> dict[str, Any]:
        sid = runtime.ensure_session(session_id=None)
        return {"session_id": sid, "meta": runtime.get_session_meta(session_id=sid)}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        try:
            runtime.ensure_session(session_id=session_id)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return runtime.get_session_meta(session_id=session_id)

    @app.post("/api/sessions/{session_id}/settings")
    def update_settings(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            runtime.ensure_session(session_id=session_id)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        try:
            runtime.update_session_settings(
                session_id=session_id,
                chat_profile_id=body.get("chat_profile_id"),
                llm_streaming=body.get("llm_streaming"),
                tool_approval_mode=body.get("tool_approval_mode"),
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"ok": True, "meta": runtime.get_session_meta(session_id=session_id)}

    @app.get("/api/sessions/{session_id}/approvals")
    def get_pending_approvals(session_id: str, request_id: str | None = None) -> dict[str, Any]:
        try:
            runtime.ensure_session(session_id=session_id)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"approvals": runtime.list_pending_approvals(session_id=session_id, request_id=request_id)}

    @app.get("/api/validate/{session_id}")
    def validate_session(session_id: str, strict: bool = False) -> dict[str, Any]:
        issues = validate_project_session(project_root=runtime.project_root, session_id=session_id, strict=bool(strict))
        return {"count": len(issues), "issues": [it.render() for it in issues]}

    @app.get("/api/artifacts/{locator:path}")
    def get_artifact(locator: str) -> FileResponse:
        root = runtime.paths.artifacts_dir.resolve()
        path = (runtime.paths.artifacts_dir / locator).resolve()
        if path != root and root not in path.parents:
            raise HTTPException(status_code=400, detail="Invalid artifact locator.")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found.")
        return FileResponse(str(path), media_type="application/octet-stream")

    def _hub(session_id: str) -> SessionHub:
        h = hubs.get(session_id)
        if h is None:
            h = SessionHub(session_id=session_id, runtime=runtime)
            hubs[session_id] = h
        return h

    async def _replay_events(session_id: str, *, since_event_id: str | None, max_events: int) -> list[dict[str, Any]]:
        buf = deque(maxlen=max_events)
        seen_anchor = since_event_id is None
        for ev in runtime.event_log_store.read(session_id):
            if not seen_anchor:
                if ev.event_id == since_event_id:
                    seen_anchor = True
                continue
            buf.append(ev.to_dict())
        return list(buf)

    @app.websocket("/ws/{session_id}")
    async def ws_session(ws: WebSocket, session_id: str) -> None:
        try:
            runtime.ensure_session(session_id=session_id)
        except FileNotFoundError:
            await ws.close(code=1008)
            return

        await ws.accept()
        hub = _hub(session_id)
        await hub.add(ws)

        async def send(msg: dict[str, Any]) -> None:
            await ws.send_text(_json(msg))

        # Initial replay + meta
        replay_cap = int(runtime.config.get("web_ws_replay_cap", 400)) if hasattr(runtime, "config") else int((__import__("os").environ.get("AURA_WEB_WS_REPLAY_CAP") or "400"))
        await send({"type": "session_meta", "meta": runtime.get_session_meta(session_id=session_id)})
        await send({"type": "replay", "events": await _replay_events(session_id, since_event_id=None, max_events=replay_cap)})

        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await send({"type": "error", "message": "Invalid JSON"})
                    continue
                if not isinstance(msg, dict):
                    await send({"type": "error", "message": "Invalid message"})
                    continue

                mtype = str(msg.get("type") or "")
                if mtype == "ping":
                    await send({"type": "pong"})
                    continue

                if mtype == "hello":
                    since_event_id = msg.get("since_event_id") if isinstance(msg.get("since_event_id"), str) else None
                    await send({"type": "replay", "events": await _replay_events(session_id, since_event_id=since_event_id, max_events=replay_cap)})
                    continue

                if mtype in {"chat", "approval"}:
                    async with runtime.lock_for_session(session_id):
                        engine = runtime.engine_for_session(session_id=session_id)
                        if mtype == "chat":
                            text = str(msg.get("text") or "").strip()
                            if not text:
                                await send({"type": "error", "message": "Empty text"})
                                continue
                            op = Op(
                                kind=OpKind.CHAT.value,
                                payload={"text": text},
                                session_id=session_id,
                                request_id=runtime.new_request_id(),
                                timestamp=runtime.now_ts_ms(),
                                turn_id=runtime.new_turn_id(),
                            )
                            await engine.arun(op)
                            await send({"type": "ack", "op": "chat", "request_id": op.request_id, "turn_id": op.turn_id})
                        else:
                            approval_id = str(msg.get("approval_id") or "").strip()
                            decision = str(msg.get("decision") or "").strip().lower()
                            if not approval_id or decision not in {"approve", "deny"}:
                                await send({"type": "error", "message": "approval requires approval_id + decision=approve|deny"})
                                continue
                            op = Op(
                                kind=OpKind.APPROVAL_DECISION.value,
                                payload={"approval_id": approval_id, "decision": decision},
                                session_id=session_id,
                                request_id=runtime.new_request_id(),
                                timestamp=runtime.now_ts_ms(),
                                turn_id=runtime.new_turn_id(),
                            )
                            await engine.arun(op)
                            await send({"type": "ack", "op": "approval", "approval_id": approval_id, "decision": decision})

                        # Ensure mergeable events are flushed so clients get a consistent stream.
                        try:
                            runtime.event_bus.flush(session_id=session_id)
                        except Exception:
                            pass
                    continue

                if mtype == "settings":
                    try:
                        runtime.update_session_settings(
                            session_id=session_id,
                            chat_profile_id=msg.get("chat_profile_id"),
                            llm_streaming=msg.get("llm_streaming"),
                            tool_approval_mode=msg.get("tool_approval_mode"),
                        )
                    except Exception as e:
                        await send({"type": "error", "message": str(e)})
                        continue
                    await hub.broadcast({"type": "session_meta", "meta": runtime.get_session_meta(session_id=session_id)})
                    continue

                if mtype == "list_approvals":
                    await send({"type": "approvals", "approvals": runtime.list_pending_approvals(session_id=session_id, request_id=None)})
                    continue

                await send({"type": "error", "message": f"Unknown type: {mtype}"})
        except WebSocketDisconnect:
            return
        finally:
            await hub.remove(ws)

    return app


def run(*, project_root: Path, host: str, port: int) -> None:
    import uvicorn

    app = build_app(project_root=project_root)
    uvicorn.run(app, host=str(host), port=int(port), log_level="info")
