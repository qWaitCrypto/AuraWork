from __future__ import annotations

import threading
from typing import Any, Iterator

from .client_tool_calls import _AnthropicToolCallBuilder
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


def _anthropic_to_usage(resp: Any) -> LLMUsage | None:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    cache_creation = getattr(usage, "cache_creation_input_tokens", None)
    cache_read = getattr(usage, "cache_read_input_tokens", None)
    total = None
    if input_tokens is not None and output_tokens is not None:
        total = input_tokens + output_tokens
    return LLMUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


def _anthropic_to_response(*, profile_id: str, resp: Any) -> LLMResponse:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text_parts.append(block.text)
        elif getattr(block, "type", None) == "tool_use":
            tool_calls.append(
                ToolCall(
                    tool_call_id=block.id,
                    name=block.name,
                    arguments=block.input,
                    raw_arguments=None,
                )
            )
        else:
            raise ProviderAdapterError(f"Unsupported Anthropic content block type: {getattr(block, 'type', None)}")

    return LLMResponse(
        provider_kind=ProviderKind.ANTHROPIC,
        profile_id=profile_id,
        model=getattr(resp, "model", ""),
        text="".join(text_parts),
        tool_calls=tool_calls,
        usage=_anthropic_to_usage(resp),
        stop_reason=str(getattr(resp, "stop_reason", None)) if getattr(resp, "stop_reason", None) else None,
        request_id=getattr(resp, "id", None),
    )


def _anthropic_stream_to_events(
    *,
    profile_id: str,
    stream: Any,
    timeout_flag: threading.Event | None = None,
    on_event: callable | None = None,
    on_provider_event: callable | None = None,
) -> Iterator[LLMStreamEvent]:
    text_parts: list[str] = []
    block_types: dict[int, str] = {}
    tool_builders: dict[int, _AnthropicToolCallBuilder] = {}
    tool_calls: list[ToolCall] = []
    stop_reason: str | None = None
    request_id: str | None = None
    model: str | None = None
    usage: LLMUsage | None = None

    try:
        for event in stream:
            if on_provider_event is not None:
                try:
                    on_provider_event(event)
                except Exception:
                    pass
            if on_event is not None:
                try:
                    on_event()
                except Exception:
                    pass

            etype = getattr(event, "type", None)
            if etype == "message_start":
                msg = event.message
                request_id = getattr(msg, "id", request_id)
                model = getattr(msg, "model", model)
                continue

            if etype == "content_block_start":
                idx = event.index
                block = event.content_block
                btype = getattr(block, "type", None)
                block_types[idx] = btype
                if btype == "tool_use":
                    tool_builders[idx] = _AnthropicToolCallBuilder(tool_call_id=block.id, name=block.name)
                continue

            if etype == "content_block_delta":
                idx = event.index
                delta = event.delta
                dtype = getattr(delta, "type", None)
                if dtype == "text_delta":
                    text = delta.text
                    text_parts.append(text)
                    yield LLMStreamEvent(kind=LLMStreamEventKind.TEXT_DELTA, text_delta=text)
                    continue
                if dtype == "thinking_delta":
                    thinking = getattr(delta, "thinking", None)
                    if thinking is None:
                        thinking = getattr(delta, "text", None)
                    if thinking:
                        yield LLMStreamEvent(kind=LLMStreamEventKind.THINKING_DELTA, thinking_delta=str(thinking))
                    continue
                if dtype == "input_json_delta":
                    builder = tool_builders.get(idx)
                    if builder is None:
                        raise ProviderAdapterError("Anthropic input_json_delta without tool_use block start.")
                    builder.append_partial(delta.partial_json)
                    yield LLMStreamEvent(
                        kind=LLMStreamEventKind.TOOL_CALL_DELTA,
                        tool_call_delta=ToolCallDelta(
                            tool_call_index=idx,
                            tool_call_id=builder.tool_call_id,
                            name=builder.name,
                            raw_arguments_delta=delta.partial_json,
                        ),
                    )
                    continue
                raise ProviderAdapterError(f"Unsupported Anthropic content delta type: {dtype}")

            if etype == "content_block_stop":
                idx = event.index
                btype = block_types.get(idx)
                if btype == "tool_use":
                    builder = tool_builders.get(idx)
                    if builder is None:
                        raise ProviderAdapterError("Anthropic tool_use block stop without builder.")
                    call = builder.build()
                    tool_calls.append(call)
                    yield LLMStreamEvent(kind=LLMStreamEventKind.TOOL_CALL, tool_call=call)
                continue

            if etype == "message_delta":
                if getattr(event, "usage", None) is not None:
                    usage = _anthropic_to_usage(event)
                if getattr(event, "delta", None) is not None and getattr(event.delta, "stop_reason", None):
                    stop_reason = str(event.delta.stop_reason)
                continue

            if etype == "message_stop":
                break

            raise ProviderAdapterError(f"Unsupported Anthropic stream event type: {etype}")
    except Exception:
        if timeout_flag is None or not timeout_flag.is_set():
            raise

    if timeout_flag is not None and timeout_flag.is_set():
        return

    response = LLMResponse(
        provider_kind=ProviderKind.ANTHROPIC,
        profile_id=profile_id,
        model=model or "",
        text="".join(text_parts),
        tool_calls=tool_calls,
        usage=usage,
        stop_reason=stop_reason or ("timeout" if (timeout_flag is not None and timeout_flag.is_set()) else None),
        request_id=request_id,
    )
    yield LLMStreamEvent(kind=LLMStreamEventKind.COMPLETED, response=response)

