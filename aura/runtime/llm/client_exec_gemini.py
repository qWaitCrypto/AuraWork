from __future__ import annotations

import json
from typing import Any, Iterator

from .client_gemini import gemini_to_response
from .client_httpx_errors import _wrap_httpx_like_exception
from .client_stream_guard import _maybe_close_stream, _start_cancel_closer, _start_stream_idle_watchdog
from .errors import CancellationToken, CredentialResolutionError, LLMErrorCode, LLMRequestError
from .providers.base import PreparedRequest
from .providers.gemini import GeminiAdapter, build_generate_content_url
from .secrets import resolve_credential
from .trace import LLMTrace
from .types import CanonicalRequest, LLMResponse, LLMStreamEvent, LLMStreamEventKind, ProviderKind, ToolCall


def _resolve_auth_header(*, profile) -> str:
    if profile.credential_ref is None:
        raise LLMRequestError(
            "Missing credentials for gemini profile.",
            code=LLMErrorCode.AUTH,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            retryable=False,
            details={"operation": "auth", "missing": "credential_ref"},
        )
    try:
        token = resolve_credential(profile.credential_ref)
    except CredentialResolutionError as e:
        raise LLMRequestError(
            str(e),
            code=LLMErrorCode.AUTH,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            retryable=False,
            details={"operation": "auth", "credential_ref": getattr(e, "credential_ref", None)},
            cause=e,
        ) from e
    # Gateways differ on whether they expect `Authorization: <token>` or `Authorization: Bearer <token>`.
    # Aura treats the configured credential value as the full header value; include the `Bearer ` prefix
    # in the configured api_key if your gateway requires it.
    return token.strip()


