from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
import tomllib

from .config import ModelConfig, ModelConfigLayers
from .errors import ModelConfigError
from .types import (
    ContextManagementConfig,
    CredentialRef,
    ModelCapabilities,
    ModelLimits,
    ModelProfile,
    ModelRole,
    ProviderKind,
)

ENV_FILENAME = "env"
MODELS_FILENAME = "models.json"

def default_global_models_path() -> Path:
    override = os.environ.get("NOVELAIRE_GLOBAL_MODELS_PATH")
    if override:
        return Path(os.path.expanduser(override))
    return Path.home() / ".aura" / "config" / "models.json"


def project_models_path(project_root: Path) -> Path:
    override = os.environ.get("NOVELAIRE_PROJECT_MODELS_PATH")
    if override:
        path = Path(os.path.expanduser(override))
        if not path.is_absolute():
            path = project_root / path
        return path
    return project_root / ".aura" / "config" / "models.json"

def default_global_env_path() -> Path:
    override = os.environ.get("NOVELAIRE_GLOBAL_ENV_PATH")
    if override:
        return Path(os.path.expanduser(override))
    return Path.home() / ".aura" / "config" / ENV_FILENAME


def project_env_path(project_root: Path) -> Path:
    override = os.environ.get("NOVELAIRE_PROJECT_ENV_PATH")
    if override:
        path = Path(os.path.expanduser(override))
        if not path.is_absolute():
            path = project_root / path
        return path
    return project_root / ".aura" / "config" / ENV_FILENAME


def discover_project_root(start_dir: Path) -> Path | None:
    current = start_dir.resolve()
    while True:
        if (current / ".aura").is_dir():
            return current
        if current.parent == current:
            return None
        current = current.parent


def load_model_config_file(path: Path) -> ModelConfig:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ModelConfigError(f"Model config file not found: {path}") from e
    except OSError as e:
        raise ModelConfigError(f"Failed to read model config file: {path} ({e})") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ModelConfigError(f"Model config file is not valid JSON: {path} ({e})") from e

    return load_model_config_dict(data, source=str(path))


