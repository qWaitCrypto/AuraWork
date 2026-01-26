from __future__ import annotations

import json
from typing import Any

from .errors import ProviderAdapterError
from .types import LLMResponse, LLMUsage, ProviderKind, ToolCall


def _gemini_usage_from_metadata(meta: Any) -> LLMUsage | None:
    if not isinstance(meta, dict):
        return None
    prompt = meta.get("promptTokenCount")
    candidates = meta.get("candidatesTokenCount")
    total = meta.get("totalTokenCount")
    return LLMUsage(
        input_tokens=prompt if isinstance(prompt, int) else None,
        output_tokens=candidates if isinstance(candidates, int) else None,
        total_tokens=total if isinstance(total, int) else None,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
    )


def _normalize_candidates(root: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = root.get("candidates")
    if isinstance(candidates, list):
        out: list[dict[str, Any]] = []
        for item in candidates:
            if isinstance(item, dict):
                out.append(item)
        return out
    if isinstance(candidates, dict):
        return [candidates]
    return []


def gemini_to_response(*, profile_id: str, data: Any) -> LLMResponse:
    if not isinstance(data, dict):
        raise ProviderAdapterError("gemini response must be a JSON object.")

    # Some gateways wrap the payload.
    root = data.get("response") if isinstance(data.get("response"), dict) else data
    if not isinstance(root, dict):
        raise ProviderAdapterError("gemini response wrapper is invalid.")

    candidates = _normalize_candidates(root)
    if not candidates:
        raise ProviderAdapterError("gemini response is missing candidates[0].")
    cand0 = candidates[0]

    content = cand0.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        parts = []

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for idx, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and part.get("thought") is not True:
            text_parts.append(text)
        fc = part.get("functionCall")
        if isinstance(fc, dict):
            name = fc.get("name")
            args = fc.get("args")
            thought_sig = (
                part.get("thoughtSignature")
                or part.get("thought_signature")
                or fc.get("thoughtSignature")
                or fc.get("thought_signature")
            )
            if not isinstance(name, str) or not name:
                raise ProviderAdapterError("gemini functionCall missing name.")
            if args is None:
                parsed_args: dict[str, Any] = {}
            elif isinstance(args, dict):
                parsed_args = args
            else:
                raise ProviderAdapterError("gemini functionCall.args must be an object.")
            raw = json.dumps(parsed_args, ensure_ascii=False)
            tool_calls.append(
                ToolCall(
                    tool_call_id=f"gemini_{idx}",
                    name=name,
                    arguments=dict(parsed_args),
                    raw_arguments=raw,
                    thought_signature=(str(thought_sig) if isinstance(thought_sig, str) and thought_sig else None),
                )
            )

    model = root.get("modelVersion")
    if not isinstance(model, str) or not model:
        model = root.get("model") if isinstance(root.get("model"), str) else ""

    stop = cand0.get("finishReason")
    stop_str = stop if isinstance(stop, str) else None

    request_id = root.get("responseId")
    request_id_str = request_id if isinstance(request_id, str) else None

    usage = _gemini_usage_from_metadata(root.get("usageMetadata"))
    return LLMResponse(
        provider_kind=ProviderKind.GEMINI,
        profile_id=profile_id,
        model=model or "",
        text="".join(text_parts),
        tool_calls=tool_calls,
        usage=usage,
        stop_reason=stop_str,
        request_id=request_id_str,
    )
