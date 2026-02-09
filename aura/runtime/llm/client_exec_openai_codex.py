from __future__ import annotations

import base64
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from .client_common import _assert_no_reserved_params, _raise_if_cancelled
from .client_openai import _openai_stream_to_events, _openai_to_response
from .client_openai_responses import _responses_stream_to_events, _responses_to_response
from .client_stream_guard import _maybe_close_stream, _start_cancel_closer, _start_stream_idle_watchdog
from .client_httpx_errors import _wrap_httpx_like_exception
from .errors import (
    CancellationToken,
    CredentialResolutionError,
    LLMErrorCode,
    LLMRequestError,
    ProviderAdapterError,
    classify_provider_exception,
    wrap_provider_exception,
)
from .providers.openai_codex import OpenAICodexAdapter
from .providers.openai_compatible import OpenAICompatibleAdapter
from .secrets import resolve_credential
from .trace import LLMTrace
from .types import (
    CanonicalRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMStreamEventKind,
    ProviderKind,
    ToolCall,
    ToolCallDelta,
)


def _dedupe_tool_calls(calls: list[ToolCall]) -> list[ToolCall]:
    seen: set[tuple[str | None, str, str | None]] = set()
    out: list[ToolCall] = []
    for tc in calls:
        key = (tc.tool_call_id, tc.name, tc.raw_arguments)
        if key in seen:
            continue
        seen.add(key)
        out.append(tc)
    return out


def _finalize_response_from_stream_events(
    *,
    provider_kind: ProviderKind,
    profile_id: str,
    events: Iterator[LLMStreamEvent],
) -> LLMResponse:
    """
    Convert a stream of canonical LLMStreamEvents into a single LLMResponse.

    Important for gateways that do not emit `response.completed`: tool calls may only be visible as
    TOOL_CALL events (from `response.output_item.done`), so we must preserve them for non-streaming callers.
    """

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    final: LLMResponse | None = None

    for ev in events:
        if ev.kind is LLMStreamEventKind.TEXT_DELTA and isinstance(ev.text_delta, str) and ev.text_delta:
            text_parts.append(ev.text_delta)
            continue
        if ev.kind is LLMStreamEventKind.THINKING_DELTA and isinstance(ev.thinking_delta, str) and ev.thinking_delta:
            thinking_parts.append(ev.thinking_delta)
            continue
        if ev.kind is LLMStreamEventKind.TOOL_CALL and ev.tool_call is not None:
            tool_calls.append(ev.tool_call)
            continue
        if ev.kind is LLMStreamEventKind.COMPLETED and ev.response is not None:
            final = ev.response
            continue

    merged_text = "".join(text_parts)
    merged_thinking = "".join(thinking_parts)
    merged_tool_calls = _dedupe_tool_calls(tool_calls)

    if final is None:
        return LLMResponse(
            provider_kind=provider_kind,
            profile_id=profile_id,
            model="",
            text=merged_text,
            tool_calls=merged_tool_calls,
            usage=None,
            stop_reason="eof",
            request_id=None,
            thinking=(merged_thinking if merged_thinking else None),
        )

    return replace(
        final,
        text=(final.text if final.text else merged_text),
        thinking=(final.thinking if (isinstance(final.thinking, str) and final.thinking.strip()) else (merged_thinking if merged_thinking else None)),
        tool_calls=(_dedupe_tool_calls([*(final.tool_calls or []), *merged_tool_calls]) if merged_tool_calls else final.tool_calls),
    )


def _blocked_fallback_request(request: CanonicalRequest) -> CanonicalRequest:
    # Some OpenAI-compatible gateways block large system prompts and/or tool schemas.
    # Retrying with a minimal instruction and no tools often succeeds (and matches common curl examples).
    return CanonicalRequest(
        system="You are an AI assistant.",
        messages=request.messages,
        tools=[],
        params=dict(request.params),
    )


def _should_retry_blocked_without_tools(*, request: CanonicalRequest) -> bool:
    system = request.system or ""
    # Heuristic: retry if we sent any tools OR the system prompt is very large.
    return bool(request.tools) or len(system) >= 4000


def _should_use_httpx_for_responses(*, base_url: str, credential_ref_kind: str | None) -> bool:
    """
    Some OpenAI-compatible gateways block the official OpenAI Python SDK (often via WAF rules on headers/UA).
    For non-OpenAI hosts, prefer raw httpx requests with minimal curl-like headers.
    """

    if os.environ.get("AURA_OPENAI_CODEX_FORCE_HTTPX", "").strip() in {"1", "true", "yes", "on"}:
        return True
    if credential_ref_kind != "inline":
        return False
    host = (urlparse(base_url).hostname or "").lower()
    # Keep OpenAI platform on the SDK path by default.
    if host in {"api.openai.com"}:
        return False
    return True


