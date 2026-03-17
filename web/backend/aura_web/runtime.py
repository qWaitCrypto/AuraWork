from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aura.runtime.api import (
    ApprovalStatus,
    Engine,
    EngineBuildError,
    EventBus,
    FileApprovalStore,
    FileArtifactStore,
    FileEventLogStore,
    FileSessionStore,
    ModelConfig,
    ModelRole,
    RuntimePaths,
    ToolApprovalMode,
    build_engine_for_session,
    load_model_config_layers_for_dir,
    new_id,
    now_ts_ms,
    update_session_settings,
)
from aura.runtime.error_codes import ErrorCode
from aura.runtime.protocol import EVENT_SCHEMA_VERSION, Event, EventKind, Op
from aura.runtime.subagents.approval_manager import ApprovalManager


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    updated_at: int | None
    created_at: int | None
    mode: str | None
    chat_profile_id: str | None
    tool_approval_mode: str | None
    llm_streaming: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "updated_at": self.updated_at,
            "created_at": self.created_at,
            "mode": self.mode,
            "chat_profile_id": self.chat_profile_id,
            "tool_approval_mode": self.tool_approval_mode,
            "llm_streaming": self.llm_streaming,
        }


@dataclass(frozen=True, slots=True)
class ActiveSessionOp:
    session_id: str
    op_kind: str
    request_id: str
    turn_id: str | None
    started_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "op_kind": self.op_kind,
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "started_at": self.started_at,
        }


@dataclass(slots=True)
class _ActiveSessionOpTask:
    info: ActiveSessionOp
    task: asyncio.Task[Any] = field(repr=False)


