from __future__ import annotations

from typing import Any, Iterator

from .client_common import _assert_no_reserved_params, _raise_if_cancelled
from .client_openai import _openai_stream_to_events, _openai_to_response
from .client_stream_guard import _maybe_close_stream, _start_cancel_closer, _start_stream_idle_watchdog
from .errors import (
    CancellationToken,
    CredentialResolutionError,
    LLMErrorCode,
    LLMRequestError,
    ProviderAdapterError,
    wrap_provider_exception,
)
from .providers.openai_compatible import OpenAICompatibleAdapter
from .secrets import resolve_credential
from .trace import LLMTrace
from .types import CanonicalRequest, LLMResponse, LLMStreamEvent


def complete_openai_compatible(
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
        reserved_keys={"model", "messages", "tools", "stream", "timeout"},
    )
    if profile.credential_ref is None:
        raise LLMRequestError(
            "Missing credentials for openai_compatible profile.",
            code=LLMErrorCode.AUTH,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            retryable=False,
            details={"operation": "complete", "missing": "credential_ref"},
        )
    try:
        api_key = resolve_credential(profile.credential_ref)
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
    client = OpenAI(api_key=api_key, base_url=profile.base_url, max_retries=0)
    payload = OpenAICompatibleAdapter().prepare_request(profile, request).json
    request_timeout_s = timeout_s if timeout_s is not None else profile.timeout_s
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
    _raise_if_cancelled(
        cancel,
        provider_kind=profile.provider_kind,
        profile_id=profile.profile_id,
        model=profile.model_name,
        operation="complete",
    )
    try:
        if request_timeout_s is not None:
            resp = client.chat.completions.create(**payload, timeout=request_timeout_s)
        else:
            resp = client.chat.completions.create(**payload)
    except openai.OpenAIError as e:
        if trace is not None:
            try:
                resp = getattr(e, "response", None)
                headers = None
                text = None
                if resp is not None:
                    try:
                        headers = dict(getattr(resp, "headers", {}) or {})
                    except Exception:
                        headers = None
                    try:
                        text = resp.text  # httpx.Response.text
                    except Exception:
                        text = None
                trace.write_json(
                    "provider_error_response.json",
                    {
                        "status_code": getattr(e, "status_code", None),
                        "request_id": getattr(e, "request_id", None),
                        "body": getattr(e, "body", None),
                        "headers": headers,
                        "text": (text[:4000] if isinstance(text, str) else None),
                        "message": str(e),
                    },
                )
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
        raise LLMRequestError(
            str(e),
            code=LLMErrorCode.BAD_REQUEST,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            retryable=False,
            details={"operation": "complete"},
            cause=e,
        ) from e
    if trace is not None:
        trace.write_json("provider_response.json", resp)
    return _openai_to_response(profile_id=profile.profile_id, resp=resp)


def stream_openai_compatible(
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
        reserved_keys={"model", "messages", "tools", "stream", "timeout"},
    )
    if profile.credential_ref is None:
        raise LLMRequestError(
            "Missing credentials for openai_compatible profile.",
            code=LLMErrorCode.AUTH,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            retryable=False,
            details={"operation": "stream", "missing": "credential_ref"},
        )
    try:
        api_key = resolve_credential(profile.credential_ref)
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
    client = OpenAI(api_key=api_key, base_url=profile.base_url, max_retries=0)
    payload = OpenAICompatibleAdapter().prepare_request(profile, request).json
    request_timeout_s = timeout_s if timeout_s is not None else profile.timeout_s

    added_stream_usage = False
    stream_options = payload.get("stream_options")
    if stream_options is None:
        payload["stream_options"] = {"include_usage": True}
        added_stream_usage = True
    elif isinstance(stream_options, dict):
        if stream_options.get("include_usage") is None:
            stream_options["include_usage"] = True
            added_stream_usage = True

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
    timeout_arg: Any | None = None
    if request_timeout_s is not None:
        # Some OpenAI-compatible gateways stall mid-stream. Keep the SDK's socket read
        # timeout aligned with the configured request timeout so users can tune it via
        # `NOVELAIRE_TIMEOUT_S` / `--timeout`.
        try:
            import httpx  # type: ignore

            read_timeout_s = float(request_timeout_s)
            timeout_arg = httpx.Timeout(float(request_timeout_s), read=read_timeout_s)
        except Exception:
            timeout_arg = float(request_timeout_s)
    try:
        if timeout_arg is not None:
            raw_stream = client.chat.completions.create(**payload, stream=True, timeout=timeout_arg)
        else:
            raw_stream = client.chat.completions.create(**payload, stream=True)
    except openai.OpenAIError as e:
        status_code = getattr(e, "status_code", None)
        if added_stream_usage and status_code == 400 and ("stream_options" in str(e) or "include_usage" in str(e)):
            try:
                payload.pop("stream_options", None)
            except Exception:
                pass
            if trace is not None:
                trace.record_meta(stream_include_usage_rejected=True)
            try:
                if timeout_arg is not None:
                    raw_stream = client.chat.completions.create(**payload, stream=True, timeout=timeout_arg)
                else:
                    raw_stream = client.chat.completions.create(**payload, stream=True)
            except openai.OpenAIError as e2:
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
    except ProviderAdapterError as e:
        raise LLMRequestError(
            str(e),
            code=LLMErrorCode.BAD_REQUEST,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            retryable=False,
            details={"operation": "stream"},
            cause=e,
        ) from e

    stop_closer = _start_cancel_closer(cancel, raw_stream)
    wd_stop, wd_timed_out, wd_tick, wd_phase = _start_stream_idle_watchdog(
        stream=raw_stream,
        cancel=cancel,
        first_event_timeout_s=(None if request_timeout_s is None else float(request_timeout_s)),
        # After the first chunk, token streams should stay "chatty". If we go silent,
        # close the stream so the CLI doesn't hang forever on buggy endpoints.
        idle_timeout_s=(None if request_timeout_s is None else float(request_timeout_s)),
    )
    try:
        for event in _openai_stream_to_events(
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
                trace.record_stream_event(event)
            yield event
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
    except ProviderAdapterError as e:
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
    except openai.OpenAIError as e:
        if cancel is not None and cancel.cancelled:
            raise LLMRequestError(
                "Request cancelled.",
                code=LLMErrorCode.CANCELLED,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                retryable=False,
                details={"operation": "stream"},
                cause=e,
            ) from e
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
                cause=e,
            ) from e
        raise wrap_provider_exception(
            e,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            operation="stream",
        ) from e
    finally:
        if stop_closer is not None:
            stop_closer.set()
        wd_stop.set()
        _maybe_close_stream(raw_stream)