def _is_openai_platform_base_url(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    # Keep this intentionally strict: gateways often mimic OpenAI-compatible paths but not /responses.
    return host == "api.openai.com"


def _iter_sse_json(resp: Any) -> Iterator[dict[str, Any]]:
    """
    Yield JSON objects from an SSE-ish response.

    Supports:
    - standard SSE framing: `data: {...}` separated by blank lines
    - newline-delimited JSON objects (some gateways omit SSE framing)
    """

    import json as _json

    buf: list[str] = []
    current_event: str | None = None

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
            data = _json.loads(raw)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        # Some providers use `event: ...` and omit a `type` field in the JSON.
        if current_event and "type" not in data:
            try:
                data = dict(data)
                data["type"] = current_event
            except Exception:
                pass
        # Some gateways wrap as {"event": "...", "data": {...}}.
        ev_name = data.get("event")
        ev_data = data.get("data")
        if isinstance(ev_name, str) and isinstance(ev_data, dict):
            out = dict(ev_data)
            out.setdefault("type", ev_name)
            return out
        return data

    for line in resp.iter_lines():
        if line is None:
            continue
        if isinstance(line, (bytes, bytearray)):
            try:
                line = line.decode("utf-8", errors="replace")
            except Exception:
                continue
        s = str(line).strip()
        if not s:
            data = _flush()
            current_event = None
            if data is not None:
                yield data
            continue
        if s.startswith(":"):
            continue
        if s.startswith("event:"):
            current_event = s[len("event:") :].strip() or None
            continue
        if s.startswith("data:"):
            buf.append(s[len("data:") :].lstrip())
            continue
        if s.startswith("{") or s.startswith("["):
            try:
                loaded = _json.loads(s)
            except Exception:
                continue
            if isinstance(loaded, dict):
                yield loaded
            continue
        buf.append(s)

    data = _flush()
    if data is not None:
        yield data


def _responses_sse_dicts_to_events(
    *,
    provider_kind: ProviderKind,
    profile_id: str,
    events: Iterator[dict[str, Any]],
    on_provider_chunk: callable | None = None,
) -> Iterator[LLMStreamEvent]:
    import json as _json

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls_by_output_index: dict[int, dict[str, Any]] = {}
    saw_terminal = False

    def _coerce_text(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for k in ("text", "delta", "content"):
                v = value.get(k)
                if isinstance(v, str):
                    return v
        return None

    for ev in events:
        if on_provider_chunk is not None:
            try:
                on_provider_chunk(ev)
            except Exception:
                pass

        event_type = ev.get("type") or ev.get("event")
        if event_type is None and isinstance(ev.get("response"), (dict, list)):
            event_type = "response.completed"
        if event_type is None and isinstance(ev.get("output"), list):
            # Some gateways may stream only the final response object.
            event_type = "response.completed"

        if isinstance(event_type, str) and ("reasoning" in event_type or "thinking" in event_type):
            delta = _coerce_text(ev.get("delta")) or _coerce_text(ev.get("text"))
            if isinstance(delta, str) and delta:
                thinking_parts.append(delta)
                yield LLMStreamEvent(kind=LLMStreamEventKind.THINKING_DELTA, thinking_delta=delta)
            continue

        if event_type == "response.output_text.delta":
            delta = _coerce_text(ev.get("delta"))
            if isinstance(delta, str) and delta:
                text_parts.append(delta)
                yield LLMStreamEvent(kind=LLMStreamEventKind.TEXT_DELTA, text_delta=delta)
            continue

        if event_type == "response.output_text.done":
            # Some gateways emit only a terminal "done" with full text (no deltas).
            if not text_parts:
                text = _coerce_text(ev.get("text")) or _coerce_text(ev.get("output_text"))
                if isinstance(text, str) and text:
                    text_parts.append(text)
                    yield LLMStreamEvent(kind=LLMStreamEventKind.TEXT_DELTA, text_delta=text)
            continue

        if event_type == "response.output_item.added":
            item = ev.get("item") if isinstance(ev.get("item"), dict) else None
            item_type = item.get("type") if item else None
            if item_type == "function_call":
                output_index = int(ev.get("output_index") or 0)
                tool_calls_by_output_index[output_index] = {
                    "call_id": item.get("call_id") if item else None,
                    "name": item.get("name") if item else None,
                    "raw": "",
                }
            continue

        if event_type == "response.function_call_arguments.delta":
            output_index = int(ev.get("output_index") or 0)
            delta = ev.get("delta")
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
            item = ev.get("item") if isinstance(ev.get("item"), dict) else None
            if not item:
                continue
            item_type = item.get("type")
            if item_type == "function_call":
                call_id = item.get("call_id")
                name = item.get("name")
                raw_args = item.get("arguments") or ""
                try:
                    parsed_any = _json.loads(raw_args) if raw_args else {}
                except _json.JSONDecodeError as e:
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
            resp = ev.get("response")
            out = _responses_to_response(
                provider_kind=provider_kind, profile_id=profile_id, resp=(resp if resp is not None else ev)
            )
            if not out.text and text_parts:
                out = replace(out, text="".join(text_parts))
            if (out.thinking is None or not str(out.thinking or "").strip()) and thinking_parts:
                out = replace(out, thinking="".join(thinking_parts))
            yield LLMStreamEvent(kind=LLMStreamEventKind.COMPLETED, response=out)
            saw_terminal = True
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
            stop_reason="eof",
            request_id=None,
            thinking=("".join(thinking_parts) if thinking_parts else None),
        ),
    )


def _httpx_post_json(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_s: float | None,
    trace: LLMTrace | None,
    provider_kind: ProviderKind,
    profile_id: str,
    model: str | None,
    operation: str,
) -> dict[str, Any]:
    try:
        import httpx  # type: ignore

        timeout = None if timeout_s is None else httpx.Timeout(float(timeout_s), read=float(timeout_s))
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method.upper(), url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ProviderAdapterError("Provider returned non-object JSON.")
            return data
    except Exception as e:
        try:
            import httpx  # type: ignore

            if isinstance(e, httpx.HTTPStatusError) and trace is not None:
                body = ""
                try:
                    body = e.response.text or ""
                except Exception:
                    body = ""
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
            provider_kind=provider_kind,
            profile_id=profile_id,
            model=model,
            operation=operation,
        ) from e


def _httpx_stream_responses(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_s: float | None,
    trace: LLMTrace | None,
    provider_kind: ProviderKind,
    profile_id: str,
    model: str | None,
    operation: str,
) -> Iterator[dict[str, Any]]:
    try:
        import httpx  # type: ignore

        timeout = None if timeout_s is None else httpx.Timeout(float(timeout_s), read=float(timeout_s))
        client = httpx.Client(timeout=timeout)
        stream_ctx = client.stream("POST", url, headers=headers, json=payload)
        resp = stream_ctx.__enter__()
        resp.raise_for_status()
    except Exception as e:
        try:
            import httpx  # type: ignore

            if isinstance(e, httpx.HTTPStatusError) and trace is not None:
                body = ""
                try:
                    body = e.response.text or ""
                except Exception:
                    body = ""
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
        if trace is not None:
            try:
                trace.record_error(e, code=classify_provider_exception(e).value)  # type: ignore[name-defined]
            except Exception:
                pass
        raise _wrap_httpx_like_exception(
            e,
            provider_kind=provider_kind,
            profile_id=profile_id,
            model=model,
            operation=operation,
        ) from e

    try:
        yield from _iter_sse_json(resp)
    finally:
        try:
            stream_ctx.__exit__(None, None, None)
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass


