from __future__ import annotations

from .errors import LLMErrorCode, LLMRequestError
from .types import ProviderKind


def _wrap_httpx_like_exception(
    exc: BaseException,
    *,
    provider_kind: ProviderKind,
    profile_id: str,
    model: str | None,
    operation: str,
) -> LLMRequestError:
    try:
        import httpx  # type: ignore

        if isinstance(exc, httpx.TimeoutException):
            code = LLMErrorCode.TIMEOUT
            status_code = None
        elif isinstance(exc, httpx.NetworkError):
            code = LLMErrorCode.NETWORK_ERROR
            status_code = None
        elif isinstance(exc, httpx.HTTPStatusError):
            status_code = int(exc.response.status_code)
            body_snippet: str | None = None
            try:
                text = exc.response.text
                if isinstance(text, str) and text.strip():
                    body_snippet = text.strip()[:2000]
            except Exception:
                body_snippet = None
            code = LLMErrorCode.UNKNOWN
            if status_code == 400:
                code = LLMErrorCode.BAD_REQUEST
            elif status_code == 401:
                code = LLMErrorCode.AUTH
            elif status_code == 403:
                code = LLMErrorCode.PERMISSION
            elif status_code == 404:
                code = LLMErrorCode.NOT_FOUND
            elif status_code == 409:
                code = LLMErrorCode.CONFLICT
            elif status_code == 422:
                code = LLMErrorCode.UNPROCESSABLE
            elif status_code == 429:
                code = LLMErrorCode.RATE_LIMIT
            elif 500 <= status_code <= 599:
                code = LLMErrorCode.SERVER_ERROR
        else:
            code = LLMErrorCode.UNKNOWN
            status_code = None
    except Exception:
        code = LLMErrorCode.UNKNOWN
        status_code = None

    retryable = code in {
        LLMErrorCode.TIMEOUT,
        LLMErrorCode.RATE_LIMIT,
        LLMErrorCode.SERVER_ERROR,
        LLMErrorCode.NETWORK_ERROR,
    }
    message = str(exc) or exc.__class__.__name__
    try:
        if "body_snippet" in locals() and isinstance(body_snippet, str) and body_snippet:
            message = f"{message}\n\nProvider response (truncated):\n{body_snippet}"
    except Exception:
        pass
    return LLMRequestError(
        message,
        code=code,
        provider_kind=provider_kind,
        profile_id=profile_id,
        model=model,
        status_code=status_code,
        request_id=None,
        retryable=retryable,
        details={"operation": operation},
        cause=exc,
    )
