from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from .base import PreparedRequest
from ..errors import ProviderAdapterError
from ..types import CanonicalMessage, CanonicalMessageRole, CanonicalRequest, ModelProfile


def _text_part(text: str) -> dict[str, Any]:
    return {"text": text}


def _function_call_part_with_signature(*, name: str, args: dict[str, Any], thought_signature: str | None) -> dict[str, Any]:
    part: dict[str, Any] = {"functionCall": {"name": name, "args": args}}
    if thought_signature:
        # Different Gemini gateways disagree on the exact field name. Some require snake_case.
        part["thoughtSignature"] = thought_signature
        part["thought_signature"] = thought_signature
    return part


def _function_response_part(*, name: str, response: Any) -> dict[str, Any]:
    return {"functionResponse": {"name": name, "response": response}}


def _tool_message_to_part(msg: CanonicalMessage) -> dict[str, Any] | None:
    if msg.role is not CanonicalMessageRole.TOOL:
        return None
    tool_name = msg.tool_name or "tool"
    try:
        parsed = json.loads(msg.content)
    except Exception:
        parsed = {"content": msg.content}

    response_obj: Any = parsed
    if isinstance(parsed, dict) and "result" in parsed:
        response_obj = parsed["result"]
    if not isinstance(response_obj, dict):
        response_obj = {"result": response_obj}
    return _function_response_part(name=tool_name, response=response_obj)


def _message_to_content(msg: CanonicalMessage) -> dict[str, Any] | None:
    parts: list[dict[str, Any]] = []

    if msg.role is not CanonicalMessageRole.TOOL and msg.content:
        parts.append(_text_part(msg.content))

    if msg.role is CanonicalMessageRole.ASSISTANT and msg.tool_calls:
        for tc in msg.tool_calls:
            parts.append(
                _function_call_part_with_signature(
                    name=tc.name,
                    args=dict(tc.arguments),
                    thought_signature=tc.thought_signature,
                )
            )

    # Tool responses are grouped separately in GeminiAdapter.prepare_request to satisfy Gemini's
    # "function response parts count == function call parts count per turn" invariant.
    if msg.role is CanonicalMessageRole.TOOL:
        return None

    if not parts:
        return None

    if msg.role is CanonicalMessageRole.USER:
        role = "user"
    elif msg.role is CanonicalMessageRole.ASSISTANT:
        role = "model"
    elif msg.role is CanonicalMessageRole.TOOL:
        role = "user"
    elif msg.role is CanonicalMessageRole.SYSTEM:
        role = "user"
    else:
        role = "user"

    return {"role": role, "parts": parts}


class GeminiAdapter:
    """
    Adapter for Gemini-style `v1beta/models/{model}:generateContent`.

    `base_url` can be either:
      - a base prefix (e.g. `https://gateway.example.com/api`) in which case the adapter
        appends `v1beta/models/{model}:generateContent`, or
      - a full `:generateContent` URL.

    Provider-specific fields live in ModelProfile.default_params and are merged into the
    top-level JSON payload (except reserved keys like `contents` / `tools`).
    """

    def prepare_request(self, profile: ModelProfile, request: CanonicalRequest) -> PreparedRequest:
        if not isinstance(profile.default_params, dict):
            raise ProviderAdapterError("gemini profile.default_params must be a dict.")

        contents: list[dict[str, Any]] = []
        if request.system:
            contents.append({"role": "user", "parts": [_text_part(request.system)]})
        pending_tool_parts: list[dict[str, Any]] = []
        for msg in request.messages:
            if msg.role is CanonicalMessageRole.TOOL:
                part = _tool_message_to_part(msg)
                if part is not None:
                    pending_tool_parts.append(part)
                continue

            if pending_tool_parts:
                # Gemini expects all functionResponse parts for a tool-call turn to appear in a single
                # "user" content entry, matching the number/order of functionCall parts.
                contents.append({"role": "user", "parts": list(pending_tool_parts)})
                pending_tool_parts.clear()

            content = _message_to_content(msg)
            if content is not None:
                contents.append(content)

        if pending_tool_parts:
            contents.append({"role": "user", "parts": list(pending_tool_parts)})
            pending_tool_parts.clear()

        payload: dict[str, Any] = {}
        for k, v in profile.default_params.items():
            if k in {"contents", "tools", "model", "request"}:
                continue
            payload[k] = v

        for k, v in (request.params or {}).items():
            if k in {"contents", "tools", "model", "request"}:
                continue
            payload[k] = v

        payload["contents"] = contents

        if request.tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.input_schema,
                        }
                        for t in request.tools
                    ]
                }
            ]
            payload.setdefault("toolConfig", {"functionCallingConfig": {"mode": "AUTO"}})

        url = build_generate_content_url(base_url=profile.base_url, model_name=profile.model_name, stream=False)
        return PreparedRequest(method="POST", url=url, headers={"Content-Type": "application/json"}, json=payload)


def build_generate_content_url(*, base_url: str, model_name: str, stream: bool) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ProviderAdapterError("gemini base_url is empty.")

    if ":generateContent" in base_url:
        url = base_url
    else:
        parsed = urlparse(base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ProviderAdapterError(f"Invalid gemini base_url: {base_url!r}")
        url = urljoin(base_url.rstrip("/") + "/", f"v1beta/models/{model_name}:generateContent")

    if not stream:
        return url

    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    if "alt" not in qs:
        qs["alt"] = ["sse"]
    query = urlencode(qs, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, parsed.fragment))
