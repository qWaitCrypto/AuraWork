from __future__ import annotations

import asyncio
import json
import os
import mimetypes
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from aura.runtime.event_bus import EventFilter
from aura.runtime.protocol import Op, OpKind
from aura.runtime.validate import validate_project_session
from aura.runtime.agent_browser import (
    agent_browser_daemon_socket_dir,
    agent_browser_daemon_stream_file_for_session,
    agent_browser_session_for_aura_session,
    agent_browser_socket_dir_for_project,
    agent_browser_stream_port_file_for_session,
)

from .runtime import WebRuntime
from .runtime_manager import RuntimeManager
from .session_index import SessionIndex
from .workspace_registry import WorkspaceRegistry




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
        loop.call_soon_threadsafe(
            asyncio.create_task,
            self.broadcast({"type": "event", "event": ev.to_dict()}),
        )

    async def broadcast(self, msg: dict[str, Any]) -> None:
        if not self.clients:
            return
        data = _json(msg)
        # IMPORTANT: Keep sends serialized per session to preserve event ordering
        # and avoid concurrent writes to the same WebSocket.
        async with self._send_lock:
            timeout_s = 0.0
            try:
                timeout_s = float(str(os.environ.get("AURA_WEB_WS_SEND_TIMEOUT_S") or "0.8"))
            except Exception:
                timeout_s = 0.8
            if timeout_s < 0:
                timeout_s = 0.0

            async def _send_one(ws: WebSocket) -> Exception | None:
                try:
                    if timeout_s > 0:
                        await asyncio.wait_for(ws.send_text(data), timeout=timeout_s)
                    else:
                        await ws.send_text(data)
                    return None
                except Exception as e:
                    return e

            clients = list(self.clients)
            if not clients:
                return
            results = await asyncio.gather(*[_send_one(ws) for ws in clients])
            for ws, err in zip(clients, results):
                if err is not None:
                    self.clients.discard(ws)


