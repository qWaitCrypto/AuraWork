from __future__ import annotations

import time
import uuid


def new_id(prefix: str) -> str:
    ts = time.time_ns()
    rand = uuid.uuid4().hex
    return f"{prefix}_{ts:016x}_{rand}"


def new_tool_call_id() -> str:
    """
    Generate a tool_call_id that is safe for OpenAI-compatible gateways.

    Some gateways enforce a max length of 40 chars for tool call IDs.
    """

    # "call_" (5) + uuid4 hex (32) = 37 chars
    return f"call_{uuid.uuid4().hex}"


def now_ts_ms() -> int:
    return int(time.time() * 1000)
