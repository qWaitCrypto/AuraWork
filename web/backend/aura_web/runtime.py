from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aura.runtime.approval import ApprovalStatus
from aura.runtime.engine import Engine, EngineBuildError, build_engine_for_session
from aura.runtime.event_bus import EventBus
from aura.runtime.ids import new_id, now_ts_ms
from aura.runtime.llm.config import ModelConfig
from aura.runtime.llm.config_io import load_model_config_layers_for_dir
from aura.runtime.llm.types import ModelRole
from aura.runtime.project import RuntimePaths
from aura.runtime.stores import FileApprovalStore, FileArtifactStore, FileEventLogStore, FileSessionStore
from aura.runtime.tools.runtime import ToolApprovalMode


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

    def lock_for_session(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

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

    def update_session_settings(
        self,
        *,
        session_id: str,
        chat_profile_id: str | None = None,
        llm_streaming: bool | None = None,
        tool_approval_mode: str | None = None,
    ) -> None:
        patch: dict[str, Any] = {}

        if chat_profile_id is not None:
            pid = str(chat_profile_id).strip()
            cfg = self.model_config()
            if pid not in cfg.profiles:
                raise ValueError(f"Unknown model profile: {pid}")
            patch["chat_profile_id"] = pid

        if llm_streaming is not None:
            patch["llm_streaming"] = bool(llm_streaming)

        if tool_approval_mode is not None:
            raw = str(tool_approval_mode).strip().lower()
            try:
                mode = ToolApprovalMode(raw)
            except ValueError as e:
                raise ValueError("tool_approval_mode must be one of: strict, standard, trusted") from e
            patch["tool_approval_mode"] = mode.value

        if patch:
            self.session_store.update_session(session_id, patch)
            self._engine_cache.pop(session_id, None)

    def list_pending_approvals(self, *, session_id: str, request_id: str | None = None) -> list[dict[str, Any]]:
        recs = self.approval_store.list(session_id=session_id, status=ApprovalStatus.PENDING, request_id=request_id)
        return [r.to_dict() for r in recs]

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

