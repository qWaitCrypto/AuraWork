from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """
    Stable, cross-module error codes used in events and exceptions.

    Values intentionally align with the legacy LLMErrorCode strings so that
    non-LLM layers can reuse the same vocabulary where appropriate.
    """

    # Shared (LLM-compatible) codes
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    AUTH = "auth"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNPROCESSABLE = "unprocessable"
    SERVER_ERROR = "server_error"
    NETWORK_ERROR = "network_error"
    RESPONSE_VALIDATION = "response_validation"
    UNKNOWN = "unknown"

    # Orchestrator / runtime specific codes
    MODEL_RESOLUTION = "model_resolution"
    APPROVAL_PENDING = "approval_pending"
    APPROVAL_DECISION_INVALID = "approval_decision_invalid"
    APPROVAL_NOT_FOUND = "approval_not_found"
    APPROVAL_SESSION_MISMATCH = "approval_session_mismatch"
    APPROVAL_NOT_PENDING = "approval_not_pending"
    APPROVAL_RESUME_INVALID = "approval_resume_invalid"

    TOOL_CALLS_DISABLED = "tool_calls_disabled"
    TOOL_CALL_PLAN_FAILED = "tool_call_plan_failed"
    TOOL_UNKNOWN = "tool_unknown"
    TOOL_DENIED = "tool_denied"
    TOOL_FAILED = "tool_failed"
    TOOL_LOOP_LIMIT = "tool_loop_limit"

    EVENT_LOG_APPEND_FAILED = "event_log_append_failed"