def build_app(*, project_root: Path) -> FastAPI:
    # `project_root` is no longer a single workspace root. We keep the parameter
    # for backwards compatibility with the existing web entrypoint.
    registry = WorkspaceRegistry()
    session_index = SessionIndex(state_dir=registry.state_dir)
    runtime_manager = RuntimeManager(registry=registry)

    hubs: dict[str, SessionHub] = {}

    app = FastAPI(title="Aura Web Surface", version="0.2")
    app.state.workspace_registry = registry
    app.state.session_index = session_index
    app.state.runtime_manager = runtime_manager

    # Web surface defaults to enabling agent-browser WebSocket streaming.
    # This is used by `browser__run` to inject AGENT_BROWSER_STREAM_PORT.
    os.environ.setdefault("AURA_ENABLE_BROWSER_STREAMING", "1")

    if os.environ.get("AURA_DESKTOP") == "1":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["tauri://localhost", "https://tauri.localhost"],
            allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=True,
        )

    def _runtime_for_session(session_id: str) -> tuple[WebRuntime, str]:
        sid = str(session_id or "").strip()
        if not sid:
            raise HTTPException(status_code=400, detail="session_id must be non-empty")

        wid = session_index.get_workspace_id(sid)

        # Backfill index for sessions that existed before the workspace-aware web server.
        if not wid:
            for ws in registry.list_workspaces():
                try:
                    pr = Path(ws.project_root).expanduser().resolve()
                except Exception:
                    continue
                sess_path = pr / ".aura" / "sessions" / f"{sid}.json"
                if sess_path.is_file():
                    try:
                        session_index.set(session_id=sid, workspace_id=ws.workspace_id)
                    except Exception:
                        pass
                    wid = ws.workspace_id
                    break

        if not wid:
            raise HTTPException(status_code=404, detail="Unknown session_id")

        try:
            rt = runtime_manager.runtime_for_workspace(workspace_id=wid)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown workspace_id")
        return rt, wid

    _WORKSPACE_IGNORE_DIRS = {
        ".aura",
        ".aura_web",
        ".git",
        ".idea",
        ".vscode",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
    }
    _extra_ignores = os.environ.get("AURA_WORKSPACE_IGNORE_DIRS")
    if isinstance(_extra_ignores, str) and _extra_ignores.strip():
        for part in _extra_ignores.replace(";", ",").split(","):
            name = part.strip()
            if name:
                _WORKSPACE_IGNORE_DIRS.add(name)

    def _resolve_workspace_path(*, root: Path, rel: str) -> Path:
        raw = str(rel or "").strip().replace("\\", "/")
        raw = raw.lstrip("/")
        p = (root / raw).resolve()
        root = root.resolve()
        if p != root and root not in p.parents:
            raise HTTPException(status_code=400, detail="Invalid workspace path.")
        return p

    def _is_ignored_workspace_rel(rel: Path) -> bool:
        for part in rel.parts:
            if part in _WORKSPACE_IGNORE_DIRS:
                return True
        return False

    @app.get("/api/workspaces")
    def list_workspaces() -> dict[str, Any]:
        return {"workspaces": [w.to_dict() for w in registry.list_workspaces()]}

    @app.post("/api/workspaces")
    def register_workspace(body: dict[str, Any]) -> dict[str, Any]:
        raw = body.get("project_root")
        if not isinstance(raw, str) or not raw.strip():
            raise HTTPException(status_code=400, detail="project_root is required")
        try:
            rec = registry.register(project_root=raw)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return rec.to_dict()

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, Any]:
        workspaces = registry.list_workspaces()

        sessions: list[dict[str, Any]] = []
        for ws in workspaces:
            try:
                rt = runtime_manager.runtime_for_workspace(workspace_id=ws.workspace_id)
            except Exception:
                continue
            for s in rt.list_sessions():
                sessions.append(
                    {
                        **s.to_dict(),
                        "workspace_id": ws.workspace_id,
                        "project_root": ws.project_root,
                    }
                )

        sessions.sort(key=lambda r: int(r.get("updated_at") or 0), reverse=True)

        model_profiles: list[dict[str, Any]] = []
        if workspaces:
            try:
                model_profiles = runtime_manager.runtime_for_workspace(workspace_id=workspaces[0].workspace_id).list_model_profiles()
            except Exception:
                model_profiles = []

        legacy_root = workspaces[0].project_root if workspaces else ""
        return {
            "workspaces": [w.to_dict() for w in workspaces],
            "sessions": sessions,
            "model_profiles": model_profiles,
            # Legacy field (kept for older UIs).
            "project_root": str(legacy_root),
        }

    @app.post("/api/sessions")
    def create_session(body: dict[str, Any]) -> dict[str, Any]:
        wid = body.get("workspace_id")
        if not isinstance(wid, str) or not wid.strip():
            raise HTTPException(status_code=400, detail="workspace_id is required")
        wid = wid.strip()

        try:
            rt = runtime_manager.runtime_for_workspace(workspace_id=wid)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown workspace_id")

        # Create a new session in this workspace.
        sid = rt.ensure_session(session_id=None)

        # Ensure global uniqueness at the index layer.
        if session_index.has(sid):
            # Extremely unlikely (time_ns + uuid), but keep behavior deterministic.
            sid = rt.ensure_session(session_id=None)

        session_index.set(session_id=sid, workspace_id=wid)
        registry.touch(wid)

        return {
            "session_id": sid,
            "workspace_id": wid,
            "project_root": str(rt.project_root),
            "meta": rt.get_session_meta(session_id=sid),
        }

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        rt, _wid = _runtime_for_session(session_id)
        try:
            rt.ensure_session(session_id=session_id)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return rt.get_session_meta(session_id=session_id)

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str) -> dict[str, Any]:
        rt, _wid = _runtime_for_session(session_id)
        try:
            rt.delete_session(session_id=session_id)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

        try:
            session_index.delete(session_id)
        except Exception:
            pass
        try:
            hubs.pop(str(session_id), None)
        except Exception:
            pass
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/workspace/files")
    def list_workspace_files(
        session_id: str,
        dir: str | None = None,
        limit: int = 200,
        show_hidden: bool = False,
    ) -> dict[str, Any]:
        rt, _wid = _runtime_for_session(session_id)
        root = rt.project_root.resolve()
        rel_dir = str(dir or "").strip()
        if limit < 1:
            limit = 1
        if limit > 5000:
            limit = 5000

        abs_dir = _resolve_workspace_path(root=root, rel=rel_dir)
        if not abs_dir.is_dir():
            raise HTTPException(status_code=404, detail="Directory not found.")

        rel_dir_norm = abs_dir.relative_to(root).as_posix() if abs_dir != root else ""
        if rel_dir_norm and _is_ignored_workspace_rel(Path(rel_dir_norm)):
            raise HTTPException(status_code=404, detail="Directory not found.")

        entries: list[dict[str, Any]] = []
        try:
            for item in abs_dir.iterdir():
                name = item.name
                if not name:
                    continue
                if not show_hidden and name.startswith("."):
                    continue
                if item.is_symlink():
                    continue
                rel_path = item.relative_to(root)
                if _is_ignored_workspace_rel(rel_path):
                    continue
                try:
                    st = item.stat()
                except Exception:
                    continue
                is_dir = item.is_dir()
                entries.append(
                    {
                        "path": rel_path.as_posix(),
                        "name": name,
                        "is_dir": bool(is_dir),
                        "size_bytes": (None if is_dir else int(st.st_size)),
                        "modified_at": int(st.st_mtime * 1000),
                    }
                )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

        entries.sort(key=lambda r: (-(int(r.get("modified_at") or 0)), str(r.get("name") or "")))
        return {"dir": rel_dir_norm, "entries": entries[:limit]}

    @app.get("/api/sessions/{session_id}/workspace/file_text")
    def get_workspace_file_text(session_id: str, path: str, max_bytes: int = 200_000) -> dict[str, Any]:
        rt, _wid = _runtime_for_session(session_id)
        root = rt.project_root.resolve()
        rel = str(path or "").strip()
        if not rel:
            raise HTTPException(status_code=400, detail="path is required")
        if max_bytes < 1:
            max_bytes = 1
        if max_bytes > 2_000_000:
            max_bytes = 2_000_000

        abs_path = _resolve_workspace_path(root=root, rel=rel)
        if abs_path.is_symlink():
            raise HTTPException(status_code=400, detail="Symlinks are not supported.")
        if not abs_path.is_file():
            raise HTTPException(status_code=404, detail="File not found.")
        rel_norm = abs_path.relative_to(root).as_posix()
        if _is_ignored_workspace_rel(Path(rel_norm)):
            raise HTTPException(status_code=404, detail="File not found.")

        try:
            data = abs_path.read_bytes()
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

        truncated = False
        if len(data) > max_bytes:
            data = data[:max_bytes]
            truncated = True
        text = data.decode("utf-8", errors="replace")
        return {"path": rel_norm, "text": text, "bytes": len(data), "truncated": truncated}

    @app.get("/api/sessions/{session_id}/workspace/file/{path:path}")
    def download_workspace_file(session_id: str, path: str) -> FileResponse:
        rt, _wid = _runtime_for_session(session_id)
        root = rt.project_root.resolve()
        rel = str(path or "").strip()
        if not rel:
            raise HTTPException(status_code=400, detail="path is required")

        abs_path = _resolve_workspace_path(root=root, rel=rel)
        if abs_path.is_symlink():
            raise HTTPException(status_code=400, detail="Symlinks are not supported.")
        if not abs_path.is_file():
            raise HTTPException(status_code=404, detail="File not found.")
        rel_norm = abs_path.relative_to(root).as_posix()
        if _is_ignored_workspace_rel(Path(rel_norm)):
            raise HTTPException(status_code=404, detail="File not found.")

        media_type, _enc = mimetypes.guess_type(str(abs_path))
        return FileResponse(
            str(abs_path),
            media_type=media_type or "application/octet-stream",
            filename=abs_path.name,
        )

    @app.post("/api/sessions/{session_id}/settings")
    def update_settings(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        rt, _wid = _runtime_for_session(session_id)
        try:
            rt.ensure_session(session_id=session_id)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        try:
            rt.update_session_settings(
                session_id=session_id,
                chat_profile_id=body.get("chat_profile_id"),
                llm_streaming=body.get("llm_streaming"),
                tool_approval_mode=body.get("tool_approval_mode"),
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"ok": True, "meta": rt.get_session_meta(session_id=session_id)}

    @app.get("/api/sessions/{session_id}/approvals")
    def get_pending_approvals(session_id: str, request_id: str | None = None) -> dict[str, Any]:
        rt, _wid = _runtime_for_session(session_id)
        try:
            rt.ensure_session(session_id=session_id)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"approvals": rt.list_pending_approvals(session_id=session_id, request_id=request_id)}

    @app.get("/api/validate/{session_id}")
    def validate_session(session_id: str, strict: bool = False) -> dict[str, Any]:
        rt, _wid = _runtime_for_session(session_id)
        issues = validate_project_session(project_root=rt.project_root, session_id=session_id, strict=bool(strict))
        return {"count": len(issues), "issues": [it.render() for it in issues]}

    @app.get("/api/sessions/{session_id}/artifacts/{locator:path}")
    def get_session_artifact(session_id: str, locator: str) -> FileResponse:
        rt, _wid = _runtime_for_session(session_id)

        root = rt.paths.artifacts_dir.resolve()
        path = (rt.paths.artifacts_dir / locator).resolve()
        if path != root and root not in path.parents:
            raise HTTPException(status_code=400, detail="Invalid artifact locator.")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found.")
        return FileResponse(str(path), media_type="application/octet-stream")

    @app.get("/api/artifacts/{locator:path}")
    def get_artifact_legacy(locator: str) -> FileResponse:
        raise HTTPException(status_code=410, detail="Use /api/sessions/{session_id}/artifacts/{locator}")

    def _hub(session_id: str, runtime: WebRuntime) -> SessionHub:
        h = hubs.get(session_id)
        if h is None:
            h = SessionHub(session_id=session_id, runtime=runtime)
            hubs[session_id] = h
        return h

    async def _replay_events(
        runtime: WebRuntime,
        session_id: str,
        *,
        since_event_id: str | None,
        max_events: int,
    ) -> list[dict[str, Any]]:
        buf = deque(maxlen=max_events)
        seen_anchor = since_event_id is None
        for ev in runtime.event_log_store.read(session_id):
            if not seen_anchor:
                if ev.event_id == since_event_id:
                    seen_anchor = True
                continue
            buf.append(ev.to_dict())
        return list(buf)

    def _read_int_file(p: Path) -> int | None:
        try:
            raw = p.read_text(encoding="utf-8").strip()
            v = int(raw)
            if 1 <= v <= 65535:
                return v
        except Exception:
            return None
        return None

    _AGENT_SESSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{1,120}$")

    def _resolve_agent_session(*, session_id: str, agent_session: str | None) -> str:
        requested = str(agent_session or "").strip()
        if requested and _AGENT_SESSION_TOKEN_RE.match(requested):
            return requested
        return agent_browser_session_for_aura_session(session_id)

    def _browser_stream_port(*, project_root: Path, session_id: str, agent_session: str | None = None) -> int | None:
        # Prefer daemon-written file (source of truth), then Aura's allocated port file.
        resolved = _resolve_agent_session(session_id=session_id, agent_session=agent_session)
        p1 = agent_browser_daemon_stream_file_for_session(agent_session=resolved)
        p2 = agent_browser_stream_port_file_for_session(project_root, agent_session=resolved)
        return _read_int_file(p1) or _read_int_file(p2)

    @app.get("/api/sessions/{session_id}/browser/stream")
    def get_browser_stream(session_id: str, agent_session: str | None = None) -> dict[str, Any]:
        rt, _wid = _runtime_for_session(session_id)
        try:
            rt.ensure_session(session_id=session_id)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

        resolved_agent_session = _resolve_agent_session(session_id=session_id, agent_session=agent_session)
        aura_state_dir = agent_browser_socket_dir_for_project(rt.project_root)
        daemon_socket_dir = agent_browser_daemon_socket_dir()
        daemon_stream_file = agent_browser_daemon_stream_file_for_session(agent_session=resolved_agent_session)
        aura_port_file = agent_browser_stream_port_file_for_session(rt.project_root, agent_session=resolved_agent_session)
        port = _browser_stream_port(project_root=rt.project_root, session_id=session_id, agent_session=resolved_agent_session)
        return {
            "enabled": os.environ.get("AURA_ENABLE_BROWSER_STREAMING") == "1",
            "agent_session": resolved_agent_session,
            "aura_state_dir": str(aura_state_dir),
            "daemon_socket_dir": str(daemon_socket_dir),
            "daemon_stream_file": str(daemon_stream_file),
            "aura_port_file": str(aura_port_file),
            "port": port,
            "ws_upstream": (f"ws://127.0.0.1:{port}" if port else None),
        }

    @app.websocket("/ws/{session_id}")
    async def ws_session(ws: WebSocket, session_id: str) -> None:
        try:
            rt, _wid = _runtime_for_session(session_id)
        except HTTPException:
            await ws.close(code=1008)
            return

        try:
            rt.ensure_session(session_id=session_id)
        except FileNotFoundError:
            await ws.close(code=1008)
            return

        await ws.accept()
        hub = _hub(session_id, rt)
        await hub.add(ws)

        async def send(msg: dict[str, Any]) -> bool:
            try:
                await ws.send_text(_json(msg))
                return True
            except Exception:
                return False

        replay_cap = (
            int(rt.config.get("web_ws_replay_cap", 400))
            if hasattr(rt, "config")
            else int((__import__("os").environ.get("AURA_WEB_WS_REPLAY_CAP") or "400"))
        )

        await send({"type": "session_meta", "meta": rt.get_session_meta(session_id=session_id)})
        await send({"type": "replay", "events": await _replay_events(rt, session_id, since_event_id=None, max_events=replay_cap)})

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
                    await send({"type": "replay", "events": await _replay_events(rt, session_id, since_event_id=since_event_id, max_events=replay_cap)})
                    continue

                if mtype in {"chat", "approval"}:
                    async with rt.lock_for_session(session_id):
                        engine = rt.engine_for_session(session_id=session_id)
                        if mtype == "chat":
                            text = str(msg.get("text") or "").strip()
                            if not text:
                                await send({"type": "error", "message": "Empty text"})
                                continue
                            op = Op(
                                kind=OpKind.CHAT.value,
                                payload={"text": text},
                                session_id=session_id,
                                request_id=rt.new_request_id(),
                                timestamp=rt.now_ts_ms(),
                                turn_id=rt.new_turn_id(),
                            )
                            await engine.arun(op)
                            if not (await send({"type": "ack", "op": "chat", "request_id": op.request_id, "turn_id": op.turn_id})):
                                break
                        else:
                            approval_id = str(msg.get("approval_id") or "").strip()
                            decision = str(msg.get("decision") or "").strip().lower()
                            if not approval_id or decision not in {"approve", "deny"}:
                                await send({"type": "error", "message": "approval requires approval_id + decision=approve|deny"})
                                continue
                            payload: dict[str, Any] = {"approval_id": approval_id, "decision": decision}
                            note = msg.get("note")
                            if isinstance(note, str) and note.strip():
                                payload["note"] = note.strip()
                            op = Op(
                                kind=OpKind.APPROVAL_DECISION.value,
                                payload=payload,
                                session_id=session_id,
                                request_id=rt.new_request_id(),
                                timestamp=rt.now_ts_ms(),
                                turn_id=rt.new_turn_id(),
                            )
                            await engine.arun(op)
                            if not (await send({"type": "ack", "op": "approval", "approval_id": approval_id, "decision": decision})):
                                break

                        try:
                            rt.event_bus.flush(session_id=session_id)
                        except Exception:
                            pass
                    continue

                if mtype == "settings":
                    try:
                        rt.update_session_settings(
                            session_id=session_id,
                            chat_profile_id=msg.get("chat_profile_id"),
                            llm_streaming=msg.get("llm_streaming"),
                            tool_approval_mode=msg.get("tool_approval_mode"),
                        )
                    except Exception as e:
                        await send({"type": "error", "message": str(e)})
                        continue
                    await hub.broadcast({"type": "session_meta", "meta": rt.get_session_meta(session_id=session_id)})
                    continue

                if mtype == "list_approvals":
                    await send({"type": "approvals", "approvals": rt.list_pending_approvals(session_id=session_id, request_id=None)})
                    continue

                await send({"type": "error", "message": f"Unknown type: {mtype}"})
        except WebSocketDisconnect:
            return
        finally:
            await hub.remove(ws)

    @app.websocket("/ws/{session_id}/browser")
    async def ws_browser_stream(ws: WebSocket, session_id: str) -> None:
        try:
            rt, _wid = _runtime_for_session(session_id)
        except HTTPException:
            await ws.close(code=1008)
            return

        try:
            rt.ensure_session(session_id=session_id)
        except FileNotFoundError:
            await ws.close(code=1008)
            return

        requested_agent_session = ws.query_params.get("agent_session") if ws.query_params is not None else None
        stream_agent_session = _resolve_agent_session(session_id=session_id, agent_session=requested_agent_session)

        await ws.accept()

        async def send(obj: dict[str, Any]) -> bool:
            try:
                await ws.send_text(_json(obj))
                return True
            except Exception:
                return False

        try:
            import websockets
        except Exception:
            await send({"type": "error", "message": "websockets dependency missing on server"})
            await ws.close(code=1011)
            return

        last_status: tuple[bool, int | None] | None = None

        async def wait_for_port() -> int | None:
            # Wait until either the stream port is known (browser__run invoked) or the client disconnects.
            while True:
                port = _browser_stream_port(project_root=rt.project_root, session_id=session_id, agent_session=stream_agent_session)
                if port:
                    return port
                # Keep the connection responsive to pings while waiting.
                try:
                    raw = await asyncio.wait_for(ws.receive_text(), timeout=1.0)
                except asyncio.TimeoutError:
                    raw = None
                except WebSocketDisconnect:
                    return None
                if raw:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        msg = None
                    if isinstance(msg, dict) and str(msg.get("type") or "") == "ping":
                        if not (await send({"type": "pong"})):
                            return None

        try:
            while True:
                port = await wait_for_port()
                if port is None:
                    return

                status = (True, port)
                if status != last_status:
                    last_status = status
                    await send(
                        {
                            "type": "status",
                            "connected": True,
                            "screencasting": True,
                            "port": port,
                            "agent_session": stream_agent_session,
                        }
                    )

                upstream = f"ws://127.0.0.1:{port}"
                try:
                    async with websockets.connect(upstream, max_size=50 * 1024 * 1024) as upstream_ws:
                        async def upstream_to_client() -> None:
                            async for msg in upstream_ws:
                                if isinstance(msg, (bytes, bytearray)):
                                    continue
                                text = str(msg)
                                # agent-browser may accept the WS connection before the browser is launched.
                                # In that case it sends "Browser not launched" and won't auto-retry screencast
                                # for the existing client. Trigger a reconnect loop in our proxy so the UI
                                # starts receiving frames once the browser is ready.
                                parsed: dict[str, Any] | None = None
                                try:
                                    obj = json.loads(text)
                                    if isinstance(obj, dict):
                                        parsed = obj
                                except json.JSONDecodeError:
                                    parsed = None

                                if (
                                    isinstance(parsed, dict)
                                    and parsed.get("type") == "error"
                                    and "browser not launched" in str(parsed.get("message") or "").lower()
                                ):
                                    # Treat "Browser not launched" as a non-fatal state rather than an error.
                                    # agent-browser won't start screencasting for an already-connected client once
                                    # the browser launches, so we still trigger our reconnect loop — but we surface
                                    # it as a status update so the UI doesn't show a scary error banner.
                                    await send(
                                        {
                                            "type": "status",
                                            "connected": True,
                                            "screencasting": False,
                                            "port": port,
                                            "agent_session": stream_agent_session,
                                        }
                                    )
                                    raise RuntimeError("agent_browser_not_launched")
                                await ws.send_text(text)

                        async def client_to_upstream() -> None:
                            while True:
                                raw = await ws.receive_text()
                                # Allow a lightweight keepalive.
                                if raw == "ping":
                                    await send({"type": "pong"})
                                    continue
                                try:
                                    msg = json.loads(raw)
                                except Exception:
                                    msg = None
                                if isinstance(msg, dict) and str(msg.get("type") or "") == "ping":
                                    await send({"type": "pong"})
                                    continue
                                await upstream_ws.send(raw)

                        t1 = asyncio.create_task(upstream_to_client())
                        t2 = asyncio.create_task(client_to_upstream())
                        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_EXCEPTION)
                        for t in pending:
                            t.cancel()
                        for t in done:
                            exc = t.exception()
                            if exc is not None:
                                raise exc
                except WebSocketDisconnect:
                    return
                except Exception as e:
                    # Upstream not ready yet or crashed. Retry.
                    if str(e) == "agent_browser_not_launched":
                        await asyncio.sleep(0.5)
                        continue
                    await send({"type": "error", "message": f"browser_stream_connect_failed: {e}"})
                    await asyncio.sleep(1.0)
                    continue
        finally:
            return

    return app


def run(*, project_root: Path, host: str, port: int) -> None:
    import uvicorn

    app = build_app(project_root=project_root)
    uvicorn.run(app, host=str(host), port=int(port), log_level="info")
