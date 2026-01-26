from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ProviderKind(StrEnum):
    OPENAI_COMPATIBLE = "openai_compatible"
    OPENAI_CODEX = "openai_codex"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"


class ModelRole(StrEnum):
    MAIN = "main"
    WRITE = "write"
    EXTRACT = "extract"
    QUICK = "quick"
    TOOL_INTERPRETER = "tool_interpreter"
    SUBAGENT = "subagent"


@dataclass(frozen=True)
class CredentialRef:
    kind: str
    identifier: str

    def to_redacted_string(self) -> str:
        if self.kind in {"inline", "plaintext"}:
            return f"{self.kind}:***"
        return f"{self.kind}:{self.identifier}"


@dataclass(frozen=True)
class ModelLimits:
    context_limit_tokens: int | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class ContextManagementConfig:
    # When set to a value between 0 and 1, auto-compact triggers if estimated_input/context_limit > threshold.
    # Disabled if <= 0 or >= 1, or if unset.
    auto_compact_threshold_ratio: float | None = None
    # Budget for retained history after compaction: floor(context_limit * ratio).
    history_budget_ratio: float | None = None
    # Fallback budget when context limit is unknown.
    history_budget_fallback_tokens: int | None = None
    # Per-tool-message budget when preparing compaction input.
    tool_output_budget_tokens: int | None = None


@dataclass(frozen=True)
class ModelCapabilities:
    supports_tools: bool | None = None
    supports_structured_output: bool | None = None
    supports_streaming: bool | None = None

    def with_provider_defaults(self, provider_kind: ProviderKind) -> "ModelCapabilities":
        supports_streaming = self.supports_streaming
        if supports_streaming is None:
            if provider_kind in (
                ProviderKind.OPENAI_COMPATIBLE,
                ProviderKind.OPENAI_CODEX,
                ProviderKind.ANTHROPIC,
                ProviderKind.GEMINI,
            ):
                supports_streaming = True
        supports_tools = self.supports_tools
        if supports_tools is None:
            if provider_kind in (
                ProviderKind.OPENAI_COMPATIBLE,
                ProviderKind.OPENAI_CODEX,
                ProviderKind.ANTHROPIC,
                ProviderKind.GEMINI,
            ):
                supports_tools = True
        return ModelCapabilities(
            supports_tools=supports_tools,
            supports_structured_output=self.supports_structured_output,
            supports_streaming=supports_streaming,
        )


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    provider_kind: ProviderKind
    base_url: str
    model_name: str
    credential_ref: CredentialRef | None = None
    timeout_s: float | None = None
    default_params: dict[str, Any] = field(default_factory=dict)
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    tags: set[str] = field(default_factory=set)
    limits: ModelLimits | None = None
    context_management: ContextManagementConfig | None = None


@dataclass(frozen=True)
class ModelRequirements:
    needs_streaming: bool = False
    needs_tools: bool = False
    needs_structured_output: bool = False
    min_context_tokens: int | None = None


class CanonicalMessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class CanonicalMessage:
    role: CanonicalMessageRole
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: list[ToolCall] | None = None
    # Some OpenAI-compatible gateways (e.g. Moonshot/Kimi) require assistant tool-call
    # messages to include `reasoning_content` when "thinking" is enabled server-side.
    reasoning_content: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class CanonicalRequest:
    system: str | None
    messages: list[CanonicalMessage]
    tools: list[ToolSpec] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    tool_call_id: str | None
    name: str
    arguments: dict[str, Any]
    raw_arguments: str | None = None
    thought_signature: str | None = None


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


@dataclass(frozen=True)
class LLMResponse:
    provider_kind: ProviderKind
    profile_id: str
    model: str
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: LLMUsage | None = None
    stop_reason: str | None = None
    request_id: str | None = None
    thinking: str | None = None


class LLMStreamEventKind(StrEnum):
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL = "tool_call"
    COMPLETED = "completed"

 
@dataclass(frozen=True)
class ToolCallDelta:
    tool_call_index: int | None
    tool_call_id: str | None
    name: str | None
    raw_arguments_delta: str | None


@dataclass(frozen=True)
class LLMStreamEvent:
    kind: LLMStreamEventKind
    text_delta: str | None = None
    thinking_delta: str | None = None
    tool_call_delta: ToolCallDelta | None = None
    tool_call: ToolCall | None = None
    response: LLMResponse | None = None
