from __future__ import annotations

from typing import Any

from .errors import CancellationToken, LLMErrorCode, LLMRequestError, ProviderAdapterError
from .types import CanonicalRequest, ModelRequirements, ProviderKind


def _merge_requirements(
    requirements: ModelRequirements, *, request: CanonicalRequest, force_streaming: bool
) -> ModelRequirements:
    return ModelRequirements(
        needs_streaming=force_streaming or requirements.needs_streaming,
        needs_tools=requirements.needs_tools or bool(request.tools),
        needs_structured_output=requirements.needs_structured_output,
        min_context_tokens=requirements.min_context_tokens,
    )


def _assert_no_reserved_params(
    *,
    profile_id: str,
    provider_kind: ProviderKind,
    profile_default_params: dict[str, Any],
    request_params: dict[str, Any],
    reserved_keys: set[str],
) -> None:
    found: set[str] = set()
    for k in reserved_keys:
        if k in profile_default_params or k in request_params:
            found.add(k)
    if found:
        rendered = ", ".join(sorted(found))
        raise ProviderAdapterError(
            f"Reserved params set in profile/request params for {provider_kind} profile '{profile_id}': {rendered}"
        )


def _raise_if_cancelled(
    cancel: CancellationToken | None,
    *,
    code: LLMErrorCode = LLMErrorCode.CANCELLED,
    provider_kind: ProviderKind,
    profile_id: str,
    model: str | None,
    operation: str,
) -> None:
    if cancel is not None and cancel.cancelled:
        raise LLMRequestError(
            "Request cancelled.",
            code=code,
            provider_kind=provider_kind,
            profile_id=profile_id,
            model=model,
            retryable=False,
            details={"operation": operation},
        )