def complete_gemini(
    *,
    profile,
    request: CanonicalRequest,
    timeout_s: float | None,
    cancel: CancellationToken | None,
    trace: LLMTrace | None,
) -> LLMResponse:
    prepared = GeminiAdapter().prepare_request(profile, request)
    auth_value = _resolve_auth_header(profile=profile)
    prepared = PreparedRequest(
        method=prepared.method,
        url=prepared.url,
        headers={**prepared.headers, "Authorization": auth_value},
        json=prepared.json,
    )

    request_timeout_s = timeout_s if timeout_s is not None else profile.timeout_s
    if trace is not None:
        trace.record_prepared_request(
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            base_url=profile.base_url,
            model=profile.model_name,
            stream=False,
            timeout_s=request_timeout_s,
            payload=prepared.redacted().json,
        )

    if cancel is not None and cancel.cancelled:
        raise LLMRequestError(
            "Request cancelled.",
            code=LLMErrorCode.CANCELLED,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            retryable=False,
            details={"operation": "complete"},
        )

    try:
        import httpx  # type: ignore

        with httpx.Client(timeout=(None if request_timeout_s is None else float(request_timeout_s))) as client:
            r = client.request(
                prepared.method,
                prepared.url,
                headers=prepared.headers,
                json=prepared.json,
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        try:
            import httpx  # type: ignore

            if isinstance(e, httpx.HTTPStatusError):
                body = ""
                try:
                    body = e.response.text or ""
                except Exception:
                    body = ""
                if trace is not None:
                    try:
                        trace.write_json(
                            "provider_error_response.json",
                            {
                                "status_code": int(e.response.status_code),
                                "headers": dict(e.response.headers),
                                "text": body[:4000],
                            },
                        )
                    except Exception:
                        pass
        except Exception:
            pass
        raise _wrap_httpx_like_exception(
            e,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            operation="complete",
        ) from e

    if trace is not None:
        trace.write_json("provider_response.json", data)
    return gemini_to_response(profile_id=profile.profile_id, data=data)


def _normalize_candidates(root: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = root.get("candidates")
    if isinstance(candidates, list):
        return [c for c in candidates if isinstance(c, dict)]
    if isinstance(candidates, dict):
        return [candidates]
    return []


def _extract_chunk(
    chunk: dict[str, Any], *, tool_call_index_offset: int
) -> tuple[str, str, list[ToolCall], str | None, str | None, dict[str, Any] | None]:
    candidates = _normalize_candidates(chunk)
    if not candidates:
        return "", "", [], None, None, None
    cand0 = candidates[0]
    content = cand0.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        parts = []

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for idx, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            if part.get("thought") is True:
                thinking_parts.append(text)
            else:
                text_parts.append(text)
        fc = part.get("functionCall")
        if isinstance(fc, dict):
            name = fc.get("name")
            args = fc.get("args")
            thought_sig = (
                part.get("thoughtSignature")
                or part.get("thought_signature")
                or fc.get("thoughtSignature")
                or fc.get("thought_signature")
            )
            if not isinstance(name, str) or not name:
                continue
            if args is None:
                parsed_args: dict[str, Any] = {}
            elif isinstance(args, dict):
                parsed_args = args
            else:
                continue
            raw = json.dumps(parsed_args, ensure_ascii=False)
            tool_calls.append(
                ToolCall(
                    tool_call_id=f"gemini_{tool_call_index_offset + idx}",
                    name=name,
                    arguments=dict(parsed_args),
                    raw_arguments=raw,
                    thought_signature=(str(thought_sig) if isinstance(thought_sig, str) and thought_sig else None),
                )
            )

    finish = cand0.get("finishReason")
    finish_reason = finish if isinstance(finish, str) else None

    model = chunk.get("modelVersion")
    model_str = model if isinstance(model, str) else None
    usage = chunk.get("usageMetadata")
    usage_obj = usage if isinstance(usage, dict) else None
    return "".join(text_parts), "".join(thinking_parts), tool_calls, finish_reason, model_str, usage_obj


def _iter_sse_json(resp: Any) -> Iterator[dict[str, Any]]:
    """
    Yield JSON objects from an SSE-ish response.

    Supports:
    - standard SSE framing: `data: {...}` separated by blank lines
    - newline-delimited JSON objects (some gateways omit SSE framing)
    """
    buf: list[str] = []

    def _flush() -> dict[str, Any] | None:
        if not buf:
            return None
        raw = "\n".join(buf).strip()
        buf.clear()
        if not raw:
            return None
        if raw == "[DONE]":
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    for line in resp.iter_lines():
        if line is None:
            continue
        s = line.strip()
        if not s:
            data = _flush()
            if data is not None:
                yield data
            continue
        if s.startswith(":"):
            continue
        if s.startswith("data:"):
            buf.append(s[len("data:") :].lstrip())
            continue
        # Non-SSE mode: try parse immediately.
        if s.startswith("{") or s.startswith("["):
            try:
                loaded = json.loads(s)
            except Exception:
                continue
            if isinstance(loaded, dict):
                yield loaded
            continue
        # Otherwise buffer the line as part of a multi-line `data:` event.
        buf.append(s)

    data = _flush()
    if data is not None:
        yield data


def stream_gemini(
    *,
    profile,
    request: CanonicalRequest,
    timeout_s: float | None,
    cancel: CancellationToken | None,
    trace: LLMTrace | None,
) -> Iterator[LLMStreamEvent]:
    prepared = GeminiAdapter().prepare_request(profile, request)
    url = build_generate_content_url(base_url=profile.base_url, model_name=profile.model_name, stream=True)
    auth_value = _resolve_auth_header(profile=profile)
    prepared = PreparedRequest(
        method=prepared.method,
        url=url,
        headers={**prepared.headers, "Authorization": auth_value},
        json=prepared.json,
    )

    request_timeout_s = timeout_s if timeout_s is not None else profile.timeout_s
    if trace is not None:
        trace.record_prepared_request(
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            base_url=profile.base_url,
            model=profile.model_name,
            stream=True,
            timeout_s=request_timeout_s,
            payload=prepared.redacted().json,
        )

    if cancel is not None and cancel.cancelled:
        raise LLMRequestError(
            "Request cancelled.",
            code=LLMErrorCode.CANCELLED,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            retryable=False,
            details={"operation": "stream"},
        )

    try:
        import httpx  # type: ignore

        timeout = None if request_timeout_s is None else httpx.Timeout(float(request_timeout_s), read=float(request_timeout_s))
        client = httpx.Client(timeout=timeout)
        stream_ctx = client.stream(prepared.method, prepared.url, headers=prepared.headers, json=prepared.json)
        resp = stream_ctx.__enter__()
        resp.raise_for_status()
    except Exception as e:
        try:
            import httpx  # type: ignore

            if isinstance(e, httpx.HTTPStatusError):
                body = ""
                try:
                    body = e.response.text or ""
                except Exception:
                    body = ""
                if trace is not None:
                    try:
                        trace.write_json(
                            "provider_error_response.json",
                            {
                                "status_code": int(e.response.status_code),
                                "headers": dict(e.response.headers),
                                "text": body[:4000],
                            },
                        )
                    except Exception:
                        pass
        except Exception:
            pass
        raise _wrap_httpx_like_exception(
            e,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            operation="stream",
        ) from e

    stop_closer = _start_cancel_closer(cancel, resp)
    watchdog_timeout = float(request_timeout_s) if request_timeout_s is not None else None
    wd_stop, wd_timed_out, wd_tick, wd_phase = _start_stream_idle_watchdog(
        stream=resp,
        cancel=cancel,
        first_event_timeout_s=watchdog_timeout,
        idle_timeout_s=watchdog_timeout,
    )

    accumulated_text = ""
    accumulated_thinking = ""
    accumulated_tool_calls: list[ToolCall] = []
    finish_reason: str | None = None
    last_model: str | None = None
    last_usage: dict[str, Any] | None = None

    try:
        for chunk in _iter_sse_json(resp):
            if cancel is not None and cancel.cancelled:
                break
            if wd_tick is not None:
                try:
                    wd_tick()
                except Exception:
                    pass

            text, thinking, tool_calls, finish, model_version, usage = _extract_chunk(
                chunk, tool_call_index_offset=len(accumulated_tool_calls)
            )

            if model_version:
                last_model = model_version
            if usage is not None:
                last_usage = usage
            if finish:
                finish_reason = finish

            if thinking:
                if accumulated_thinking and thinking.startswith(accumulated_thinking):
                    delta = thinking[len(accumulated_thinking) :]
                    accumulated_thinking = thinking
                else:
                    delta = thinking
                    accumulated_thinking += thinking
                if delta:
                    ev = LLMStreamEvent(kind=LLMStreamEventKind.THINKING_DELTA, thinking_delta=delta)
                    if trace is not None:
                        trace.record_stream_event(ev)
                    yield ev

            if text:
                if accumulated_text and text.startswith(accumulated_text):
                    delta = text[len(accumulated_text) :]
                    accumulated_text = text
                else:
                    delta = text
                    accumulated_text += text
                if delta:
                    ev = LLMStreamEvent(kind=LLMStreamEventKind.TEXT_DELTA, text_delta=delta)
                    if trace is not None:
                        trace.record_stream_event(ev)
                    yield ev

            if tool_calls:
                for tc in tool_calls:
                    accumulated_tool_calls.append(tc)
                    ev = LLMStreamEvent(kind=LLMStreamEventKind.TOOL_CALL, tool_call=tc)
                    if trace is not None:
                        trace.record_stream_event(ev)
                    yield ev

            if finish_reason:
                break
    finally:
        if wd_stop is not None:
            try:
                wd_stop()
            except Exception:
                pass
        if stop_closer is not None:
            try:
                stop_closer()
            except Exception:
                pass
        _maybe_close_stream(resp)
        try:
            stream_ctx.__exit__(None, None, None)
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass

    response = gemini_to_response(
        profile_id=profile.profile_id,
        data={
            "candidates": [
                {"content": {"parts": [{"text": accumulated_text}]}, "finishReason": finish_reason, "index": 0}
            ],
            "modelVersion": last_model or profile.model_name,
            "usageMetadata": last_usage,
        },
    )
    response = LLMResponse(
        provider_kind=ProviderKind.GEMINI,
        profile_id=response.profile_id,
        model=response.model,
        text=accumulated_text,
        tool_calls=list(accumulated_tool_calls),
        usage=response.usage,
        stop_reason=finish_reason,
        request_id=response.request_id,
    )
    ev = LLMStreamEvent(kind=LLMStreamEventKind.COMPLETED, response=response)
    if trace is not None:
        trace.record_stream_event(ev)
    yield ev
