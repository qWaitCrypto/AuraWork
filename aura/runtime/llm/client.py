from __future__ import annotations

from typing import Iterator

from .client_common import _merge_requirements, _raise_if_cancelled
from .config import ModelConfig
from .errors import CancellationToken, LLMErrorCode, LLMRequestError, ProviderAdapterError
from .router import ModelRouter
from .trace import LLMTrace
from .types import CanonicalRequest, LLMResponse, LLMStreamEvent, ModelRequirements, ModelRole, ProviderKind


class LLMClient:
    def __init__(self, config: ModelConfig) -> None:
        self._router = ModelRouter(config)

    @staticmethod
    def _assert_profile_base_url(*, profile: "ModelProfile", operation: str) -> None:
        if not profile.base_url.strip():
            raise LLMRequestError(
                "Model profile base_url is empty. Edit .aura/config/models.json and set base_url for the active profile.",
                code=LLMErrorCode.BAD_REQUEST,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                retryable=False,
                details={"operation": operation, "missing": "base_url"},
            )

    def complete(
        self,
        *,
        role: ModelRole,
        requirements: ModelRequirements,
        request: CanonicalRequest,
        timeout_s: float | None = None,
        cancel: CancellationToken | None = None,
        trace: LLMTrace | None = None,
    ) -> LLMResponse:
        if requirements.needs_streaming:
            raise ProviderAdapterError("complete() does not support needs_streaming=True; use stream().")

        effective = _merge_requirements(requirements, request=request, force_streaming=False)
        resolved = self._router.resolve(role=role, requirements=effective)
        profile = resolved.profile
        self._assert_profile_base_url(profile=profile, operation="complete")
        if trace is not None:
            trace.record_canonical_request(request)
        _raise_if_cancelled(
            cancel,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            operation="complete",
        )

        if profile.provider_kind is ProviderKind.OPENAI_COMPATIBLE:
            from .client_exec_openai_compatible import complete_openai_compatible

            return complete_openai_compatible(
                profile=profile,
                request=request,
                timeout_s=timeout_s,
                cancel=cancel,
                trace=trace,
            )

        if profile.provider_kind is ProviderKind.OPENAI_CODEX:
            from .client_exec_openai_codex import complete_openai_codex

            return complete_openai_codex(
                profile=profile,
                request=request,
                timeout_s=timeout_s,
                cancel=cancel,
                trace=trace,
            )

        if profile.provider_kind is ProviderKind.GEMINI:
            from .client_exec_gemini import complete_gemini

            return complete_gemini(
                profile=profile,
                request=request,
                timeout_s=timeout_s,
                cancel=cancel,
                trace=trace,
            )

        if profile.provider_kind is ProviderKind.ANTHROPIC:
            from .client_exec_anthropic import complete_anthropic

            return complete_anthropic(
                profile=profile,
                request=request,
                timeout_s=timeout_s,
                cancel=cancel,
                trace=trace,
            )

        raise ProviderAdapterError(f"Unsupported provider_kind: {profile.provider_kind}")

    def stream(
        self,
        *,
        role: ModelRole,
        requirements: ModelRequirements,
        request: CanonicalRequest,
        timeout_s: float | None = None,
        cancel: CancellationToken | None = None,
        trace: LLMTrace | None = None,
    ) -> Iterator[LLMStreamEvent]:
        effective = _merge_requirements(requirements, request=request, force_streaming=True)
        resolved = self._router.resolve(role=role, requirements=effective)
        profile = resolved.profile
        self._assert_profile_base_url(profile=profile, operation="stream")
        if trace is not None:
            trace.record_canonical_request(request)
        _raise_if_cancelled(
            cancel,
            provider_kind=profile.provider_kind,
            profile_id=profile.profile_id,
            model=profile.model_name,
            operation="stream",
        )

        if profile.provider_kind is ProviderKind.OPENAI_COMPATIBLE:
            from .client_exec_openai_compatible import stream_openai_compatible

            yield from stream_openai_compatible(
                profile=profile,
                request=request,
                timeout_s=timeout_s,
                cancel=cancel,
                trace=trace,
            )
            return

        if profile.provider_kind is ProviderKind.OPENAI_CODEX:
            from .client_exec_openai_codex import stream_openai_codex

            yield from stream_openai_codex(
                profile=profile,
                request=request,
                timeout_s=timeout_s,
                cancel=cancel,
                trace=trace,
            )
            return

        if profile.provider_kind is ProviderKind.GEMINI:
            from .client_exec_gemini import stream_gemini

            yield from stream_gemini(
                profile=profile,
                request=request,
                timeout_s=timeout_s,
                cancel=cancel,
                trace=trace,
            )
            return

        if profile.provider_kind is ProviderKind.ANTHROPIC:
            from .client_exec_anthropic import stream_anthropic

            yield from stream_anthropic(
                profile=profile,
                request=request,
                timeout_s=timeout_s,
                cancel=cancel,
                trace=trace,
            )
            return

        raise ProviderAdapterError(f"Unsupported provider_kind: {profile.provider_kind}")


def __getattr__(name: str):  # pragma: no cover
    # Lazily expose provider SDK modules/classes for exec adapters. This keeps
    # `aura` import/startup fast while preserving the previous access pattern:
    # `from . import client as _client_mod; _client_mod.openai`.
    if name in {"openai", "OpenAI"}:
        try:
            import openai as _openai  # type: ignore
            from openai import OpenAI as _OpenAI  # type: ignore
        except Exception as e:  # pragma: no cover
            raise AttributeError(f"{__name__} has no attribute {name!r} (openai import failed: {e})") from e
        globals()["openai"] = _openai
        globals()["OpenAI"] = _OpenAI
        return globals()[name]

    if name in {"anthropic", "Anthropic"}:
        try:
            import anthropic as _anthropic  # type: ignore
            from anthropic import Anthropic as _Anthropic  # type: ignore
        except Exception as e:  # pragma: no cover
            raise AttributeError(f"{__name__} has no attribute {name!r} (anthropic import failed: {e})") from e
        globals()["anthropic"] = _anthropic
        globals()["Anthropic"] = _Anthropic
        return globals()[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
