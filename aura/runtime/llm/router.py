from __future__ import annotations

from dataclasses import dataclass

from .config import ModelConfig
from .errors import ModelResolutionError
from .types import ModelCapabilities, ModelProfile, ModelRequirements, ModelRole
from ..context_mgmt import resolve_context_limit_tokens


@dataclass(frozen=True)
class ResolvedModel:
    role: ModelRole
    profile: ModelProfile
    requirements: ModelRequirements
    why: str


class ModelRouter:
    def __init__(self, config: ModelConfig) -> None:
        self._config = config

    def set_config(self, config: ModelConfig) -> None:
        """
        Replace the underlying ModelConfig in-place.

        Important: Engines/tools may hold a long-lived reference to a ModelRouter
        instance. Updating the config in-place ensures those references observe
        runtime model switches (e.g. /model in the CLI) without needing to
        rebuild and re-register tools.
        """

        self._config = config

    def resolve(self, *, role: ModelRole, requirements: ModelRequirements) -> ResolvedModel:
        profile_id = self._config.role_pointers.get(role)
        if profile_id is None:
            raise ModelResolutionError(
                f"No model configured for role '{role}'.",
                role=str(role),
            )

        profile = self._config.profiles.get(profile_id)
        if profile is None:
            raise ModelResolutionError(
                f"Role '{role}' points to missing profile '{profile_id}'.",
                role=str(role),
                profile_id=profile_id,
            )

        caps = profile.capabilities.with_provider_defaults(profile.provider_kind)
        self._assert_requirements(profile=profile, capabilities=caps, requirements=requirements)

        why = f"role={role} -> profile_id={profile.profile_id} (explicit pointer)"
        return ResolvedModel(role=role, profile=profile, requirements=requirements, why=why)

    @staticmethod
    def _assert_requirements(
        *,
        profile: ModelProfile,
        capabilities: ModelCapabilities,
        requirements: ModelRequirements,
    ) -> None:
        if requirements.needs_streaming and capabilities.supports_streaming is not True:
            raise ModelResolutionError(
                f"Profile '{profile.profile_id}' does not support streaming.",
                role=None,
                profile_id=profile.profile_id,
            )
        if requirements.needs_tools and capabilities.supports_tools is not True:
            raise ModelResolutionError(
                f"Profile '{profile.profile_id}' does not support tools.",
                role=None,
                profile_id=profile.profile_id,
            )
        if (
            requirements.needs_structured_output
            and capabilities.supports_structured_output is not True
        ):
            raise ModelResolutionError(
                f"Profile '{profile.profile_id}' does not support structured output.",
                role=None,
                profile_id=profile.profile_id,
            )
        if requirements.min_context_tokens is not None:
            limit = resolve_context_limit_tokens(
                profile.limits.context_limit_tokens if profile.limits is not None else None
            )
            if limit < requirements.min_context_tokens:
                raise ModelResolutionError(
                    f"Profile '{profile.profile_id}' context limit is too small.",
                    role=None,
                    profile_id=profile.profile_id,
                )