def save_model_config_file(path: Path, config: ModelConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = model_config_to_dict(config)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_model_registry_file(path: Path) -> tuple[ModelConfig, str | None]:
    """
    Load a hierarchical models.json (registry format) and return (ModelConfig, default_profile_id).

    Registry schema:
      - default_profile (optional)
      - profiles: { "<profile-id>": { provider_kind, base_url, model, api_key?, timeout_s?, max_tokens?, ... } }

    The returned ModelConfig uses role_pointers ONLY to set the default chat model pointer (ModelRole.MAIN).
    Users do not configure role pointers directly in models.json.
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ModelConfigError(f"Model registry file not found: {path}") from e
    except OSError as e:
        raise ModelConfigError(f"Failed to read model registry file: {path} ({e})") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ModelConfigError(f"Model registry file is not valid JSON: {path} ({e})") from e

    root = _ensure_dict(data, ctx=f"{path}:root")
    _assert_known_keys(root, allowed={"default_profile", "profiles"}, ctx=f"{path}:root")

    default_profile_raw = root.get("default_profile")
    default_profile = str(default_profile_raw).strip() if default_profile_raw is not None else None
    if default_profile == "":
        default_profile = None

    profiles_raw = root.get("profiles", {})
    profiles_obj = _ensure_dict(profiles_raw, ctx=f"{path}:profiles")
    if not profiles_obj:
        raise ModelConfigError(f"{path}:profiles must not be empty")

    profiles: dict[str, ModelProfile] = {}
    for profile_id, profile_data in profiles_obj.items():
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ModelConfigError(f"{path}:profiles: profile id must be a non-empty string")
        profile_id = profile_id.strip()
        profile_dict = _ensure_dict(profile_data, ctx=f"{path}:profiles.{profile_id}")
        profiles[profile_id] = _parse_registry_profile(profile_id, profile_dict, source=str(path))

    chosen_default = default_profile
    if chosen_default is None:
        if "main" in profiles:
            chosen_default = "main"
        else:
            chosen_default = sorted(profiles.keys())[0]

    if chosen_default not in profiles:
        raise ModelConfigError(f"{path}: default_profile '{chosen_default}' not found in profiles")

    cfg = ModelConfig(profiles=profiles, role_pointers={ModelRole.MAIN: chosen_default})
    cfg.validate_consistency()
    return cfg, chosen_default


def _parse_registry_profile(profile_id: str, profile_dict: dict[str, Any], *, source: str) -> ModelProfile:
    _assert_known_keys(
        profile_dict,
        allowed={
            "provider_kind",
            "base_url",
            "model",
            "api_key",
            "timeout_s",
            "max_tokens",
            "default_params",
            "capabilities",
            "limits",
            "context_management",
        },
        ctx=f"{source}:profiles.{profile_id}",
    )

    provider_kind_str = _require_str(profile_dict, "provider_kind", ctx=f"{source}:profiles.{profile_id}")
    try:
        provider_kind = ProviderKind(provider_kind_str)
    except ValueError as e:
        supported = ", ".join(sorted(k.value for k in ProviderKind))
        raise ModelConfigError(
            f"{source}:profiles.{profile_id}: unknown provider_kind '{provider_kind_str}'. Supported: {supported}"
        ) from e

    # Allow empty base_url in templates; the client will raise an actionable error if a profile
    # with an empty base_url is selected for use.
    base_url_val = profile_dict.get("base_url")
    if not isinstance(base_url_val, str):
        raise ModelConfigError(f"{source}:profiles.{profile_id}.base_url must be a string")
    base_url = base_url_val
    model_name = _require_str(profile_dict, "model", ctx=f"{source}:profiles.{profile_id}")

    api_key_val = profile_dict.get("api_key")
    api_key = api_key_val if isinstance(api_key_val, str) else None
    api_key = api_key.strip() if isinstance(api_key, str) else None

    if (
        provider_kind is ProviderKind.OPENAI_CODEX
        and api_key
        and not api_key.startswith("codex-cli")
        and "/backend-api/codex" not in base_url
        and model_name.endswith("-codex")
    ):
        # Common misconfiguration: using a Codex-suffixed model name with a non-Codex OAuth token.
        # For OpenAI-compatible Responses endpoints, the unsuffixed model name is usually required.
        model_name = model_name[: -len("-codex")]

    if provider_kind is ProviderKind.OPENAI_CODEX and api_key == "codex-cli" and _should_apply_codex_cli_model_migrations():
        migrated = _codex_cli_model_migrations().get(model_name)
        if isinstance(migrated, str) and migrated.strip():
            model_name = migrated.strip()

    if api_key:
        if provider_kind is ProviderKind.OPENAI_CODEX and api_key.startswith("codex-cli"):
            # "codex-cli" means: reuse CODEX_HOME/auth.json (or ~/.codex/auth.json).
            # "codex-cli:/path/to/auth.json" can override the path.
            identifier = ""
            if api_key.startswith("codex-cli:"):
                identifier = api_key[len("codex-cli:") :].strip()
            credential_ref = CredentialRef(kind="codex_cli", identifier=identifier)
        else:
            credential_ref = CredentialRef(kind="inline", identifier=api_key)
    elif provider_kind is ProviderKind.OPENAI_COMPATIBLE:
        # OpenAI-compatible endpoints are often local/proxied and may not require authentication.
        # The OpenAI SDK still expects an api_key, so default to a dummy key if none is provided.
        credential_ref = CredentialRef(kind="inline", identifier="aura")
    else:
        credential_ref = None
        if provider_kind is ProviderKind.OPENAI_CODEX:
            raise ModelConfigError(f"{source}:profiles.{profile_id}: api_key is required for provider_kind=openai_codex")
        if provider_kind is ProviderKind.ANTHROPIC:
            raise ModelConfigError(f"{source}:profiles.{profile_id}: api_key is required for provider_kind=anthropic")
        if provider_kind is ProviderKind.GEMINI:
            raise ModelConfigError(f"{source}:profiles.{profile_id}: api_key is required for provider_kind=gemini")

    timeout_s = None
    if "timeout_s" in profile_dict and profile_dict["timeout_s"] is not None:
        timeout_s = _maybe_float(profile_dict["timeout_s"], ctx=f"{source}:profiles.{profile_id}.timeout_s")
        if timeout_s <= 0:
            raise ModelConfigError(f"{source}:profiles.{profile_id}.timeout_s must be > 0")

    max_tokens = None
    if "max_tokens" in profile_dict and profile_dict["max_tokens"] is not None:
        max_tokens = _maybe_int(profile_dict["max_tokens"], ctx=f"{source}:profiles.{profile_id}.max_tokens")
        if max_tokens <= 0:
            raise ModelConfigError(f"{source}:profiles.{profile_id}.max_tokens must be > 0")

    default_params: dict[str, Any] = {}
    if "default_params" in profile_dict and profile_dict["default_params"] is not None:
        default_params_obj = _ensure_dict(profile_dict["default_params"], ctx=f"{source}:profiles.{profile_id}.default_params")
        default_params = dict(default_params_obj)

    if max_tokens is not None:
        if provider_kind is ProviderKind.GEMINI:
            gen = default_params.get("generationConfig")
            if gen is None:
                default_params["generationConfig"] = {"maxOutputTokens": max_tokens}
            elif isinstance(gen, dict):
                gen.setdefault("maxOutputTokens", max_tokens)
            else:
                raise ModelConfigError(
                    f"{source}:profiles.{profile_id}: generationConfig must be an object when max_tokens is set for provider_kind=gemini"
                )
        else:
            default_params.setdefault("max_tokens", max_tokens)

    if provider_kind is ProviderKind.ANTHROPIC and "max_tokens" not in default_params:
        raise ModelConfigError(f"{source}:profiles.{profile_id}: max_tokens is required for provider_kind=anthropic")

    capabilities = ModelCapabilities(
        supports_tools=None,
        supports_structured_output=None,
        supports_streaming=None,
    )
    if "capabilities" in profile_dict and profile_dict["capabilities"] is not None:
        cap = _ensure_dict(profile_dict["capabilities"], ctx=f"{source}:profiles.{profile_id}.capabilities")
        _assert_known_keys(
            cap,
            allowed={"supports_tools", "supports_structured_output", "supports_streaming"},
            ctx=f"{source}:profiles.{profile_id}.capabilities",
        )
        capabilities = ModelCapabilities(
            supports_tools=_maybe_bool(
                cap.get("supports_tools"), ctx=f"{source}:profiles.{profile_id}.capabilities.supports_tools"
            ),
            supports_structured_output=_maybe_bool(
                cap.get("supports_structured_output"),
                ctx=f"{source}:profiles.{profile_id}.capabilities.supports_structured_output",
            ),
            supports_streaming=_maybe_bool(
                cap.get("supports_streaming"),
                ctx=f"{source}:profiles.{profile_id}.capabilities.supports_streaming",
            ),
        )

    limits = None
    if "limits" in profile_dict and profile_dict["limits"] is not None:
        lim = _ensure_dict(profile_dict["limits"], ctx=f"{source}:profiles.{profile_id}.limits")
        _assert_known_keys(
            lim,
            allowed={"context_limit_tokens", "max_output_tokens"},
            ctx=f"{source}:profiles.{profile_id}.limits",
        )
        limits = ModelLimits(
            context_limit_tokens=_maybe_int(
                lim.get("context_limit_tokens"),
                ctx=f"{source}:profiles.{profile_id}.limits.context_limit_tokens",
            ),
            max_output_tokens=_maybe_int(
                lim.get("max_output_tokens"),
                ctx=f"{source}:profiles.{profile_id}.limits.max_output_tokens",
            ),
        )

    context_management = None
    if "context_management" in profile_dict and profile_dict["context_management"] is not None:
        context_management = _parse_context_management(
            profile_dict["context_management"], ctx=f"{source}:profiles.{profile_id}.context_management"
        )

    return ModelProfile(
        profile_id=profile_id,
        provider_kind=provider_kind,
        base_url=base_url,
        model_name=model_name,
        credential_ref=credential_ref,
        timeout_s=timeout_s,
        default_params=default_params,
        capabilities=capabilities,
        tags=set(),
        limits=limits,
        context_management=context_management,
    )

def load_model_config_env_file(path: Path) -> ModelConfig:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ModelConfigError(f"Model env config file not found: {path}") from e
    except OSError as e:
        raise ModelConfigError(f"Failed to read model env config file: {path} ({e})") from e

    env = parse_env_text(raw)
    return load_model_config_from_env(env, source=str(path))


def parse_env_text(raw: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line_no, line in enumerate(raw.splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[len("export ") :].lstrip()
        if "=" not in s:
            raise ModelConfigError(f"Invalid env line (missing '=') at line {line_no}")
        key, value = s.split("=", 1)
        key = key.strip()
        if not key:
            raise ModelConfigError(f"Invalid env line (empty key) at line {line_no}")
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        env[key] = value
    return env


def _maybe_bool_env(value: str | None, *, key: str) -> bool | None:
    if value is None or value == "":
        return None
    v = value.strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    raise ModelConfigError(f"Invalid boolean for {key}: {value!r}")


def _maybe_float_env(value: str | None, *, key: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError as e:
        raise ModelConfigError(f"Invalid float for {key}: {value!r}") from e


def _maybe_int_env(value: str | None, *, key: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as e:
        raise ModelConfigError(f"Invalid integer for {key}: {value!r}") from e


def load_model_config_from_env(env: dict[str, str], *, source: str) -> ModelConfig:
    provider_raw = (env.get("NOVELAIRE_PROVIDER_KIND") or "").strip()
    if not provider_raw:
        raise ModelConfigError(
            f"{source}: missing NOVELAIRE_PROVIDER_KIND (set to 'openai_compatible' or 'anthropic')"
        )
    try:
        provider_kind = ProviderKind(provider_raw)
    except ValueError as e:
        supported = ", ".join(sorted(k.value for k in ProviderKind))
        raise ModelConfigError(
            f"{source}: invalid NOVELAIRE_PROVIDER_KIND={provider_raw!r}. Supported: {supported}"
        ) from e

    base_url = (env.get("NOVELAIRE_BASE_URL") or "").strip()
    if not base_url:
        raise ModelConfigError(f"{source}: missing NOVELAIRE_BASE_URL")

    model_name = (env.get("NOVELAIRE_MODEL") or "").strip()
    if not model_name:
        raise ModelConfigError(f"{source}: missing NOVELAIRE_MODEL")

    api_key_raw = env.get("NOVELAIRE_API_KEY")
    api_key = api_key_raw.strip() if isinstance(api_key_raw, str) else ""

    if provider_kind is ProviderKind.OPENAI_COMPATIBLE:
        # OpenAI-compatible endpoints are often local/proxied and may not require authentication.
        # The OpenAI SDK still expects an api_key, so default to a dummy key if none is provided.
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
        elif not os.environ.get("OPENAI_API_KEY"):
            os.environ["OPENAI_API_KEY"] = "aura"
        credential_ref = CredentialRef(kind="env", identifier="OPENAI_API_KEY")
    else:
        # Anthropic requires authentication.
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ModelConfigError(
                f"{source}: missing NOVELAIRE_API_KEY (or ANTHROPIC_API_KEY in environment) for provider_kind=anthropic"
            )
        credential_ref = CredentialRef(kind="env", identifier="ANTHROPIC_API_KEY")

    timeout_s = _maybe_float_env(env.get("NOVELAIRE_TIMEOUT_S"), key="NOVELAIRE_TIMEOUT_S")
    if timeout_s is None:
        timeout_s = 60.0
    if timeout_s <= 0:
        raise ModelConfigError(f"{source}: NOVELAIRE_TIMEOUT_S must be > 0")
    max_tokens = _maybe_int_env(env.get("NOVELAIRE_MAX_TOKENS"), key="NOVELAIRE_MAX_TOKENS")

    default_params: dict[str, Any] = {}
    if provider_kind is ProviderKind.ANTHROPIC:
        if max_tokens is None:
            raise ModelConfigError(f"{source}: NOVELAIRE_MAX_TOKENS is required for provider_kind=anthropic")
        default_params["max_tokens"] = max_tokens
    else:
        if max_tokens is not None:
            default_params["max_tokens"] = max_tokens

    capabilities = ModelCapabilities(
        supports_tools=_maybe_bool_env(env.get("NOVELAIRE_SUPPORTS_TOOLS"), key="NOVELAIRE_SUPPORTS_TOOLS"),
        supports_structured_output=_maybe_bool_env(
            env.get("NOVELAIRE_SUPPORTS_STRUCTURED_OUTPUT"), key="NOVELAIRE_SUPPORTS_STRUCTURED_OUTPUT"
        ),
        supports_streaming=_maybe_bool_env(env.get("NOVELAIRE_SUPPORTS_STREAMING"), key="NOVELAIRE_SUPPORTS_STREAMING"),
    )

    profile = ModelProfile(
        profile_id="main",
        provider_kind=provider_kind,
        base_url=base_url,
        model_name=model_name,
        credential_ref=credential_ref,
        timeout_s=timeout_s,
        default_params=default_params,
        capabilities=capabilities,
        tags=set(),
        limits=None,
        context_management=None,
    )
    return ModelConfig(profiles={"main": profile}, role_pointers={ModelRole.MAIN: "main"})


def load_model_config_layers_for_dir(
    start_dir: Path | None = None,
    *,
    global_path: Path | None = None,
    require_project: bool = True,
) -> ModelConfigLayers:
    start_dir = (start_dir or Path.cwd()).resolve()

    # v0.2+: prefer hierarchical models.json; keep env support as a legacy fallback.
    global_cfg = ModelConfig()
    global_models_path = default_global_models_path() if global_path is None else global_path
    if global_models_path is not None and global_models_path.exists():
        global_cfg, _ = load_model_registry_file(global_models_path)
    else:
        legacy_global_env = default_global_env_path()
        if legacy_global_env.exists():
            global_cfg = load_model_config_env_file(legacy_global_env)

    project_root = discover_project_root(start_dir)
    if project_root is None:
        if require_project:
            raise ModelConfigError(
                "No Aura project found (missing '.aura' directory). "
                "Run 'aura init' or pass an explicit project root."
            )
        return ModelConfigLayers(global_config=global_cfg)

    project_models = project_models_path(project_root)
    if project_models.exists():
        project_cfg, _ = load_model_registry_file(project_models)
        return ModelConfigLayers(global_config=global_cfg, project_config=project_cfg)

    # Legacy fallback: project env.
    project_env = project_env_path(project_root)
    if project_env.exists():
        return ModelConfigLayers(global_config=global_cfg, project_config=load_model_config_env_file(project_env))

    if require_project:
        raise ModelConfigError(
            f"Missing required project model config file: {project_models}. "
            "Run 'aura init' and edit '.aura/config/models.json'."
        )

    return ModelConfigLayers(global_config=global_cfg)


def load_model_config_dict(data: Any, *, source: str) -> ModelConfig:
    root = _ensure_dict(data, ctx=f"{source}:root")
    _assert_known_keys(root, allowed={"profiles", "role_pointers"}, ctx=f"{source}:root")

    profiles_raw = root.get("profiles", {})
    profiles_obj = _ensure_dict(profiles_raw, ctx=f"{source}:profiles")
    profiles: dict[str, ModelProfile] = {}
    for profile_id, profile_data in profiles_obj.items():
        if not isinstance(profile_id, str) or not profile_id:
            raise ModelConfigError(f"{source}:profiles: profile id must be a non-empty string")
        profile_dict = _ensure_dict(profile_data, ctx=f"{source}:profiles.{profile_id}")
        profiles[profile_id] = _parse_profile(profile_id, profile_dict, source=source)

    role_pointers_raw = root.get("role_pointers", {})
    role_pointers_obj = _ensure_dict(role_pointers_raw, ctx=f"{source}:role_pointers")
    role_pointers: dict[ModelRole, str] = {}
    for role_key, profile_id in role_pointers_obj.items():
        if not isinstance(role_key, str) or not role_key:
            raise ModelConfigError(f"{source}:role_pointers: role key must be a non-empty string")
        if not isinstance(profile_id, str) or not profile_id:
            raise ModelConfigError(
                f"{source}:role_pointers.{role_key}: profile id must be a non-empty string"
            )
        try:
            role = ModelRole(role_key)
        except ValueError as e:
            raise ModelConfigError(f"{source}:role_pointers: unknown role '{role_key}'") from e
        role_pointers[role] = profile_id

    return ModelConfig(profiles=profiles, role_pointers=role_pointers)


def model_config_to_dict(config: ModelConfig) -> dict[str, Any]:
    profiles: dict[str, Any] = {}
    for profile_id, profile in config.profiles.items():
        profiles[profile_id] = _profile_to_dict(profile)

    role_pointers = {role.value: profile_id for role, profile_id in config.role_pointers.items()}
    return {
        "profiles": profiles,
        "role_pointers": role_pointers,
    }


def _parse_profile(profile_id: str, profile_dict: dict[str, Any], *, source: str) -> ModelProfile:
    _assert_known_keys(
        profile_dict,
        allowed={
            "provider_kind",
            "base_url",
            "model_name",
            "credential_ref",
            "timeout_s",
            "default_params",
            "capabilities",
            "tags",
            "limits",
            "context_management",
            "profile_id",
        },
        ctx=f"{source}:profiles.{profile_id}",
    )

    explicit_id = profile_dict.get("profile_id")
    if explicit_id is not None and explicit_id != profile_id:
        raise ModelConfigError(
            f"{source}:profiles.{profile_id}: profile_id mismatch (got {explicit_id!r})"
        )

    provider_kind_str = _require_str(
        profile_dict, "provider_kind", ctx=f"{source}:profiles.{profile_id}"
    )
    try:
        provider_kind = ProviderKind(provider_kind_str)
    except ValueError as e:
        supported = ", ".join(sorted(k.value for k in ProviderKind))
        raise ModelConfigError(
            f"{source}:profiles.{profile_id}: unknown provider_kind '{provider_kind_str}'. Supported: {supported}"
        ) from e

    base_url = _require_str(profile_dict, "base_url", ctx=f"{source}:profiles.{profile_id}")
    model_name = _require_str(profile_dict, "model_name", ctx=f"{source}:profiles.{profile_id}")

    credential_ref = None
    if "credential_ref" in profile_dict and profile_dict["credential_ref"] is not None:
        cred = _ensure_dict(profile_dict["credential_ref"], ctx=f"{source}:profiles.{profile_id}.credential_ref")
        _assert_known_keys(cred, allowed={"kind", "identifier"}, ctx=f"{source}:profiles.{profile_id}.credential_ref")
        kind = _require_str(cred, "kind", ctx=f"{source}:profiles.{profile_id}.credential_ref")
        ident = _require_str(cred, "identifier", ctx=f"{source}:profiles.{profile_id}.credential_ref")
        credential_ref = CredentialRef(kind=kind, identifier=ident)

    timeout_s = None
    if "timeout_s" in profile_dict and profile_dict["timeout_s"] is not None:
        timeout_s = _maybe_float(profile_dict["timeout_s"], ctx=f"{source}:profiles.{profile_id}.timeout_s")
        if timeout_s <= 0:
            raise ModelConfigError(f"{source}:profiles.{profile_id}.timeout_s must be > 0")

    default_params: dict[str, Any] = {}
    if "default_params" in profile_dict and profile_dict["default_params"] is not None:
        default_params_obj = _ensure_dict(profile_dict["default_params"], ctx=f"{source}:profiles.{profile_id}.default_params")
        default_params = dict(default_params_obj)

    capabilities = ModelCapabilities()
    if "capabilities" in profile_dict and profile_dict["capabilities"] is not None:
        cap = _ensure_dict(profile_dict["capabilities"], ctx=f"{source}:profiles.{profile_id}.capabilities")
        _assert_known_keys(
            cap,
            allowed={"supports_tools", "supports_structured_output", "supports_streaming"},
            ctx=f"{source}:profiles.{profile_id}.capabilities",
        )
        capabilities = ModelCapabilities(
            supports_tools=_maybe_bool(
                cap.get("supports_tools"), ctx=f"{source}:profiles.{profile_id}.capabilities.supports_tools"
            ),
            supports_structured_output=_maybe_bool(
                cap.get("supports_structured_output"),
                ctx=f"{source}:profiles.{profile_id}.capabilities.supports_structured_output",
            ),
            supports_streaming=_maybe_bool(
                cap.get("supports_streaming"),
                ctx=f"{source}:profiles.{profile_id}.capabilities.supports_streaming",
            ),
        )

    tags: set[str] = set()
    if "tags" in profile_dict and profile_dict["tags"] is not None:
        tags_raw = profile_dict["tags"]
        if not isinstance(tags_raw, list) or not all(isinstance(t, str) and t for t in tags_raw):
            raise ModelConfigError(f"{source}:profiles.{profile_id}.tags must be a list of strings")
        tags = set(tags_raw)

    limits = None
    if "limits" in profile_dict and profile_dict["limits"] is not None:
        lim = _ensure_dict(profile_dict["limits"], ctx=f"{source}:profiles.{profile_id}.limits")
        _assert_known_keys(
            lim,
            allowed={"context_limit_tokens", "max_output_tokens"},
            ctx=f"{source}:profiles.{profile_id}.limits",
        )
        limits = ModelLimits(
            context_limit_tokens=_maybe_int(
                lim.get("context_limit_tokens"),
                ctx=f"{source}:profiles.{profile_id}.limits.context_limit_tokens",
            ),
            max_output_tokens=_maybe_int(
                lim.get("max_output_tokens"),
                ctx=f"{source}:profiles.{profile_id}.limits.max_output_tokens",
            ),
        )

    context_management = None
    if "context_management" in profile_dict and profile_dict["context_management"] is not None:
        context_management = _parse_context_management(
            profile_dict["context_management"], ctx=f"{source}:profiles.{profile_id}.context_management"
        )

    return ModelProfile(
        profile_id=profile_id,
        provider_kind=provider_kind,
        base_url=base_url,
        model_name=model_name,
        credential_ref=credential_ref,
        timeout_s=timeout_s,
        default_params=default_params,
        capabilities=capabilities,
        tags=tags,
        limits=limits,
        context_management=context_management,
    )


def _profile_to_dict(profile: ModelProfile) -> dict[str, Any]:
    out: dict[str, Any] = {
        "provider_kind": profile.provider_kind.value,
        "base_url": profile.base_url,
        "model_name": profile.model_name,
    }
    if profile.credential_ref is not None:
        out["credential_ref"] = {
            "kind": profile.credential_ref.kind,
            "identifier": profile.credential_ref.identifier,
        }
    if profile.timeout_s is not None:
        out["timeout_s"] = profile.timeout_s
    if profile.default_params:
        out["default_params"] = dict(profile.default_params)

    caps = profile.capabilities
    caps_dict: dict[str, Any] = {}
    if caps.supports_tools is not None:
        caps_dict["supports_tools"] = caps.supports_tools
    if caps.supports_structured_output is not None:
        caps_dict["supports_structured_output"] = caps.supports_structured_output
    if caps.supports_streaming is not None:
        caps_dict["supports_streaming"] = caps.supports_streaming
    if caps_dict:
        out["capabilities"] = caps_dict

    if profile.tags:
        out["tags"] = sorted(profile.tags)

    if profile.limits is not None and (
        profile.limits.context_limit_tokens is not None
        or profile.limits.max_output_tokens is not None
    ):
        limits: dict[str, Any] = {}
        if profile.limits.context_limit_tokens is not None:
            limits["context_limit_tokens"] = profile.limits.context_limit_tokens
        if profile.limits.max_output_tokens is not None:
            limits["max_output_tokens"] = profile.limits.max_output_tokens
        out["limits"] = limits

    if profile.context_management is not None:
        cm = profile.context_management
        cm_dict: dict[str, Any] = {}
        if cm.auto_compact_threshold_ratio is not None:
            cm_dict["auto_compact_threshold_ratio"] = cm.auto_compact_threshold_ratio
        if cm.history_budget_ratio is not None:
            cm_dict["history_budget_ratio"] = cm.history_budget_ratio
        if cm.history_budget_fallback_tokens is not None:
            cm_dict["history_budget_fallback_tokens"] = cm.history_budget_fallback_tokens
        if cm.tool_output_budget_tokens is not None:
            cm_dict["tool_output_budget_tokens"] = cm.tool_output_budget_tokens
        if cm_dict:
            out["context_management"] = cm_dict

    return out


def _parse_context_management(value: Any, *, ctx: str) -> ContextManagementConfig:
    obj = _ensure_dict(value, ctx=ctx)
    _assert_known_keys(
        obj,
        allowed={
            "auto_compact_threshold_ratio",
            "history_budget_ratio",
            "history_budget_fallback_tokens",
            "tool_output_budget_tokens",
        },
        ctx=ctx,
    )
    auto_ratio = _maybe_float(obj.get("auto_compact_threshold_ratio"), ctx=f"{ctx}.auto_compact_threshold_ratio")
    history_ratio = _maybe_float(obj.get("history_budget_ratio"), ctx=f"{ctx}.history_budget_ratio")
    fallback_tokens = _maybe_int(obj.get("history_budget_fallback_tokens"), ctx=f"{ctx}.history_budget_fallback_tokens")
    tool_budget = _maybe_int(obj.get("tool_output_budget_tokens"), ctx=f"{ctx}.tool_output_budget_tokens")

    if history_ratio is not None and not (0.0 < history_ratio <= 1.0):
        raise ModelConfigError(f"{ctx}.history_budget_ratio must be > 0 and <= 1")
    if fallback_tokens is not None and fallback_tokens <= 0:
        raise ModelConfigError(f"{ctx}.history_budget_fallback_tokens must be > 0")
    if tool_budget is not None and tool_budget < 0:
        raise ModelConfigError(f"{ctx}.tool_output_budget_tokens must be >= 0")
    return ContextManagementConfig(
        auto_compact_threshold_ratio=auto_ratio,
        history_budget_ratio=history_ratio,
        history_budget_fallback_tokens=fallback_tokens,
        tool_output_budget_tokens=tool_budget,
    )


def _ensure_dict(value: Any, *, ctx: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelConfigError(f"{ctx} must be an object")
    return value


def _require_str(obj: dict[str, Any], key: str, *, ctx: str) -> str:
    val = obj.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ModelConfigError(f"{ctx}.{key} must be a non-empty string")
    return val


def _maybe_bool(value: Any, *, ctx: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ModelConfigError(f"{ctx} must be a boolean or null")


def _maybe_int(value: Any, *, ctx: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    raise ModelConfigError(f"{ctx} must be an integer or null")


def _maybe_float(value: Any, *, ctx: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ModelConfigError(f"{ctx} must be a number or null")
    if isinstance(value, (int, float)):
        return float(value)
    raise ModelConfigError(f"{ctx} must be a number or null")


def _assert_known_keys(obj: dict[str, Any], *, allowed: set[str], ctx: str) -> None:
    unknown = set(obj.keys()) - allowed
    if unknown:
        rendered = ", ".join(sorted(unknown))
        raise ModelConfigError(f"{ctx} contains unknown keys: {rendered}")


_CACHED_CODEX_CLI_MODEL_MIGRATIONS: dict[str, str] | None = None


def _should_apply_codex_cli_model_migrations() -> bool:
    return os.environ.get("AURA_CODEX_CLI_APPLY_MODEL_MIGRATIONS") == "1"


def _codex_cli_model_migrations() -> dict[str, str]:
    global _CACHED_CODEX_CLI_MODEL_MIGRATIONS
    if _CACHED_CODEX_CLI_MODEL_MIGRATIONS is not None:
        return _CACHED_CODEX_CLI_MODEL_MIGRATIONS

    codex_home = os.environ.get("CODEX_HOME")
    config_path = (Path(codex_home) / "config.toml") if codex_home else (Path.home() / ".codex" / "config.toml")
    try:
        raw = config_path.read_bytes()
    except FileNotFoundError:
        _CACHED_CODEX_CLI_MODEL_MIGRATIONS = {}
        return _CACHED_CODEX_CLI_MODEL_MIGRATIONS

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except Exception:
        _CACHED_CODEX_CLI_MODEL_MIGRATIONS = {}
        return _CACHED_CODEX_CLI_MODEL_MIGRATIONS

    notice = data.get("notice")
    if not isinstance(notice, dict):
        _CACHED_CODEX_CLI_MODEL_MIGRATIONS = {}
        return _CACHED_CODEX_CLI_MODEL_MIGRATIONS
    migrations = notice.get("model_migrations")
    if not isinstance(migrations, dict):
        _CACHED_CODEX_CLI_MODEL_MIGRATIONS = {}
        return _CACHED_CODEX_CLI_MODEL_MIGRATIONS

    out: dict[str, str] = {}
    for k, v in migrations.items():
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            out[k.strip()] = v.strip()
    _CACHED_CODEX_CLI_MODEL_MIGRATIONS = out
    return out
