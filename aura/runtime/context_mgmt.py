from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .llm.types import CanonicalMessage, CanonicalRequest, CanonicalMessageRole


APPROX_BYTES_PER_TOKEN = 4
DEFAULT_CONTEXT_LIMIT_TOKENS = 256_000


def resolve_context_limit_tokens(context_limit_tokens: int | None) -> int:
    """
    Resolve the effective context window size for budgeting/UI.

    If the model profile doesn't declare a context limit, we treat it as 256k by default.
    """
    if isinstance(context_limit_tokens, int) and context_limit_tokens > 0:
        return int(context_limit_tokens)
    return DEFAULT_CONTEXT_LIMIT_TOKENS


def approx_tokens_from_text(text: str) -> int:
    data = text.encode("utf-8", errors="replace")
    n = len(data)
    return (n + (APPROX_BYTES_PER_TOKEN - 1)) // APPROX_BYTES_PER_TOKEN


def approx_tokens_from_json(obj: Any) -> int:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return approx_tokens_from_text(s)


def canonical_request_to_dict(request: CanonicalRequest) -> dict[str, Any]:
    return {
        "system": request.system,
        "messages": [canonical_message_to_dict(m) for m in request.messages],
        "params": dict(request.params),
        "tools": [t.__dict__ for t in request.tools],
    }


def canonical_message_to_dict(msg: CanonicalMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": msg.role.value, "content": msg.content}
    if msg.tool_call_id is not None:
        out["tool_call_id"] = msg.tool_call_id
    if msg.tool_name is not None:
        out["tool_name"] = msg.tool_name
    if msg.tool_calls:
        out["tool_calls"] = [
            {"tool_call_id": tc.tool_call_id, "name": tc.name, "arguments": tc.arguments}
            for tc in msg.tool_calls
        ]
    if msg.reasoning_content is not None:
        out["reasoning_content"] = msg.reasoning_content
    return out


@dataclass(frozen=True)
class ContextBudget:
    budget_tokens: int
    source: str


def history_budget_for_limit(context_limit_tokens: int | None, *, ratio: float, fallback_tokens: int) -> ContextBudget:
    if isinstance(context_limit_tokens, int) and context_limit_tokens > 0:
        return ContextBudget(budget_tokens=int(context_limit_tokens * ratio), source="ratio")
    return ContextBudget(budget_tokens=int(fallback_tokens), source="fallback")


def compute_context_left_percent(*, used_tokens: int, context_limit_tokens: int) -> int:
    if context_limit_tokens <= 0:
        return 0
    used = max(0, used_tokens)
    left = 1.0 - (used / float(context_limit_tokens))
    pct = int(round(left * 100.0))
    if pct < 0:
        return 0
    if pct > 100:
        return 100
    return pct


def render_context_left_line(*, used_tokens: int | None, context_limit_tokens: int | None) -> str:
    if isinstance(context_limit_tokens, int) and context_limit_tokens > 0 and isinstance(used_tokens, int):
        pct = compute_context_left_percent(used_tokens=used_tokens, context_limit_tokens=context_limit_tokens)
        return f"{pct}% context left"
    if isinstance(used_tokens, int):
        return f"~{used_tokens:,} tokens used"
    return "100% context left"


def truncate_text_to_budget(text: str, *, budget_tokens: int) -> str:
    if budget_tokens <= 0:
        return ""
    # Approximate bytes-per-token; keep a prefix and suffix.
    max_bytes = budget_tokens * APPROX_BYTES_PER_TOKEN
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text
    # Keep a small head and tail to preserve structure.
    head = raw[: max_bytes // 2]
    tail = raw[-(max_bytes - len(head)) :]
    marker = f"\n…{max(0, approx_tokens_from_text(text) - budget_tokens)} tokens truncated…\n".encode(
        "utf-8", errors="replace"
    )
    out = head + marker + tail
    return out.decode("utf-8", errors="replace")


def select_recent_messages_to_fit_budget(
    *,
    messages_newest_first: list[CanonicalMessage],
    budget_tokens: int,
) -> list[CanonicalMessage]:
    kept: list[CanonicalMessage] = []
    remaining = max(0, int(budget_tokens))
    for msg in messages_newest_first:
        if remaining <= 0:
            break
        est = approx_tokens_from_json(canonical_message_to_dict(msg))
        if est <= remaining:
            kept.append(msg)
            remaining -= est
            continue
        # If the message doesn't fit, include a truncated version if we have room.
        if remaining >= 8:
            truncated = CanonicalMessage(
                role=msg.role,
                content=truncate_text_to_budget(msg.content, budget_tokens=remaining),
                tool_call_id=msg.tool_call_id,
                tool_name=msg.tool_name,
                tool_calls=msg.tool_calls,
                reasoning_content=msg.reasoning_content,
            )
            kept.append(truncated)
        break
    return kept


def strip_tool_output_for_compaction(
    msg: CanonicalMessage, *, tool_output_budget_tokens: int = 400
) -> CanonicalMessage:
    if msg.role is not CanonicalMessageRole.TOOL:
        return msg
    if tool_output_budget_tokens <= 0:
        return CanonicalMessage(
            role=msg.role,
            content="",
            tool_call_id=msg.tool_call_id,
            tool_name=msg.tool_name,
            reasoning_content=msg.reasoning_content,
        )
    content = truncate_text_to_budget(msg.content, budget_tokens=tool_output_budget_tokens)
    return CanonicalMessage(
        role=msg.role,
        content=content,
        tool_call_id=msg.tool_call_id,
        tool_name=msg.tool_name,
        reasoning_content=msg.reasoning_content,
    )