def _token_supports_openai_responses(token: str) -> bool:
    # For platform API keys ("sk-..."), treat as responses-capable by default.
    parts = token.split(".")
    if len(parts) != 3:
        return True
    try:
        payload_b64 = parts[1]
        pad = "=" * (-len(payload_b64) % 4)
        payload_raw = base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii"))
        payload = json.loads(payload_raw.decode("utf-8"))
    except Exception:
        return True
    scp = payload.get("scp")
    if isinstance(scp, list):
        return "api.responses.write" in scp
    return True


def _codex_default_headers(token: str, *, auth_json_account_id: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}

    account_id = None
    if isinstance(auth_json_account_id, str) and auth_json_account_id.strip():
        account_id = auth_json_account_id.strip()
    else:
        parts = token.split(".")
        if len(parts) == 3:
            try:
                payload_b64 = parts[1]
                pad = "=" * (-len(payload_b64) % 4)
                payload_raw = base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii"))
                payload = json.loads(payload_raw.decode("utf-8"))
                auth = payload.get("https://api.openai.com/auth")
                if isinstance(auth, dict):
                    cid = auth.get("chatgpt_account_id")
                    if isinstance(cid, str) and cid.strip():
                        account_id = cid.strip()
            except Exception:
                account_id = None

    if account_id:
        headers["chatgpt-account-id"] = account_id
        headers["ChatGPT-Account-Id"] = account_id

    version = os.environ.get("AURA_CODEX_CLI_VERSION")
    if not (isinstance(version, str) and version.strip()):
        version = _maybe_codex_cli_version()
    if not (isinstance(version, str) and version.strip()):
        version = "0.91.0"
    headers["version"] = version.strip()

    return headers


_CACHED_CODEX_CLI_VERSION: str | None = None


def _maybe_codex_cli_version() -> str | None:
    global _CACHED_CODEX_CLI_VERSION
    if _CACHED_CODEX_CLI_VERSION is not None:
        return _CACHED_CODEX_CLI_VERSION
    try:
        out = subprocess.check_output(["codex", "--version"], stderr=subprocess.STDOUT, text=True, timeout=2)
        v = out.strip().splitlines()[-1].strip()
        _CACHED_CODEX_CLI_VERSION = v or None
        return _CACHED_CODEX_CLI_VERSION
    except Exception:
        _CACHED_CODEX_CLI_VERSION = None
        return None


def _is_chatgpt_codex_route(base_url: str) -> bool:
    return "/backend-api/codex" in base_url