class WebRuntime:
    """
    Project-scoped runtime for the web surface.

    Mirrors the CLI wiring:
    - RuntimePaths + File* stores
    - EventBus with persistent EventLogStore
    - Build per-session Engine instances from `.aura/` state
    """

    def __init__(self, *, project_root: Path) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.paths = RuntimePaths.for_project(self.project_root)

        self.artifact_store = FileArtifactStore(self.paths.artifacts_dir)
        self.session_store = FileSessionStore(self.paths.sessions_dir)
        self.approval_store = FileApprovalStore(self.paths.state_dir / "approvals")
        self.event_log_store = FileEventLogStore(
            self.paths.events_dir,
            artifact_store=self.artifact_store,
            session_store=self.session_store,
        )
        self.event_bus = EventBus(event_log_store=self.event_log_store)

        # Web surface can run in a "no-project" mode (UI-only) where the repo
        # hasn't been initialized with `.aura/config/models.json` yet.
        # In that case, we allow startup and return empty model profiles/sessions.
        self._model_layers = load_model_config_layers_for_dir(self.project_root, require_project=False)
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._engine_cache: dict[str, Engine] = {}
        self._approval_managers: dict[str, ApprovalManager] = {}
        self._active_ops: dict[str, _ActiveSessionOpTask] = {}

    def lock_for_session(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def active_op_for_session(self, session_id: str) -> ActiveSessionOp | None:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        active = self._active_ops.get(sid)
        if active is None:
            return None
        if active.task.done():
            self._active_ops.pop(sid, None)
            return None
        return active.info

    def start_op_for_session(self, *, session_id: str, op: Op) -> tuple[bool, ActiveSessionOp]:
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id must be non-empty.")

        existing = self._active_ops.get(sid)
        if existing is not None and not existing.task.done():
            return False, existing.info
        if existing is not None and existing.task.done():
            self._active_ops.pop(sid, None)

        info = ActiveSessionOp(
            session_id=sid,
            op_kind=str(op.kind or "").strip() or "unknown",
            request_id=str(op.request_id or "").strip(),
            turn_id=str(op.turn_id or "").strip() or None,
            started_at=self.now_ts_ms(),
        )
        task = asyncio.create_task(self._run_session_op(session_id=sid, op=op, info=info))
        self._active_ops[sid] = _ActiveSessionOpTask(info=info, task=task)
        return True, info

    async def _run_session_op(self, *, session_id: str, op: Op, info: ActiveSessionOp) -> None:
        try:
            async with self.lock_for_session(session_id):
                engine = self.engine_for_session(session_id=session_id)
                await engine.arun(op)
                try:
                    self.event_bus.flush(session_id=session_id)
                except Exception:
                    logger.warning("Failed to flush event bus for session_id=%s", session_id, exc_info=True)
        except asyncio.CancelledError:
            self._emit_runtime_operation_failed(
                session_id=session_id,
                request_id=info.request_id,
                turn_id=info.turn_id,
                op_kind=info.op_kind,
                error_code=ErrorCode.CANCELLED.value,
                error_message="Operation cancelled.",
            )
            raise
        except Exception:
            logger.warning(
                "Background session op crashed for session_id=%s request_id=%s op_kind=%s",
                session_id,
                info.request_id,
                info.op_kind,
                exc_info=True,
            )
            self._emit_runtime_operation_failed(
                session_id=session_id,
                request_id=info.request_id,
                turn_id=info.turn_id,
                op_kind=info.op_kind,
                error_code=ErrorCode.UNKNOWN.value,
                error_message="Operation crashed before completion.",
            )
        finally:
            active = self._active_ops.get(session_id)
            current = asyncio.current_task()
            if active is not None and active.task is current:
                self._active_ops.pop(session_id, None)

    def _emit_runtime_operation_failed(
        self,
        *,
        session_id: str,
        request_id: str | None,
        turn_id: str | None,
        op_kind: str,
        error_code: str,
        error_message: str,
    ) -> None:
        try:
            event = Event(
                kind=EventKind.OPERATION_FAILED.value,
                payload={
                    "op_kind": op_kind,
                    "error": error_message,
                    "error_code": error_code,
                    "source": "web_runtime",
                },
                session_id=session_id,
                event_id=new_id("evt"),
                timestamp=now_ts_ms(),
                sequence=None,
                request_id=request_id,
                turn_id=turn_id,
                step_id=None,
                schema_version=EVENT_SCHEMA_VERSION,
            )
            published = self.event_bus.publish(event)
            try:
                seq = int(published.sequence) if isinstance(published.sequence, int) else None
            except Exception:
                seq = None
            patch: dict[str, Any] = {"last_request_id": request_id, "last_event_id": published.event_id}
            if seq is not None:
                patch["last_event_sequence"] = seq
            self.session_store.update_session(session_id, patch)
        except Exception:
            logger.warning("Failed to emit runtime operation failure for session_id=%s", session_id, exc_info=True)

    def now_ts_ms(self) -> int:
        return now_ts_ms()

    def new_request_id(self) -> str:
        return new_id("req")

    def new_turn_id(self) -> str:
        return new_id("turn")

    def model_config(self) -> ModelConfig:
        return self._model_layers.merged()

    def list_model_profiles(self) -> list[dict[str, Any]]:
        cfg = self.model_config()
        out: list[dict[str, Any]] = []
        for pid in sorted(cfg.profiles):
            p = cfg.profiles[pid]
            caps = p.capabilities.with_provider_defaults(p.provider_kind)
            out.append(
                {
                    "profile_id": pid,
                    "provider_kind": p.provider_kind.value,
                    "model": p.model_name,
                    "supports_tools": bool(caps.supports_tools),
                    "supports_streaming": bool(caps.supports_streaming),
                }
            )
        return out

    def ensure_session(self, *, session_id: str | None) -> str:
        if session_id is not None:
            sid = str(session_id).strip()
            if not sid:
                raise ValueError("session_id must be non-empty.")
            _ = self.session_store.get_session(sid)
            return sid

        cfg = self.model_config()
        profile = cfg.get_profile_for_role(ModelRole.MAIN)
        default_profile_id = profile.profile_id if profile is not None else None
        return self.session_store.create_session(
            {
                "project_ref": str(self.project_root),
                "mode": "chat",
                "tool_approval_mode": ToolApprovalMode.STANDARD.value,
                "llm_streaming": True,
                "chat_profile_id": default_profile_id,
            }
        )

    def list_sessions(self) -> list[SessionSummary]:
        out: list[SessionSummary] = []
        for meta in self.session_store.list_sessions():
            sid = meta.get("session_id")
            if not isinstance(sid, str) or not sid.strip():
                continue
            out.append(
                SessionSummary(
                    session_id=sid.strip(),
                    updated_at=meta.get("updated_at") if isinstance(meta.get("updated_at"), int) else None,
                    created_at=meta.get("created_at") if isinstance(meta.get("created_at"), int) else None,
                    mode=meta.get("mode") if isinstance(meta.get("mode"), str) else None,
                    chat_profile_id=meta.get("chat_profile_id") if isinstance(meta.get("chat_profile_id"), str) else None,
                    tool_approval_mode=meta.get("tool_approval_mode") if isinstance(meta.get("tool_approval_mode"), str) else None,
                    llm_streaming=meta.get("llm_streaming") if isinstance(meta.get("llm_streaming"), bool) else None,
                )
            )
        return out

    def get_session_meta(self, *, session_id: str) -> dict[str, Any]:
        return self.session_store.get_session(session_id)

    def delete_session(self, *, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id must be non-empty.")

        # Ensure it exists first (consistent error behavior).
        _ = self.session_store.get_session(sid)

        # Drop any cached engine/locks first.
        active = self._active_ops.pop(sid, None)
        if active is not None:
            active.task.cancel()
        self._engine_cache.pop(sid, None)
        self._session_locks.pop(sid, None)
        manager = self._approval_managers.pop(sid, None)
        if manager is not None:
            manager.deny_all()

        # Remove session meta + event log (main persistence).
        try:
            (self.paths.sessions_dir / f"{sid}.json").unlink(missing_ok=True)
        except Exception:
            logger.warning("Failed to delete session metadata for session_id=%s", sid, exc_info=True)
        try:
            (self.paths.events_dir / f"{sid}.jsonl").unlink(missing_ok=True)
        except Exception:
            logger.warning("Failed to delete session event log for session_id=%s", sid, exc_info=True)

        # Remove pending approvals for this session (best-effort).
        approvals_dir = (self.paths.state_dir / "approvals").resolve()
        if approvals_dir.is_dir():
            for p in approvals_dir.glob("*.json"):
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    logger.warning("Failed to parse approval record while deleting session: %s", p, exc_info=True)
                    continue
                if isinstance(raw, dict) and str(raw.get("session_id") or "") == sid:
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        logger.warning("Failed to delete approval file: %s", p, exc_info=True)

        # Remove agent-browser state files (best-effort).
        try:
            from aura.runtime.agent_browser import agent_browser_stream_port_file

            port_file = agent_browser_stream_port_file(self.project_root, aura_session_id=sid)
            try:
                port_file.unlink(missing_ok=True)
            except Exception:
                logger.warning("Failed to delete browser stream port file for session_id=%s", sid, exc_info=True)
            # The daemon also writes a per-session `.stream` file under its socket dir, but that's not
            # workspace-local and is handled by the daemon lifecycle.
        except Exception:
            logger.warning("Failed to clean browser state for session_id=%s", sid, exc_info=True)

    def update_session_settings(
        self,
        *,
        session_id: str,
        chat_profile_id: str | None = None,
        llm_streaming: bool | None = None,
        tool_approval_mode: str | None = None,
    ) -> None:
        patch = update_session_settings(
            session_store=self.session_store,
            session_id=session_id,
            model_config=self.model_config(),
            chat_profile_id=chat_profile_id,
            llm_streaming=llm_streaming,
            tool_approval_mode=tool_approval_mode,
        )
        if patch:
            self._engine_cache.pop(session_id, None)

    def list_pending_approvals(self, *, session_id: str, request_id: str | None = None) -> list[dict[str, Any]]:
        recs = self.approval_store.list(session_id=session_id, status=ApprovalStatus.PENDING, request_id=request_id)
        merged: list[dict[str, Any]] = [r.to_dict() for r in recs]

        manager = self.approval_manager_for_session(session_id)
        pending_live = manager.list_pending()
        if pending_live:
            seen_ids: set[str] = set()
            for row in merged:
                aid = row.get("approval_id")
                if isinstance(aid, str) and aid.strip():
                    seen_ids.add(aid.strip())
            for row in pending_live:
                aid = row.get("approval_id")
                if isinstance(aid, str) and aid.strip() and aid.strip() in seen_ids:
                    continue
                aid_str = str(aid or "").strip()
                if not aid_str:
                    continue
                merged.append(
                    {
                        "approval_id": aid_str,
                        "session_id": session_id,
                        "request_id": request_id,
                        "created_at": self.now_ts_ms(),
                        "status": "pending",
                        "turn_id": None,
                        "action_summary": row.get("action_summary"),
                        "risk_level": row.get("risk_level"),
                        "options": ["approve", "deny"],
                        "reason": row.get("reason"),
                        "diff_ref": row.get("diff_ref") if isinstance(row.get("diff_ref"), dict) else None,
                        "resume_kind": None,
                        "resume_payload": None,
                        "decision": None,
                    }
                )

        return merged

    def approval_manager_for_session(self, session_id: str) -> ApprovalManager:
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id must be non-empty.")
        manager = self._approval_managers.get(sid)
        if manager is None:
            manager = ApprovalManager()
            self._approval_managers[sid] = manager
        return manager

    def _build_engine(self, *, session_id: str) -> Engine:
        meta = self.session_store.get_session(session_id)
        layers = self._model_layers

        chat_profile_id = meta.get("chat_profile_id")
        if isinstance(chat_profile_id, str) and chat_profile_id.strip():
            layers = layers.__class__(
                global_config=layers.global_config,
                project_config=layers.project_config,
                session_config=ModelConfig(role_pointers={ModelRole.MAIN: chat_profile_id.strip()}),
                op_config=None,
            )

        model_config = layers.merged()

        enable_tools: bool
        profile = model_config.get_profile_for_role(ModelRole.MAIN)
        if profile is None:
            enable_tools = False
        else:
            caps = profile.capabilities.with_provider_defaults(profile.provider_kind)
            enable_tools = caps.supports_tools is True

        try:
            orchestrator = build_engine_for_session(
                project_root=self.project_root,
                session_id=session_id,
                event_bus=self.event_bus,
                session_store=self.session_store,
                event_log_store=self.event_log_store,
                artifact_store=self.artifact_store,
                approval_store=self.approval_store,
                model_config=model_config,
                system_prompt=None,
                tools_enabled=enable_tools,
                max_tool_turns=30,
                approval_manager=self.approval_manager_for_session(session_id),
            )
        except EngineBuildError as e:
            raise RuntimeError(str(e)) from e

        raw_mode = meta.get("tool_approval_mode")
        try:
            mode = ToolApprovalMode(str(raw_mode)) if raw_mode else ToolApprovalMode.STANDARD
        except ValueError:
            mode = ToolApprovalMode.STANDARD
        if orchestrator.tool_runtime is not None:
            orchestrator.tool_runtime.set_approval_mode(mode)

        raw_streaming = meta.get("llm_streaming")
        orchestrator.set_llm_streaming(raw_streaming if isinstance(raw_streaming, bool) else True)

        orchestrator.load_history_from_events()
        orchestrator.apply_memory_summary_retention()
        return orchestrator

    def engine_for_session(self, *, session_id: str) -> Engine:
        eng = self._engine_cache.get(session_id)
        if eng is None:
            eng = self._build_engine(session_id=session_id)
            self._engine_cache[session_id] = eng
        return eng

    async def warm_engine_for_session(self, *, session_id: str) -> None:
        """Pre-build the engine in the background on session connect."""
        try:
            loop = asyncio.get_running_loop()
            async with self.lock_for_session(session_id):
                await loop.run_in_executor(
                    None,
                    lambda: self.engine_for_session(session_id=session_id),
                )
        except Exception:
            logger.debug("Engine pre-warm failed for session %s", session_id, exc_info=True)
