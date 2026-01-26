from __future__ import annotations

import json
from urllib.parse import urljoin, urlparse

from ..errors import ProviderAdapterError
from ..secrets import resolve_credential
from ..types import CanonicalMessageRole, CanonicalRequest, ModelProfile, ProviderKind, ToolCall, ToolSpec
from .base import PreparedRequest


def _use_content_parts(profile: ModelProfile) -> bool:
    if profile.credential_ref is not None and profile.credential_ref.kind == "codex_cli":
        return False
    return True


def _content_parts(role: CanonicalMessageRole, text: str) -> list[dict]:
    part_type = "input_text" if role is CanonicalMessageRole.USER else "output_text"
    return [{"type": part_type, "text": text}]


class OpenAICodexAdapter:
    """
    Adapter for OpenAI Responses API payloads.

    This is used by the OPENAI_CODEX provider to:
    - build a normalized /responses payload from Aura's canonical request
    - support tracing/testing without coupling the executor to payload construction
    """

    def prepare_request(self, profile: ModelProfile, request: CanonicalRequest) -> PreparedRequest:
        if profile.provider_kind is not ProviderKind.OPENAI_CODEX:
            raise ProviderAdapterError("Profile provider_kind mismatch for OpenAICodexAdapter.")

        base_url = _validate_base_url(profile.base_url)
        url = urljoin(base_url.rstrip("/") + "/", "responses")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if profile.credential_ref is None:
            raise ProviderAdapterError("Missing credential_ref for openai_codex profile.")
        token = resolve_credential(profile.credential_ref)
        headers["Authorization"] = f"Bearer {token}"

        payload: dict = dict(profile.default_params)
        payload.update(request.params)
        payload["model"] = profile.model_name
        payload["store"] = False

        instructions = request.system or ""
        payload["instructions"] = instructions

        use_parts = _use_content_parts(profile)
        input_items: list[dict] = []
        for msg in request.messages:
            if msg.role is CanonicalMessageRole.SYSTEM:
                # Canonical system messages should be folded into `instructions`.
                if msg.content:
                    instructions = (instructions + "\n\n" + msg.content).strip() if instructions else msg.content
                continue

            if msg.role is CanonicalMessageRole.USER:
                if use_parts:
                    input_items.append({"role": "user", "content": _content_parts(msg.role, msg.content)})
                else:
                    input_items.append({"type": "message", "role": "user", "content": msg.content})
                continue

            if msg.role is CanonicalMessageRole.ASSISTANT:
                if msg.content:
                    if use_parts:
                        input_items.append({"role": "assistant", "content": _content_parts(msg.role, msg.content)})
                    else:
                        input_items.append({"type": "message", "role": "assistant", "content": msg.content})
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        input_items.append(_tool_call_to_responses(tc))
                continue

            if msg.role is CanonicalMessageRole.TOOL:
                if not msg.tool_call_id:
                    raise ProviderAdapterError("Tool message is missing tool_call_id.")
                input_items.append({"type": "function_call_output", "call_id": msg.tool_call_id, "output": msg.content})
                continue

            raise ProviderAdapterError(f"Unsupported canonical message role: {msg.role}")

        payload["instructions"] = instructions
        payload["input"] = input_items

        if request.tools:
            payload["tools"] = [_tool_spec_to_responses(t) for t in request.tools]

        return PreparedRequest(method="POST", url=url, headers=headers, json=payload)


def _tool_spec_to_responses(tool: ToolSpec) -> dict:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema,
        "strict": False,
    }


def _tool_call_to_responses(call: ToolCall) -> dict:
    if not call.tool_call_id:
        raise ProviderAdapterError("Tool call is missing tool_call_id.")
    arguments_json = call.raw_arguments if call.raw_arguments is not None else json.dumps(call.arguments, ensure_ascii=False)
    return {
        "type": "function_call",
        "call_id": call.tool_call_id,
        "name": call.name,
        "arguments": arguments_json,
    }


def _validate_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ProviderAdapterError(f"Invalid base_url: {base_url!r}")
    path = parsed.path.rstrip("/")
    if not (path.endswith("/v1") or path.endswith("/backend-api/codex")):
        raise ProviderAdapterError(
            "openai_codex base_url must end with '/v1' (OpenAI Platform) or '/backend-api/codex' (ChatGPT Codex backend)."
        )
    return base_url
