from __future__ import annotations

TOOL_END_STATUS_MAP: dict[str, str] = {
    "ok": "succeeded",
    "success": "succeeded",
    "succeeded": "succeeded",
    "completed": "succeeded",
    "done": "succeeded",
    "error": "failed",
    "failed": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "denied": "blocked",
    "blocked": "blocked",
    "needs_approval": "needs_approval",
    "require_approval": "needs_approval",
    "requires_approval": "needs_approval",
    "pending_approval": "needs_approval",
    "running": "running",
}


def normalize_tool_end_status(status: str | None) -> str:
    raw = str(status or "").strip().lower()
    if not raw:
        return "unknown"
    return TOOL_END_STATUS_MAP.get(raw, "unknown")


def normalize_tool_end_status_with_legacy(status: str | None) -> tuple[str, str | None]:
    raw = str(status or "").strip()
    if not raw:
        return ("unknown", None)
    normalized = normalize_tool_end_status(raw)
    if normalized == raw.lower():
        return (normalized, None)
    return (normalized, raw)
