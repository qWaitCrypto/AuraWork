from __future__ import annotations

import json
import threading
from enum import StrEnum
from typing import Any

from ..error_codes import ErrorCode
from .types import ProviderKind


class ModelConfigError(ValueError):
    pass


class ModelResolutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        role: str | None = None,
        profile_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.role = role
        self.profile_id = profile_id


class CredentialResolutionError(RuntimeError):
    def __init__(self, message: str, *, credential_ref: str | None = None) -> None:
        super().__init__(message)
        self.credential_ref = credential_ref


class ProviderAdapterError(RuntimeError):
    pass


LLMErrorCode = ErrorCode


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class LLMRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: LLMErrorCode,
        provider_kind: ProviderKind | None = None,
        profile_id: str | None = None,
        model: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_kind = provider_kind
        self.profile_id = profile_id
        self.model = model
        self.status_code = status_code
        self.request_id = request_id
        self.retryable = retryable
        self.details = details
        self.__cause__ = cause


def is_retryable_error_code(code: LLMErrorCode) -> bool:
    return code in {
        LLMErrorCode.TIMEOUT,
        LLMErrorCode.RATE_LIMIT,
        LLMErrorCode.SERVER_ERROR,
        LLMErrorCode.NETWORK_ERROR,
    }


def classify_provider_exception(exc: BaseException) -> LLMErrorCode:
    if isinstance(exc, LLMRequestError):
        return exc.code

    openai = _maybe_import_openai()
    anthropic = _maybe_import_anthropic()

    if openai is not None:
        if isinstance(exc, openai.APITimeoutError):
            return LLMErrorCode.TIMEOUT
        if isinstance(exc, openai.APIConnectionError):
            return LLMErrorCode.NETWORK_ERROR
        if isinstance(exc, openai.RateLimitError):
            return LLMErrorCode.RATE_LIMIT
        if isinstance(exc, openai.AuthenticationError):
            return LLMErrorCode.AUTH
        if isinstance(exc, openai.PermissionDeniedError):
            return LLMErrorCode.PERMISSION
        if isinstance(exc, openai.NotFoundError):
            return LLMErrorCode.NOT_FOUND
        if isinstance(exc, openai.ConflictError):
            return LLMErrorCode.CONFLICT
        if isinstance(exc, openai.UnprocessableEntityError):
            return LLMErrorCode.UNPROCESSABLE
        if isinstance(exc, openai.BadRequestError):
            return LLMErrorCode.BAD_REQUEST
        if isinstance(exc, openai.InternalServerError):
            return LLMErrorCode.SERVER_ERROR
        if isinstance(exc, openai.APIResponseValidationError):
            return LLMErrorCode.RESPONSE_VALIDATION

    if anthropic is not None:
        if isinstance(exc, anthropic.APITimeoutError):
            return LLMErrorCode.TIMEOUT
        if isinstance(exc, anthropic.APIConnectionError):
            return LLMErrorCode.NETWORK_ERROR
        if isinstance(exc, anthropic.RateLimitError):
            return LLMErrorCode.RATE_LIMIT
        if isinstance(exc, anthropic.AuthenticationError):
            return LLMErrorCode.AUTH
        if isinstance(exc, anthropic.PermissionDeniedError):
            return LLMErrorCode.PERMISSION
        if isinstance(exc, anthropic.NotFoundError):
            return LLMErrorCode.NOT_FOUND
        if isinstance(exc, anthropic.ConflictError):
            return LLMErrorCode.CONFLICT
        if isinstance(exc, anthropic.UnprocessableEntityError):
            return LLMErrorCode.UNPROCESSABLE
        if isinstance(exc, anthropic.BadRequestError):
            return LLMErrorCode.BAD_REQUEST
        if isinstance(exc, anthropic.InternalServerError):
            return LLMErrorCode.SERVER_ERROR
        if isinstance(exc, anthropic.APIResponseValidationError):
            return LLMErrorCode.RESPONSE_VALIDATION

    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        if status_code == 400:
            return LLMErrorCode.BAD_REQUEST
        if status_code == 401:
            return LLMErrorCode.AUTH
        if status_code == 403:
            return LLMErrorCode.PERMISSION
        if status_code == 404:
            return LLMErrorCode.NOT_FOUND
        if status_code == 409:
            return LLMErrorCode.CONFLICT
        if status_code == 422:
            return LLMErrorCode.UNPROCESSABLE
        if status_code == 429:
            return LLMErrorCode.RATE_LIMIT
        if 500 <= status_code <= 599:
            return LLMErrorCode.SERVER_ERROR

    return LLMErrorCode.UNKNOWN


def wrap_provider_exception(
    exc: BaseException,
    *,
    provider_kind: ProviderKind,
    profile_id: str,
    model: str | None,
    operation: str,
) -> LLMRequestError:
    code = classify_provider_exception(exc)
    status_code = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None)
    retryable = is_retryable_error_code(code)
    message = str(exc) or exc.__class__.__name__
    extra = _provider_error_detail(exc)
    if extra:
        # Avoid repeating identical strings.
        if extra not in message:
            message = f"{message}: {extra}"
    details = {"operation": operation}
    return LLMRequestError(
        message,
        code=code,
        provider_kind=provider_kind,
        profile_id=profile_id,
        model=model,
        status_code=status_code if isinstance(status_code, int) else None,
        request_id=request_id if isinstance(request_id, str) else None,
        retryable=retryable,
        details=details,
        cause=exc,
    )


def _provider_error_detail(exc: BaseException) -> str | None:
    openai = _maybe_import_openai()
    anthropic = _maybe_import_anthropic()

    # OpenAI SDK HTTP errors often include a structured body with the real error message.
    if openai is not None and isinstance(exc, openai.OpenAIError):
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                msg = err.get("message")
                typ = err.get("type")
                param = err.get("param")
                code = err.get("code")
                parts: list[str] = []
                if isinstance(msg, str) and msg.strip():
                    parts.append(msg.strip())
                meta: list[str] = []
                if isinstance(typ, str) and typ.strip():
                    meta.append(f"type={typ.strip()}")
                if isinstance(param, str) and param.strip():
                    meta.append(f"param={param.strip()}")
                if isinstance(code, str) and code.strip():
                    meta.append(f"code={code.strip()}")
                if meta:
                    parts.append("(" + ", ".join(meta) + ")")
                if parts:
                    return " ".join(parts).strip()
            # Fall back to a compact JSON dump.
            return _truncate(_safe_json_dumps(body), 2000)
        if isinstance(body, str) and body.strip():
            return _truncate(body.strip(), 2000)
        return None

    # Anthropic SDK errors may also carry response information; keep best-effort.
    if anthropic is not None and isinstance(exc, anthropic.AnthropicError):
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            msg = body.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
            return _truncate(_safe_json_dumps(body), 2000)
        if isinstance(body, str) and body.strip():
            return _truncate(body.strip(), 2000)
        return None

    return None


def _safe_json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except Exception:
        return repr(obj)


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "…"


def _maybe_import_openai():
    try:
        import openai  # type: ignore

        return openai
    except Exception:
        return None


def _maybe_import_anthropic():
    try:
        import anthropic  # type: ignore

        return anthropic
    except Exception:
        return None
