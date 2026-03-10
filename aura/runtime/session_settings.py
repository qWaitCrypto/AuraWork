from __future__ import annotations

from typing import Any

from .llm import ModelConfig
from .stores import SessionStore
from .tools import ToolApprovalMode


def build_session_settings_patch(
    *,
    model_config: ModelConfig,
    chat_profile_id: str | None = None,
    llm_streaming: bool | None = None,
    tool_approval_mode: str | None = None,
) -> dict[str, Any]:
    patch: dict[str, Any] = {}

    if chat_profile_id is not None:
        profile_id = str(chat_profile_id).strip()
        if profile_id not in model_config.profiles:
            raise ValueError(f"Unknown model profile: {profile_id}")
        patch["chat_profile_id"] = profile_id

    if llm_streaming is not None:
        patch["llm_streaming"] = bool(llm_streaming)

    if tool_approval_mode is not None:
        raw_mode = str(tool_approval_mode).strip().lower()
        try:
            mode = ToolApprovalMode(raw_mode)
        except ValueError as e:
            raise ValueError("tool_approval_mode must be one of: strict, standard, trusted") from e
        patch["tool_approval_mode"] = mode.value

    return patch


def update_session_settings(
    *,
    session_store: SessionStore,
    session_id: str,
    model_config: ModelConfig,
    chat_profile_id: str | None = None,
    llm_streaming: bool | None = None,
    tool_approval_mode: str | None = None,
) -> dict[str, Any]:
    patch = build_session_settings_patch(
        model_config=model_config,
        chat_profile_id=chat_profile_id,
        llm_streaming=llm_streaming,
        tool_approval_mode=tool_approval_mode,
    )
    if patch:
        session_store.update_session(session_id, patch)
    return patch
