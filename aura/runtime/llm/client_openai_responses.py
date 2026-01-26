from __future__ import annotations

import json
import threading
from typing import Any, Iterator

from .errors import ProviderAdapterError
from .types import (
    LLMResponse,
    LLMStreamEvent,
    LLMStreamEventKind,
    LLMUsage,
    ProviderKind,
    ToolCall,
    ToolCallDelta,
)


def _responses_to_usage(resp: Any) -> LLMUsage | None:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    return LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _responses_to_response(*, provider_kind: ProviderKind, profile_id: str, resp: Any) -> LLMResponse:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    output = getattr(resp, "output", None) or []
    for item in output:
        item_type = getattr(item, "type", None) if not isinstance(item, dict) else item.get("type")
        if item_type == "message":
            content = getattr(item, "content", None) if not isinstance(item, dict) else item.get("content")
            if isinstance(content, list):
                for part in content:
                    part_type = getattr(part, "type", None) if not isinstance(part, dict) else part.get("type")
                    if part_type == "output_text":
                        text = getattr(part, "text", None) if not isinstance(part, dict) else part.get("text")
                        if isinstance(text, str):
                            text_parts.append(text)
            continue
        if item_type == "function_call":
            call_id = getattr(item, "call_id", None) if not isinstance(item, dict) else item.get("call_id")
            name = getattr(item, "name", None) if not isinstance(item, dict) else item.get("name")
            raw_args = getattr(item, "arguments", None) if not isinstance(item, dict) else item.get("arguments")
            raw_args = raw_args or ""
            try:
                parsed_any = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError as e:
                raise ProviderAdapterError(f"Responses tool call arguments are not valid JSON: {e}") from e
            if not isinstance(parsed_any, dict):
                raise ProviderAdapterError("Responses tool call arguments must be a JSON object.")
            tool_calls.append(
                ToolCall(
                    tool_call_id=str(call_id) if call_id is not None else None,
                    name=str(name) if name is not None else "",
                    arguments=parsed_any,
                    raw_arguments=str(raw_args),
                )
            )
            continue

    return LLMResponse(
        provider_kind=provider_kind,
        profile_id=profile_id,
        model=str(getattr(resp, "model", "") or ""),
        text="".join(text_parts),
        tool_calls=tool_calls,
        usage=_responses_to_usage(resp),
        stop_reason=str(getattr(resp, "status", None) or "") or None,
        request_id=str(getattr(resp, "id", None) or "") or None,
    )


def _responses_stream_to_events(
    *,
    provider_kind: ProviderKind,
    profile_id: str,
    stream: Any,
    timeout_flag: threading.Event | None = None,
    on_chunk: callable | None = None,
    on_provider_chunk: callable | None = None,
) -> Iterator[LLMStreamEvent]:
    text_parts: list[str] = []
    tool_calls_by_output_index: dict[int, dict[str, Any]] = {}
    saw_terminal = False

    try:
        for event in stream:
            if on_provider_chunk is not None:
                try:
                    on_provider_chunk(event)
                except Exception:
                    pass
            if on_chunk is not None:
                try:
                    on_chunk()
                except Exception:
                    pass

            event_type = getattr(event, "type", None)
            if event_type == "response.output_text.delta":
                delta = getattr(event, "delta", None)
                if isinstance(delta, str) and delta:
                    text_parts.append(delta)
                    yield LLMStreamEvent(kind=LLMStreamEventKind.TEXT_DELTA, text_delta=delta)
                continue

            if event_type == "response.output_item.added":
                item = getattr(event, "item", None)
                item_type = getattr(item, "type", None)
                if item_type == "function_call":
                    output_index = int(getattr(event, "output_index", 0))
                    tool_calls_by_output_index[output_index] = {
                        "call_id": getattr(item, "call_id", None),
                        "name": getattr(item, "name", None),
                        "raw": "",
                    }
                continue

            if event_type == "response.function_call_arguments.delta":
                output_index = int(getattr(event, "output_index", 0))
                delta = getattr(event, "delta", None)
                if isinstance(delta, str) and delta:
                    rec = tool_calls_by_output_index.get(output_index)
                    if rec is not None:
                        rec["raw"] = (rec.get("raw") or "") + delta
                        yield LLMStreamEvent(
                            kind=LLMStreamEventKind.TOOL_CALL_DELTA,
                            tool_call_delta=ToolCallDelta(
                                tool_call_index=output_index,
                                tool_call_id=str(rec.get("call_id") or ""),
                                name=str(rec.get("name") or ""),
                                raw_arguments_delta=delta,
                            ),
                        )
                continue

            if event_type == "response.output_item.done":
                item = getattr(event, "item", None)
                item_type = getattr(item, "type", None)
                if item_type == "function_call":
                    call_id = getattr(item, "call_id", None)
                    name = getattr(item, "name", None)
                    raw_args = getattr(item, "arguments", None) or ""
                    try:
                        parsed_any = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError as e:
                        raise ProviderAdapterError(f"Responses tool call arguments are not valid JSON: {e}") from e
                    if not isinstance(parsed_any, dict):
                        raise ProviderAdapterError("Responses tool call arguments must be a JSON object.")
                    yield LLMStreamEvent(
                        kind=LLMStreamEventKind.TOOL_CALL,
                        tool_call=ToolCall(
                            tool_call_id=str(call_id) if call_id is not None else None,
                            name=str(name) if name is not None else "",
                            arguments=parsed_any,
                            raw_arguments=str(raw_args),
                        ),
                    )
                continue

            if event_type == "response.completed":
                resp = getattr(event, "response", None)
                out = _responses_to_response(provider_kind=provider_kind, profile_id=profile_id, resp=resp)
                yield LLMStreamEvent(kind=LLMStreamEventKind.COMPLETED, response=out)
                saw_terminal = True
                return

    except Exception:
        if timeout_flag is None or not timeout_flag.is_set():
            raise

    if timeout_flag is not None and timeout_flag.is_set():
        return

    if saw_terminal:
        return

    # If the provider ends the stream without a terminal response.completed, surface what we have.
    yield LLMStreamEvent(
        kind=LLMStreamEventKind.COMPLETED,
        response=LLMResponse(
            provider_kind=provider_kind,
            profile_id=profile_id,
            model="",
            text="".join(text_parts),
            tool_calls=[],
            usage=None,
            stop_reason="timeout" if (timeout_flag is not None and timeout_flag.is_set()) else None,
            request_id=None,
        ),
    )