def _codex_auth_account_id_if_available(credential_ref) -> str | None:
    if getattr(credential_ref, "kind", None) != "codex_cli":
        return None
    identifier = getattr(credential_ref, "identifier", "") or ""
    auth_path = identifier.strip() if identifier.strip() else str(Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json")
    try:
        data = json.loads(Path(auth_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        acct = tokens.get("account_id")
        if isinstance(acct, str) and acct.strip():
            return acct.strip()
    return None


def complete_openai_codex(
    *,
    profile,
    request: CanonicalRequest,
    timeout_s: float | None,
    cancel: CancellationToken | None,
    trace: LLMTrace | None,
) -> LLMResponse:
    from . import client as _client_mod

    openai = _client_mod.openai
    OpenAI = _client_mod.OpenAI

    _assert_no_reserved_params(
        profile_id=profile.profile_id,
        provider_kind=profile.provider_kind,
        profile_default_params=profile.default_params,
        request_params=request.params,
        reserved_keys={"model", "messages", "tools", "stream", "timeout", "input", "instructions", "store"},
    )
    if profile.credential_ref is None:
        raise LLMRequestError(
            "Missing credentials for openai_codex profile.",
            code=LLMErrorCode.AUTH,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            retryable=False,
            details={"operation": "complete", "missing": "credential_ref"},
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
            details={"operation": "complete", "credential_ref": getattr(e, "credential_ref", None)},
            cause=e,
        ) from e

    account_id = _codex_auth_account_id_if_available(profile.credential_ref)
    headers = _codex_default_headers(token, auth_json_account_id=account_id) if _is_chatgpt_codex_route(profile.base_url) else {}
    client = OpenAI(api_key=token, base_url=profile.base_url, max_retries=0, default_headers=headers or None)

    request_timeout_s = timeout_s if timeout_s is not None else profile.timeout_s

    _raise_if_cancelled(
        cancel,
        provider_kind=profile.provider_kind,
        profile_id=profile.profile_id,
        model=profile.model_name,
        operation="complete",
    )

    if _is_chatgpt_codex_route(profile.base_url) or _token_supports_openai_responses(token):
        def _fallback_complete_chat_completions() -> LLMResponse:
            payload = OpenAICompatibleAdapter().prepare_request(
                replace(profile, provider_kind=ProviderKind.OPENAI_COMPATIBLE), request
            ).json
            if trace is not None:
                trace.record_prepared_request(
                    provider_kind=profile.provider_kind,
                    profile_id=profile.profile_id,
                    base_url=profile.base_url,
                    model=profile.model_name,
                    stream=False,
                    timeout_s=request_timeout_s,
                    payload=payload,
                )
            try:
                resp = client.chat.completions.create(**payload, timeout=request_timeout_s)
            except openai.OpenAIError as e:
                raise wrap_provider_exception(
                    e,
                    provider_kind=profile.provider_kind,
                    profile_id=profile.profile_id,
                    model=profile.model_name,
                    operation="complete",
                ) from e
            out = _openai_to_response(profile_id=profile.profile_id, resp=resp)
            return LLMResponse(
                provider_kind=ProviderKind.OPENAI_CODEX,
                profile_id=out.profile_id,
                model=out.model,
                text=out.text,
                tool_calls=out.tool_calls,
                usage=out.usage,
                stop_reason=out.stop_reason,
                request_id=out.request_id,
            )

        payload = OpenAICodexAdapter().prepare_request(profile, request).json
        if not (isinstance(payload.get("instructions"), str) and payload["instructions"].strip()):
            payload["instructions"] = "You are a helpful assistant."
        payload["store"] = False
        use_httpx = _should_use_httpx_for_responses(
            base_url=profile.base_url,
            credential_ref_kind=getattr(profile.credential_ref, "kind", None),
        )
        if use_httpx:
            prepared = OpenAICodexAdapter().prepare_request(profile, request)
            http_headers = dict(prepared.headers)
            http_headers.setdefault("Accept", "text/event-stream")
            try_payload = dict(payload)
            try_payload["stream"] = True
            if trace is not None:
                trace.record_prepared_request(
                    provider_kind=profile.provider_kind,
                    profile_id=profile.profile_id,
                    base_url=profile.base_url,
                    model=profile.model_name,
                    stream=True,
                    timeout_s=request_timeout_s,
                    payload=try_payload,
                )
            try:
                return _finalize_response_from_stream_events(
                    provider_kind=ProviderKind.OPENAI_CODEX,
                    profile_id=profile.profile_id,
                    events=_responses_sse_dicts_to_events(
                        provider_kind=ProviderKind.OPENAI_CODEX,
                        profile_id=profile.profile_id,
                        events=_httpx_stream_responses(
                            url=prepared.url,
                            headers=http_headers,
                            payload=try_payload,
                            timeout_s=request_timeout_s,
                            trace=trace,
                            provider_kind=profile.provider_kind,
                            profile_id=profile.profile_id,
                            model=profile.model_name,
                            operation="complete",
                        ),
                        on_provider_chunk=(None if trace is None else trace.record_provider_item),
                    ),
                )
            except LLMRequestError as e:
                if trace is not None:
                    try:
                        trace.record_error(e, code=e.code.value)
                    except Exception:
                        pass
                if (
                    not _is_openai_platform_base_url(profile.base_url)
                    and e.code in {LLMErrorCode.BAD_REQUEST, LLMErrorCode.NOT_FOUND, LLMErrorCode.UNPROCESSABLE}
                ):
                    if trace is not None:
                        try:
                            trace.record_meta(fallback="responses_unavailable:chat_completions")
                        except Exception:
                            pass
                    return _fallback_complete_chat_completions()
                if e.code == LLMErrorCode.PERMISSION and _should_retry_blocked_without_tools(request=request):
                    fallback_req = _blocked_fallback_request(request)
                    fallback_payload = OpenAICodexAdapter().prepare_request(profile, fallback_req).json
                    fallback_payload["store"] = False
                    fallback_payload["stream"] = True
                    if trace is not None:
                        try:
                            trace.write_json(
                                "prepared_request_fallback.json",
                                {
                                    "reason": "permission_blocked",
                                    "strategy": "minimal_instructions_no_tools",
                                    "payload": fallback_payload,
                                },
                            )
                            trace.record_meta(fallback="permission_blocked:minimal_instructions_no_tools")
                        except Exception:
                            pass
                    return _finalize_response_from_stream_events(
                        provider_kind=ProviderKind.OPENAI_CODEX,
                        profile_id=profile.profile_id,
                        events=_responses_sse_dicts_to_events(
                            provider_kind=ProviderKind.OPENAI_CODEX,
                            profile_id=profile.profile_id,
                            events=_httpx_stream_responses(
                                url=prepared.url,
                                headers=http_headers,
                                payload=fallback_payload,
                                timeout_s=request_timeout_s,
                                trace=trace,
                                provider_kind=profile.provider_kind,
                                profile_id=profile.profile_id,
                                model=profile.model_name,
                                operation="complete",
                            ),
                            on_provider_chunk=(None if trace is None else trace.record_provider_item),
                        ),
                    )
                else:
                    raise
        if trace is not None:
            trace.record_prepared_request(
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                base_url=profile.base_url,
                model=profile.model_name,
                stream=True,
                timeout_s=request_timeout_s,
                payload=payload,
            )
        try:
            raw_stream = client.responses.create(**payload, stream=True, timeout=request_timeout_s)
        except (openai.BadRequestError, openai.NotFoundError, openai.UnprocessableEntityError) as e:
            # Many OpenAI-compatible gateways do not implement the Responses API. Fall back to chat.completions.
            if not _is_openai_platform_base_url(profile.base_url):
                if trace is not None:
                    try:
                        trace.record_error(e, code=classify_provider_exception(e).value)  # type: ignore[name-defined]
                        trace.record_meta(fallback="responses_unavailable:chat_completions")
                    except Exception:
                        pass
                return _fallback_complete_chat_completions()
            raise wrap_provider_exception(
                e,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                operation="complete",
            ) from e
        except openai.PermissionDeniedError as e:
            # Retry once with a minimal prompt and no tools, if the request likely got blocked by a gateway filter.
            if _should_retry_blocked_without_tools(request=request):
                fallback_req = _blocked_fallback_request(request)
                fallback_payload = OpenAICodexAdapter().prepare_request(profile, fallback_req).json
                fallback_payload["store"] = False
                if not (
                    isinstance(fallback_payload.get("instructions"), str) and fallback_payload["instructions"].strip()
                ):
                    fallback_payload["instructions"] = "You are a helpful assistant."
                if trace is not None:
                    try:
                        trace.write_json(
                            "prepared_request_fallback.json",
                            {
                                "reason": "permission_blocked",
                                "strategy": "minimal_instructions_no_tools",
                                "payload": fallback_payload,
                            },
                        )
                        trace.record_meta(fallback="permission_blocked:minimal_instructions_no_tools")
                    except Exception:
                        pass
                try:
                    raw_stream = client.responses.create(**fallback_payload, stream=True, timeout=request_timeout_s)
                except openai.OpenAIError as e2:
                    raise wrap_provider_exception(
                        e2,
                        provider_kind=profile.provider_kind,
                        profile_id=profile.profile_id,
                        model=profile.model_name,
                        operation="complete",
                    ) from e2
            else:
                raise wrap_provider_exception(
                    e,
                    provider_kind=profile.provider_kind,
                    profile_id=profile.profile_id,
                    model=profile.model_name,
                    operation="complete",
                ) from e
        except openai.OpenAIError as e:
            if trace is not None:
                try:
                    trace.record_error(e, code=classify_provider_exception(e).value)  # type: ignore[name-defined]
                except Exception:
                    pass
            raise wrap_provider_exception(
                e,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                operation="complete",
            ) from e
        final: LLMResponse | None = None
        tool_calls: list[ToolCall] = []
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        try:
            for ev in _responses_stream_to_events(
                provider_kind=ProviderKind.OPENAI_CODEX,
                profile_id=profile.profile_id,
                stream=raw_stream,
                on_provider_chunk=(None if trace is None else trace.record_provider_item),
            ):
                if trace is not None:
                    trace.record_stream_event(ev)
                if ev.kind is LLMStreamEventKind.TEXT_DELTA and isinstance(ev.text_delta, str) and ev.text_delta:
                    text_parts.append(ev.text_delta)
                if ev.kind is LLMStreamEventKind.THINKING_DELTA and isinstance(ev.thinking_delta, str) and ev.thinking_delta:
                    thinking_parts.append(ev.thinking_delta)
                if ev.kind is LLMStreamEventKind.TOOL_CALL and ev.tool_call is not None:
                    tool_calls.append(ev.tool_call)
                if ev.kind is not None and ev.kind.value == "completed":
                    final = ev.response
        except openai.OpenAIError as e:
            if trace is not None:
                try:
                    trace.record_error(e, code=classify_provider_exception(e).value)  # type: ignore[name-defined]
                except Exception:
                    pass
            raise wrap_provider_exception(
                e,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                operation="complete",
            ) from e
        except ProviderAdapterError as e:
            if trace is not None:
                try:
                    trace.record_error(e, code=LLMErrorCode.RESPONSE_VALIDATION.value)  # type: ignore[attr-defined]
                except Exception:
                    pass
            raise LLMRequestError(
                str(e),
                code=LLMErrorCode.RESPONSE_VALIDATION,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                retryable=True,
                details={"operation": "complete", "phase": "response_parse"},
                cause=e,
            ) from e
        except Exception as e:
            if trace is not None:
                try:
                    trace.record_error(e, code=LLMErrorCode.NETWORK_ERROR.value)  # best-effort
                except Exception:
                    pass
            raise _wrap_httpx_like_exception(
                e,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                operation="complete",
            ) from e
        if final is None:
            raise LLMRequestError(
                "Provider stream ended without a terminal event.",
                code=LLMErrorCode.TIMEOUT,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                retryable=True,
                details={"operation": "complete"},
            )
        merged_tool_calls = _dedupe_tool_calls(tool_calls)
        return replace(
            final,
            text=(final.text if final.text else "".join(text_parts)),
            thinking=(final.thinking if (isinstance(final.thinking, str) and final.thinking.strip()) else ("".join(thinking_parts) if thinking_parts else None)),
            tool_calls=(_dedupe_tool_calls([*(final.tool_calls or []), *merged_tool_calls]) if merged_tool_calls else final.tool_calls),
        )

    payload = OpenAICompatibleAdapter().prepare_request(
        replace(profile, provider_kind=ProviderKind.OPENAI_COMPATIBLE), request
    ).json
    if trace is not None:
        trace.record_prepared_request(
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            base_url=profile.base_url,
            model=profile.model_name,
            stream=False,
            timeout_s=request_timeout_s,
            payload=payload,
        )
    try:
        resp = client.chat.completions.create(**payload, timeout=request_timeout_s)
    except openai.OpenAIError as e:
        raise wrap_provider_exception(
            e,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            operation="complete",
        ) from e
    out = _openai_to_response(profile_id=profile.profile_id, resp=resp)
    return LLMResponse(
        provider_kind=ProviderKind.OPENAI_CODEX,
        profile_id=out.profile_id,
        model=out.model,
        text=out.text,
        tool_calls=out.tool_calls,
        usage=out.usage,
        stop_reason=out.stop_reason,
        request_id=out.request_id,
    )


def stream_openai_codex(
    *,
    profile,
    request: CanonicalRequest,
    timeout_s: float | None,
    cancel: CancellationToken | None,
    trace: LLMTrace | None,
) -> Iterator[LLMStreamEvent]:
    from . import client as _client_mod

    openai = _client_mod.openai
    OpenAI = _client_mod.OpenAI

    _assert_no_reserved_params(
        profile_id=profile.profile_id,
        provider_kind=profile.provider_kind,
        profile_default_params=profile.default_params,
        request_params=request.params,
        reserved_keys={"model", "messages", "tools", "stream", "timeout", "input", "instructions", "store"},
    )
    if profile.credential_ref is None:
        raise LLMRequestError(
            "Missing credentials for openai_codex profile.",
            code=LLMErrorCode.AUTH,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            retryable=False,
            details={"operation": "stream", "missing": "credential_ref"},
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
            details={"operation": "stream", "credential_ref": getattr(e, "credential_ref", None)},
            cause=e,
        ) from e

    account_id = _codex_auth_account_id_if_available(profile.credential_ref)
    headers = _codex_default_headers(token, auth_json_account_id=account_id) if _is_chatgpt_codex_route(profile.base_url) else {}
    client = OpenAI(api_key=token, base_url=profile.base_url, max_retries=0, default_headers=headers or None)

    request_timeout_s = timeout_s if timeout_s is not None else profile.timeout_s

    use_responses = _is_chatgpt_codex_route(profile.base_url) or _token_supports_openai_responses(token)
    if use_responses:
        def _fallback_stream_chat_completions() -> Iterator[LLMStreamEvent]:
            payload = OpenAICompatibleAdapter().prepare_request(
                replace(profile, provider_kind=ProviderKind.OPENAI_COMPATIBLE), request
            ).json
            payload["stream"] = True
            if trace is not None:
                trace.record_prepared_request(
                    provider_kind=profile.provider_kind,
                    profile_id=profile.profile_id,
                    base_url=profile.base_url,
                    model=profile.model_name,
                    stream=True,
                    timeout_s=request_timeout_s,
                    payload=payload,
                )
            try:
                raw_stream = client.chat.completions.create(**payload, timeout=request_timeout_s)
            except openai.PermissionDeniedError as e:
                if _should_retry_blocked_without_tools(request=request):
                    fallback_req = _blocked_fallback_request(request)
                    fallback_payload = OpenAICompatibleAdapter().prepare_request(
                        replace(profile, provider_kind=ProviderKind.OPENAI_COMPATIBLE), fallback_req
                    ).json
                    fallback_payload["stream"] = True
                    if trace is not None:
                        try:
                            trace.write_json(
                                "prepared_request_fallback.json",
                                {
                                    "reason": "permission_blocked",
                                    "strategy": "minimal_instructions_no_tools",
                                    "endpoint": "chat.completions",
                                    "payload": fallback_payload,
                                },
                            )
                            trace.record_meta(fallback="permission_blocked:minimal_instructions_no_tools:chat_completions")
                        except Exception:
                            pass
                    try:
                        raw_stream = client.chat.completions.create(**fallback_payload, timeout=request_timeout_s)
                    except Exception as e2:
                        raise wrap_provider_exception(
                            e2,
                            provider_kind=profile.provider_kind,
                            profile_id=profile.profile_id,
                            model=profile.model_name,
                            operation="stream",
                        ) from e2
                else:
                    raise wrap_provider_exception(
                        e,
                        provider_kind=profile.provider_kind,
                        profile_id=profile.profile_id,
                        model=profile.model_name,
                        operation="stream",
                    ) from e
            except openai.OpenAIError as e:
                raise wrap_provider_exception(
                    e,
                    provider_kind=profile.provider_kind,
                    profile_id=profile.profile_id,
                    model=profile.model_name,
                    operation="stream",
                ) from e
            except Exception as e:
                raise wrap_provider_exception(
                    e,
                    provider_kind=profile.provider_kind,
                    profile_id=profile.profile_id,
                    model=profile.model_name,
                    operation="stream",
                ) from e

            stop_closer = _start_cancel_closer(cancel, raw_stream)
            wd_stop, wd_timed_out, wd_tick, wd_phase = _start_stream_idle_watchdog(
                stream=raw_stream,
                cancel=cancel,
                first_event_timeout_s=(None if request_timeout_s is None else float(request_timeout_s)),
                idle_timeout_s=(None if request_timeout_s is None else float(request_timeout_s)),
            )
            try:
                for ev in _openai_stream_to_events(
                    profile_id=profile.profile_id,
                    stream=raw_stream,
                    timeout_flag=wd_timed_out,
                    on_chunk=wd_tick,
                    on_provider_chunk=(None if trace is None else trace.record_provider_item),
                ):
                    if ev.kind.value == "completed" and ev.response is not None:
                        ev = LLMStreamEvent(kind=ev.kind, response=replace(ev.response, provider_kind=ProviderKind.OPENAI_CODEX))
                    if trace is not None:
                        trace.record_stream_event(ev)
                    yield ev
                if wd_timed_out.is_set():
                    phase = wd_phase()
                    raise LLMRequestError(
                        (
                            "Stream timed out waiting for first stream chunk."
                            if phase == "first_event"
                            else "Stream timed out (no terminal event / idle)."
                        ),
                        code=LLMErrorCode.TIMEOUT,
                        provider_kind=profile.provider_kind,
                        profile_id=profile.profile_id,
                        model=profile.model_name,
                        retryable=True,
                        details={"operation": "stream", "timeout_s": request_timeout_s, "phase": phase},
                    )
            finally:
                try:
                    wd_stop()
                except Exception:
                    pass
                try:
                    stop_closer()
                except Exception:
                    pass
                _maybe_close_stream(raw_stream)

        payload = OpenAICodexAdapter().prepare_request(profile, request).json
        if not (isinstance(payload.get("instructions"), str) and payload["instructions"].strip()):
            payload["instructions"] = "You are a helpful assistant."
        payload["store"] = False
        use_httpx = _should_use_httpx_for_responses(
            base_url=profile.base_url,
            credential_ref_kind=getattr(profile.credential_ref, "kind", None),
        )
        if use_httpx:
            # Non-streaming HTTPX fallback (yields a single terminal event). This avoids gateway blocks on OpenAI SDK.
            prepared = OpenAICodexAdapter().prepare_request(profile, request)
            http_headers = dict(prepared.headers)
            http_headers.setdefault("Accept", "text/event-stream")
            try_payload = dict(payload)
            try_payload["stream"] = True
            if trace is not None:
                trace.record_prepared_request(
                    provider_kind=profile.provider_kind,
                    profile_id=profile.profile_id,
                    base_url=profile.base_url,
                    model=profile.model_name,
                    stream=True,
                    timeout_s=request_timeout_s,
                    payload=try_payload,
                )

            try:
                yield from _responses_sse_dicts_to_events(
                    provider_kind=ProviderKind.OPENAI_CODEX,
                    profile_id=profile.profile_id,
                    events=_httpx_stream_responses(
                        url=prepared.url,
                        headers=http_headers,
                        payload=try_payload,
                        timeout_s=request_timeout_s,
                        trace=trace,
                        provider_kind=profile.provider_kind,
                        profile_id=profile.profile_id,
                        model=profile.model_name,
                        operation="stream",
                    ),
                    on_provider_chunk=(None if trace is None else trace.record_provider_item),
                )
                return
            except LLMRequestError as e:
                if trace is not None:
                    try:
                        trace.record_error(e, code=e.code.value)
                    except Exception:
                        pass
                if (
                    not _is_openai_platform_base_url(profile.base_url)
                    and e.code in {LLMErrorCode.BAD_REQUEST, LLMErrorCode.NOT_FOUND, LLMErrorCode.UNPROCESSABLE}
                ):
                    if trace is not None:
                        try:
                            trace.record_meta(fallback="responses_unavailable:chat_completions")
                        except Exception:
                            pass
                    yield from _fallback_stream_chat_completions()
                    return
                if e.code == LLMErrorCode.PERMISSION and _should_retry_blocked_without_tools(request=request):
                    fallback_req = _blocked_fallback_request(request)
                    fallback_payload = OpenAICodexAdapter().prepare_request(profile, fallback_req).json
                    fallback_payload["store"] = False
                    fallback_payload["stream"] = True
                    if trace is not None:
                        try:
                            trace.write_json(
                                "prepared_request_fallback.json",
                                {
                                    "reason": "permission_blocked",
                                    "strategy": "minimal_instructions_no_tools",
                                    "payload": fallback_payload,
                                },
                            )
                            trace.record_meta(fallback="permission_blocked:minimal_instructions_no_tools")
                        except Exception:
                            pass
                    yield from _responses_sse_dicts_to_events(
                        provider_kind=ProviderKind.OPENAI_CODEX,
                        profile_id=profile.profile_id,
                        events=_httpx_stream_responses(
                            url=prepared.url,
                            headers=http_headers,
                            payload=fallback_payload,
                            timeout_s=request_timeout_s,
                            trace=trace,
                            provider_kind=profile.provider_kind,
                            profile_id=profile.profile_id,
                            model=profile.model_name,
                            operation="stream",
                        ),
                        on_provider_chunk=(None if trace is None else trace.record_provider_item),
                    )
                    return
                raise
        if trace is not None:
            trace.record_prepared_request(
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                base_url=profile.base_url,
                model=profile.model_name,
                stream=True,
                timeout_s=request_timeout_s,
                payload=payload,
            )
        try:
            raw_stream = client.responses.create(**payload, stream=True, timeout=request_timeout_s)
        except (openai.BadRequestError, openai.NotFoundError, openai.UnprocessableEntityError) as e:
            # Many OpenAI-compatible gateways do not implement the Responses API. Fall back to chat.completions.
            if not _is_openai_platform_base_url(profile.base_url):
                if trace is not None:
                    try:
                        trace.record_error(e, code=classify_provider_exception(e).value)  # type: ignore[name-defined]
                        trace.record_meta(fallback="responses_unavailable:chat_completions")
                    except Exception:
                        pass
                yield from _fallback_stream_chat_completions()
                return
            raise wrap_provider_exception(
                e,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                operation="stream",
            ) from e
        except openai.PermissionDeniedError as e:
            if _should_retry_blocked_without_tools(request=request):
                fallback_req = _blocked_fallback_request(request)
                fallback_payload = OpenAICodexAdapter().prepare_request(profile, fallback_req).json
                fallback_payload["store"] = False
                if not (
                    isinstance(fallback_payload.get("instructions"), str) and fallback_payload["instructions"].strip()
                ):
                    fallback_payload["instructions"] = "You are a helpful assistant."
                if trace is not None:
                    try:
                        trace.write_json(
                            "prepared_request_fallback.json",
                            {
                                "reason": "permission_blocked",
                                "strategy": "minimal_instructions_no_tools",
                                "payload": fallback_payload,
                            },
                        )
                        trace.record_meta(fallback="permission_blocked:minimal_instructions_no_tools")
                    except Exception:
                        pass
                try:
                    raw_stream = client.responses.create(**fallback_payload, stream=True, timeout=request_timeout_s)
                except openai.OpenAIError as e2:
                    if trace is not None:
                        try:
                            trace.record_error(e2, code=classify_provider_exception(e2).value)  # type: ignore[name-defined]
                        except Exception:
                            pass
                    raise wrap_provider_exception(
                        e2,
                        provider_kind=profile.provider_kind,
                        profile_id=profile.profile_id,
                        model=profile.model_name,
                        operation="stream",
                    ) from e2
            else:
                if trace is not None:
                    try:
                        trace.record_error(e, code=classify_provider_exception(e).value)  # type: ignore[name-defined]
                    except Exception:
                        pass
                raise wrap_provider_exception(
                    e,
                    provider_kind=profile.provider_kind,
                    profile_id=profile.profile_id,
                    model=profile.model_name,
                    operation="stream",
                ) from e
        except openai.OpenAIError as e:
            if trace is not None:
                try:
                    trace.record_error(e, code=classify_provider_exception(e).value)  # type: ignore[name-defined]
                except Exception:
                    pass
            raise wrap_provider_exception(
                e,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                operation="stream",
            ) from e

        stop_closer = _start_cancel_closer(cancel, raw_stream)
        wd_stop, wd_timed_out, wd_tick, wd_phase = _start_stream_idle_watchdog(
            stream=raw_stream,
            cancel=cancel,
            first_event_timeout_s=(None if request_timeout_s is None else float(request_timeout_s)),
            idle_timeout_s=(None if request_timeout_s is None else float(request_timeout_s)),
        )
        try:
            for ev in _responses_stream_to_events(
                provider_kind=ProviderKind.OPENAI_CODEX,
                profile_id=profile.profile_id,
                stream=raw_stream,
                timeout_flag=wd_timed_out,
                on_chunk=wd_tick,
                on_provider_chunk=(None if trace is None else trace.record_provider_item),
            ):
                _raise_if_cancelled(
                    cancel,
                    provider_kind=profile.provider_kind,
                    profile_id=profile.profile_id,
                    model=profile.model_name,
                    operation="stream",
                )
                if trace is not None:
                    trace.record_stream_event(ev)
                yield ev
            if wd_timed_out.is_set():
                phase = wd_phase()
                raise LLMRequestError(
                    (
                        "Stream timed out waiting for first stream chunk."
                        if phase == "first_event"
                        else "Stream timed out (no terminal event / idle)."
                    ),
                    code=LLMErrorCode.TIMEOUT,
                    provider_kind=profile.provider_kind,
                    profile_id=profile.profile_id,
                    model=profile.model_name,
                    retryable=True,
                    details={"operation": "stream", "timeout_s": request_timeout_s, "phase": phase},
                )
        except openai.OpenAIError as e:
            if trace is not None:
                try:
                    trace.record_error(e, code=classify_provider_exception(e).value)  # type: ignore[name-defined]
                except Exception:
                    pass
            raise wrap_provider_exception(
                e,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                operation="stream",
            ) from e
        except ProviderAdapterError as e:
            if trace is not None:
                try:
                    trace.record_error(e, code=LLMErrorCode.RESPONSE_VALIDATION.value)  # type: ignore[attr-defined]
                except Exception:
                    pass
            raise LLMRequestError(
                str(e),
                code=LLMErrorCode.RESPONSE_VALIDATION,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                retryable=True,
                details={"operation": "stream", "phase": "response_parse"},
                cause=e,
            ) from e
        except Exception as e:
            if trace is not None:
                try:
                    trace.record_error(e, code=LLMErrorCode.NETWORK_ERROR.value)  # best-effort
                except Exception:
                    pass
            raise _wrap_httpx_like_exception(
                e,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                operation="stream",
            ) from e
        finally:
            try:
                wd_stop()
            except Exception:
                pass
            try:
                stop_closer()
            except Exception:
                pass
            _maybe_close_stream(raw_stream)
        return

    payload = OpenAICompatibleAdapter().prepare_request(
        replace(profile, provider_kind=ProviderKind.OPENAI_COMPATIBLE), request
    ).json
    payload["stream"] = True
    if trace is not None:
        trace.record_prepared_request(
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            base_url=profile.base_url,
            model=profile.model_name,
            stream=True,
            timeout_s=request_timeout_s,
            payload=payload,
        )
    try:
        raw_stream = client.chat.completions.create(**payload, timeout=request_timeout_s)
    except openai.PermissionDeniedError as e:
        if _should_retry_blocked_without_tools(request=request):
            fallback_req = _blocked_fallback_request(request)
            fallback_payload = OpenAICompatibleAdapter().prepare_request(
                replace(profile, provider_kind=ProviderKind.OPENAI_COMPATIBLE), fallback_req
            ).json
            fallback_payload["stream"] = True
            if trace is not None:
                try:
                    trace.write_json(
                        "prepared_request_fallback.json",
                        {
                            "reason": "permission_blocked",
                            "strategy": "minimal_instructions_no_tools",
                            "endpoint": "chat.completions",
                            "payload": fallback_payload,
                        },
                    )
                    trace.record_meta(fallback="permission_blocked:minimal_instructions_no_tools:chat_completions")
                except Exception:
                    pass
            try:
                raw_stream = client.chat.completions.create(**fallback_payload, timeout=request_timeout_s)
            except Exception as e2:
                raise wrap_provider_exception(
                    e2,
                    provider_kind=profile.provider_kind,
                    profile_id=profile.profile_id,
                    model=profile.model_name,
                    operation="stream",
                ) from e2
        else:
            raise wrap_provider_exception(
                e,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                operation="stream",
            ) from e
    except openai.OpenAIError as e:
        raise wrap_provider_exception(
            e,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            operation="stream",
        ) from e
    except Exception as e:
        raise wrap_provider_exception(
            e,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            operation="stream",
        ) from e

    stop_closer = _start_cancel_closer(cancel, raw_stream)
    wd_stop, wd_timed_out, wd_tick, wd_phase = _start_stream_idle_watchdog(
        stream=raw_stream,
        cancel=cancel,
        first_event_timeout_s=(None if request_timeout_s is None else float(request_timeout_s)),
        idle_timeout_s=(None if request_timeout_s is None else float(request_timeout_s)),
    )
    try:
        for ev in _openai_stream_to_events(
            profile_id=profile.profile_id,
            stream=raw_stream,
            timeout_flag=wd_timed_out,
            on_chunk=wd_tick,
            on_provider_chunk=(None if trace is None else trace.record_provider_item),
        ):
            if ev.kind.value == "completed" and ev.response is not None:
                ev = LLMStreamEvent(kind=ev.kind, response=replace(ev.response, provider_kind=ProviderKind.OPENAI_CODEX))
            if trace is not None:
                trace.record_stream_event(ev)
            yield ev
        if wd_timed_out.is_set():
            phase = wd_phase()
            raise LLMRequestError(
                (
                    "Stream timed out waiting for first stream chunk."
                    if phase == "first_event"
                    else "Stream timed out (no terminal event / idle)."
                ),
                code=LLMErrorCode.TIMEOUT,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                retryable=True,
                details={"operation": "stream", "timeout_s": request_timeout_s, "phase": phase},
            )
    finally:
        try:
            wd_stop()
        except Exception:
            pass
        try:
            stop_closer()
        except Exception:
            pass
        _maybe_close_stream(raw_stream)
