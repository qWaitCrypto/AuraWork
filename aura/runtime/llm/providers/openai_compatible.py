from __future__ import annotations

from urllib.parse import urljoin, urlparse

from ..errors import ProviderAdapterError
from ..secrets import resolve_credential
from ..types import CanonicalMessageRole, CanonicalRequest, ModelProfile, ProviderKind, ToolCall, ToolSpec
from .base import PreparedRequest


class OpenAICompatibleAdapter:
    def prepare_request(self, profile: ModelProfile, request: CanonicalRequest) -> PreparedRequest:
        if profile.provider_kind is not ProviderKind.OPENAI_COMPATIBLE:
            raise ProviderAdapterError("Profile provider_kind mismatch for OpenAICompatibleAdapter.")

        base_url = _validate_base_url(profile.base_url, requires_v1=True)
        parsed_base = urlparse(base_url)
        host = (parsed_base.hostname or "").lower()
        # Some OpenAI-compatible gateways (e.g. Moonshot/Kimi) require assistant tool-call messages
        # to include a `reasoning_content` field when "thinking" is enabled server-side.
        requires_reasoning_content = host.endswith("moonshot.cn")
        url = urljoin(base_url.rstrip("/") + "/", "chat/completions")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if profile.credential_ref is None:
            raise ProviderAdapterError("Missing credential_ref for openai_compatible profile.")
        api_key = resolve_credential(profile.credential_ref)
        headers["Authorization"] = f"Bearer {api_key}"

        payload = dict(profile.default_params)
        payload.update(request.params)
        payload["model"] = profile.model_name

        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for msg in request.messages:
            if msg.role is CanonicalMessageRole.SYSTEM:
                messages.append({"role": "system", "content": msg.content})
            elif msg.role is CanonicalMessageRole.USER:
                messages.append({"role": "user", "content": msg.content})
            elif msg.role is CanonicalMessageRole.ASSISTANT:
                # OpenAI-style tool calling requires assistant content to be null when `tool_calls` are present.
                # Some OpenAI-compatible gateways reject empty strings here, and will then treat subsequent
                # `role=tool` messages as invalid ("must be a response to a preceding message with tool_calls").
                content: str | None = msg.content
                if msg.tool_calls and (content is None or content == ""):
                    content = None
                payload_msg: dict = {"role": "assistant", "content": content}
                if msg.tool_calls:
                    payload_msg["tool_calls"] = [_tool_call_to_openai(tc) for tc in msg.tool_calls]
                if requires_reasoning_content:
                    rc = getattr(msg, "reasoning_content", None)
                    if isinstance(rc, str) and rc.strip():
                        payload_msg["reasoning_content"] = rc
                    elif msg.tool_calls:
                        payload_msg["reasoning_content"] = "..."
                messages.append(payload_msg)
            elif msg.role is CanonicalMessageRole.TOOL:
                if not msg.tool_call_id:
                    raise ProviderAdapterError("Tool message is missing tool_call_id.")
                messages.append(
                    {
                        "role": "tool",
                        "content": msg.content,
                        "tool_call_id": msg.tool_call_id,
                    }
                )
            else:
                raise ProviderAdapterError(f"Unsupported canonical message role: {msg.role}")
        payload["messages"] = messages

        if request.tools:
            payload["tools"] = [_tool_spec_to_openai(t) for t in request.tools]

        return PreparedRequest(method="POST", url=url, headers=headers, json=payload)


def _tool_spec_to_openai(tool: ToolSpec) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _tool_call_to_openai(call: ToolCall) -> dict:
    if not call.tool_call_id:
        raise ProviderAdapterError("Tool call is missing tool_call_id.")
    arguments_json = call.raw_arguments if call.raw_arguments is not None else json_dumps(call.arguments)
    return {
        "id": call.tool_call_id,
        "type": "function",
        "function": {
            "name": call.name,
            "arguments": arguments_json,
        },
    }


def json_dumps(obj: object) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def _validate_base_url(base_url: str, *, requires_v1: bool) -> str:
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ProviderAdapterError(f"Invalid base_url: {base_url!r}")
    if requires_v1 and not parsed.path.rstrip("/").endswith("/v1"):
        raise ProviderAdapterError(
            "openai_compatible base_url must include the '/v1' path segment "
            "(example: 'http://localhost:8000/v1' or 'https://api.openai.com/v1')."
        )
    return base_url
