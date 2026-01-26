from __future__ import annotations

import json
import threading
from typing import Any, Iterator

from .client_tool_calls import _OpenAIToolCallBuilder
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


def _openai_to_usage(resp: Any) -> LLMUsage | None:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    total = getattr(usage, "total_tokens", None)
    return LLMUsage(input_tokens=prompt, output_tokens=completion, total_tokens=total)


def _openai_to_response(*, profile_id: str, resp: Any) -> LLMResponse:
    choice0 = resp.choices[0]
    msg = choice0.message
    tool_calls: list[ToolCall] = []
    if getattr(msg, "tool_calls", None):
        for tc in msg.tool_calls:
            raw_args = tc.function.arguments or ""
            try:
                parsed_any = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError as e:
                raise ProviderAdapterError(f"OpenAI tool call arguments are not valid JSON: {e}") from e
            if not isinstance(parsed_any, dict):
                raise ProviderAdapterError("OpenAI tool call arguments must be a JSON object.")
            tool_calls.append(
                ToolCall(
                    tool_call_id=getattr(tc, "id", None),
                    name=tc.function.name,
                    arguments=parsed_any,
                    raw_arguments=raw_args,
                )
            )
    return LLMResponse(
        provider_kind=ProviderKind.OPENAI_COMPATIBLE,
        profile_id=profile_id,
        model=getattr(resp, "model", ""),
        text=getattr(msg, "content", None) or "",
        tool_calls=tool_calls,
        usage=_openai_to_usage(resp),
        stop_reason=getattr(choice0, "finish_reason", None),
        request_id=getattr(resp, "id", None),
        thinking=(
            str(thinking)
            if (
                (thinking := (getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)))
                and isinstance(thinking, str)
                and thinking.strip()
            )
            else None
        ),
    )


def _openai_stream_to_events(
    *,
    profile_id: str,
    stream: Any,
    timeout_flag: threading.Event | None = None,
    on_chunk: callable | None = None,
    on_provider_chunk: callable | None = None,
) -> Iterator[LLMStreamEvent]:
    text_parts: list[str] = []
    tool_builders: dict[int, _OpenAIToolCallBuilder] = {}
    accumulated_thinking = ""
    stop_reason: str | None = None
    request_id: str | None = None
    model: str | None = None
    usage: LLMUsage | None = None

    try:
        for chunk in stream:
            if on_provider_chunk is not None:
                try:
                    on_provider_chunk(chunk)
                except Exception:
                    pass
            if on_chunk is not None:
                try:
                    on_chunk()
                except Exception:
                    pass
            if request_id is None:
                request_id = getattr(chunk, "id", None)
            if model is None:
                model = getattr(chunk, "model", None)
            if getattr(chunk, "usage", None) is not None:
                usage = _openai_to_usage(chunk)

            if not chunk.choices:
                continue
            choice0 = chunk.choices[0]
            delta = choice0.delta

            if getattr(choice0, "finish_reason", None) is not None:
                stop_reason = choice0.finish_reason

            # Some OpenAI-compatible gateways expose reasoning/thinking in a separate field.
            # Keep it out of final assistant text, but surface it as a thinking stream.
            reasoning_delta = getattr(delta, "reasoning_content", None)
            if reasoning_delta is None:
                reasoning_delta = getattr(delta, "reasoning", None)
            if reasoning_delta:
                thinking_text = str(reasoning_delta)
                if accumulated_thinking and thinking_text.startswith(accumulated_thinking):
                    delta_text = thinking_text[len(accumulated_thinking) :]
                    accumulated_thinking = thinking_text
                else:
                    delta_text = thinking_text
                    accumulated_thinking += thinking_text
                if delta_text:
                    yield LLMStreamEvent(kind=LLMStreamEventKind.THINKING_DELTA, thinking_delta=delta_text)

            content_delta = getattr(delta, "content", None)
            if content_delta:
                text_parts.append(content_delta)
                yield LLMStreamEvent(kind=LLMStreamEventKind.TEXT_DELTA, text_delta=content_delta)

            tool_deltas = getattr(delta, "tool_calls", None)
            if tool_deltas:
                for tc_delta in tool_deltas:
                    idx = tc_delta.index
                    builder = tool_builders.get(idx)
                    if builder is None:
                        builder = _OpenAIToolCallBuilder()
                        tool_builders[idx] = builder
                    if getattr(tc_delta, "id", None):
                        builder.tool_call_id = tc_delta.id
                    if getattr(tc_delta, "function", None) is not None:
                        fn = tc_delta.function
                        if getattr(fn, "name", None):
                            builder.name = fn.name
                        if getattr(fn, "arguments", None):
                            builder.append_arguments(fn.arguments)
                            yield LLMStreamEvent(
                                kind=LLMStreamEventKind.TOOL_CALL_DELTA,
                                tool_call_delta=ToolCallDelta(
                                    tool_call_index=idx,
                                    tool_call_id=builder.tool_call_id,
                                    name=builder.name,
                                    raw_arguments_delta=fn.arguments,
                                ),
                            )
    except Exception:
        # If we triggered an idle watchdog close, treat it as a terminal stream boundary
        # and still surface the partial response; the caller will turn this into TIMEOUT.
        if timeout_flag is None or not timeout_flag.is_set():
            raise

    if timeout_flag is not None and timeout_flag.is_set():
        return

    tool_calls = [tool_builders[i].build() for i in sorted(tool_builders)]
    for call in tool_calls:
        yield LLMStreamEvent(kind=LLMStreamEventKind.TOOL_CALL, tool_call=call)
    response = LLMResponse(
        provider_kind=ProviderKind.OPENAI_COMPATIBLE,
        profile_id=profile_id,
        model=model or "",
        text="".join(text_parts),
        tool_calls=tool_calls,
        usage=usage,
        stop_reason=stop_reason or ("timeout" if (timeout_flag is not None and timeout_flag.is_set()) else None),
        request_id=request_id,
        thinking=(accumulated_thinking if (tool_calls and accumulated_thinking.strip()) else None),
    )
    yield LLMStreamEvent(kind=LLMStreamEventKind.COMPLETED, response=response)
