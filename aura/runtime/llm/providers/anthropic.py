from __future__ import annotations

from urllib.parse import urljoin, urlparse

from ..errors import ProviderAdapterError
from ..secrets import resolve_credential
from ..types import CanonicalMessageRole, CanonicalRequest, ModelProfile, ProviderKind, ToolCall, ToolSpec
from .base import PreparedRequest


ANTHROPIC_DEFAULT_VERSION = "2023-06-01"


class AnthropicAdapter:
    def prepare_request(self, profile: ModelProfile, request: CanonicalRequest) -> PreparedRequest:
        if profile.provider_kind is not ProviderKind.ANTHROPIC:
            raise ProviderAdapterError("Profile provider_kind mismatch for AnthropicAdapter.")

        base_url = _validate_base_url(profile.base_url)
        url = urljoin(base_url.rstrip("/") + "/", "v1/messages")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if profile.credential_ref is None:
            raise ProviderAdapterError("Missing credential_ref for anthropic profile.")
        api_key = resolve_credential(profile.credential_ref)
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = ANTHROPIC_DEFAULT_VERSION

        payload = dict(profile.default_params)
        payload.update(request.params)
        payload["model"] = profile.model_name

        if request.system:
            payload["system"] = request.system

        messages = []
        for msg in request.messages:
            if msg.role is CanonicalMessageRole.SYSTEM:
                raise ProviderAdapterError(
                    "Anthropic does not accept separate system messages; use CanonicalRequest.system."
                )
            if msg.role is CanonicalMessageRole.USER:
                messages.append({"role": "user", "content": msg.content})
            elif msg.role is CanonicalMessageRole.ASSISTANT:
                if msg.tool_calls:
                    blocks = []
                    if msg.content:
                        blocks.append({"type": "text", "text": msg.content})
                    for call in msg.tool_calls:
                        blocks.append(_tool_call_to_anthropic(call))
                    messages.append({"role": "assistant", "content": blocks})
                else:
                    messages.append({"role": "assistant", "content": msg.content})
            elif msg.role is CanonicalMessageRole.TOOL:
                if not msg.tool_call_id:
                    raise ProviderAdapterError("Tool message is missing tool_call_id.")
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id,
                                "content": msg.content,
                            }
                        ],
                    }
                )
            else:
                raise ProviderAdapterError(f"Unsupported canonical message role: {msg.role}")
        payload["messages"] = messages

        if request.tools:
            payload["tools"] = [_tool_spec_to_anthropic(t) for t in request.tools]

        return PreparedRequest(method="POST", url=url, headers=headers, json=payload)


def _tool_spec_to_anthropic(tool: ToolSpec) -> dict:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


def _tool_call_to_anthropic(call: ToolCall) -> dict:
    if not call.tool_call_id:
        raise ProviderAdapterError("Tool call is missing tool_call_id.")
    return {
        "type": "tool_use",
        "id": call.tool_call_id,
        "name": call.name,
        "input": call.arguments,
    }


def _validate_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ProviderAdapterError(f"Invalid base_url: {base_url!r}")
    if parsed.path.rstrip("/").endswith("/v1"):
        raise ProviderAdapterError(
            "anthropic base_url must not end with '/v1' "
            "(example: 'https://api.anthropic.com')."
        )
    return base_url
