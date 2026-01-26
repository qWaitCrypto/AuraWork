from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ModelConfigError
from .types import ModelProfile, ModelRole


@dataclass(frozen=True)
class ModelConfig:
    profiles: dict[str, ModelProfile] = field(default_factory=dict)
    role_pointers: dict[ModelRole, str] = field(default_factory=dict)

    def get_profile_for_role(self, role: ModelRole) -> ModelProfile | None:
        profile_id = self.role_pointers.get(role)
        if profile_id is None:
            return None
        return self.profiles.get(profile_id)

    def validate_consistency(self) -> None:
        missing = [
            (role, profile_id)
            for role, profile_id in self.role_pointers.items()
            if profile_id not in self.profiles
        ]
        if missing:
            rendered = ", ".join(f"{role} -> {profile_id}" for role, profile_id in missing)
            raise ModelConfigError(f"Role pointers reference missing profiles: {rendered}")

    def merge_over(self, base: "ModelConfig") -> "ModelConfig":
        merged_profiles = dict(base.profiles)
        merged_profiles.update(self.profiles)

        merged_pointers = dict(base.role_pointers)
        merged_pointers.update(self.role_pointers)

        return ModelConfig(profiles=merged_profiles, role_pointers=merged_pointers)


@dataclass(frozen=True)
class ModelConfigLayers:
    global_config: ModelConfig = field(default_factory=ModelConfig)
    project_config: ModelConfig | None = None
    session_config: ModelConfig | None = None
    op_config: ModelConfig | None = None

    def merged(self) -> ModelConfig:
        cfg = self.global_config
        for layer in (self.project_config, self.session_config, self.op_config):
            if layer is not None:
                cfg = layer.merge_over(cfg)
        cfg.validate_consistency()
        return cfg

