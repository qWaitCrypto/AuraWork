from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import ProviderAdapterError
from .types import ToolCall


@dataclass
class _OpenAIToolCallBuilder:
    tool_call_id: str | None = None
    name: str | None = None
    arguments_parts: list[str] | None = None

    def append_arguments(self, delta: str) -> None:
        if self.arguments_parts is None:
            self.arguments_parts = []
        self.arguments_parts.append(delta)

    def build(self) -> ToolCall:
        if not self.name:
            raise ProviderAdapterError("OpenAI tool call missing name.")
        raw = "".join(self.arguments_parts or [])
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError as e:
            snippet = raw.replace("\r", "\\r").replace("\n", "\\n")
            if len(snippet) > 240:
                snippet = snippet[:240] + f"... (+{len(raw) - 240} chars)"
            raise ProviderAdapterError(
                f"OpenAI tool call '{self.name}' arguments are not valid JSON: {e}; raw={snippet!r}"
            ) from e
        if not isinstance(parsed, dict):
            raise ProviderAdapterError("OpenAI tool call arguments must be a JSON object.")
        return ToolCall(tool_call_id=self.tool_call_id, name=self.name, arguments=parsed, raw_arguments=raw)


@dataclass
class _AnthropicToolCallBuilder:
    tool_call_id: str
    name: str
    partial_json_parts: list[str] | None = None

    def append_partial(self, delta: str) -> None:
        if self.partial_json_parts is None:
            self.partial_json_parts = []
        self.partial_json_parts.append(delta)

    def build(self) -> ToolCall:
        raw = "".join(self.partial_json_parts or [])
        if not raw:
            parsed: dict[str, Any] = {}
        else:
            try:
                parsed_any = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ProviderAdapterError(f"Anthropic tool_use input is not valid JSON: {e}") from e
            if not isinstance(parsed_any, dict):
                raise ProviderAdapterError("Anthropic tool_use input must be a JSON object.")
            parsed = parsed_any
        return ToolCall(tool_call_id=self.tool_call_id, name=self.name, arguments=parsed, raw_arguments=raw)

