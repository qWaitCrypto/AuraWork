from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any, Dict, Iterator, List, Optional, Type, Union

from pydantic import BaseModel

from .client_exec_anthropic import complete_anthropic
from .client_exec_openai_codex import complete_openai_codex
from .client_exec_gemini import complete_gemini
from .client_exec_openai_compatible import complete_openai_compatible
from .errors import LLMRequestError, ProviderAdapterError
from .trace import LLMTrace
from .types import (
    CanonicalMessage,
    CanonicalMessageRole,
    CanonicalRequest,
    LLMResponse,
    LLMUsage,
    ModelProfile,
    ProviderKind,
    ToolCall,
    ToolSpec,
)

try:  # pragma: no cover - agno is an optional dependency at import time
    from agno.models.base import Model
except Exception:  # pragma: no cover
    Model = object  # type: ignore[misc,assignment]


def _tool_dicts_to_specs(tools: Optional[List[Dict[str, Any]]]) -> list[ToolSpec]:
    specs: list[ToolSpec] = []
    for item in tools or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "function":
            continue
        fn = item.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        desc = fn.get("description")
        params = fn.get("parameters")
        specs.append(
            ToolSpec(
                name=name.strip(),
                description=str(desc or ""),
                input_schema=dict(params) if isinstance(params, dict) else {"type": "object", "properties": {}},
            )
        )
    return specs


def _parse_tool_calls(tool_calls: Any) -> list[ToolCall]:
    if not isinstance(tool_calls, list) or not tool_calls:
        return []
    parsed: list[ToolCall] = []
    for item in tool_calls:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "function":
            continue
        call_id = item.get("id")
        fn = item.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        raw_args = fn.get("arguments")
        raw_args_str = raw_args if isinstance(raw_args, str) else ""
        args_obj: dict[str, Any] = {}
        if isinstance(raw_args, dict):
            args_obj = dict(raw_args)
            raw_args_str = json.dumps(args_obj, ensure_ascii=False)
        elif isinstance(raw_args, str) and raw_args.strip():
            try:
                loaded = json.loads(raw_args)
            except Exception:
                loaded = None
            if isinstance(loaded, dict):
                args_obj = dict(loaded)
        thought_sig = (
            item.get("thought_signature")
            or item.get("thoughtSignature")
            or fn.get("thought_signature")
            or fn.get("thoughtSignature")
        )
        thought_sig_str = str(thought_sig).strip() if isinstance(thought_sig, str) and thought_sig.strip() else None
        parsed.append(
            ToolCall(
                tool_call_id=str(call_id) if isinstance(call_id, str) and call_id else None,
                name=name.strip(),
                arguments=args_obj,
                raw_arguments=raw_args_str or None,
                thought_signature=thought_sig_str,
            )
        )
    return parsed


def _canonicalize_messages(messages: List[Any]) -> CanonicalRequest:
    system_parts: list[str] = []
    out: list[CanonicalMessage] = []
    for msg in messages:
        role = getattr(msg, "role", None)
        if role == "system":
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content.strip():
                system_parts.append(content.strip())
            continue

        tool_call_id = getattr(msg, "tool_call_id", None)
        tool_name = getattr(msg, "tool_name", None)
        tool_calls = getattr(msg, "tool_calls", None)

        content_obj = getattr(msg, "content", None)
        if isinstance(content_obj, str):
            content = content_obj
        elif content_obj is None:
            content = ""
        else:
            content = str(content_obj)

        if role == "user":
            out.append(CanonicalMessage(role=CanonicalMessageRole.USER, content=content))
            continue

        if role == "assistant":
            out.append(
                CanonicalMessage(
                    role=CanonicalMessageRole.ASSISTANT,
                    content=content,
                    tool_calls=_parse_tool_calls(tool_calls) or None,
                )
            )
            continue

        if role == "tool":
            out.append(
                CanonicalMessage(
                    role=CanonicalMessageRole.TOOL,
                    content=content,
                    tool_call_id=str(tool_call_id) if isinstance(tool_call_id, str) and tool_call_id else None,
                    tool_name=str(tool_name) if isinstance(tool_name, str) and tool_name else None,
                )
            )
            continue

        # Treat any other role as user content.
        out.append(CanonicalMessage(role=CanonicalMessageRole.USER, content=content))

    system = "\n\n".join(system_parts).strip() if system_parts else None
    return CanonicalRequest(system=system or None, messages=out, tools=[], params={})


def _usage_to_metrics(usage: LLMUsage | None) -> Any | None:
    if usage is None:
        return None
    try:
        from agno.models.metrics import Metrics
    except Exception:  # pragma: no cover
        return None

    return Metrics(
        input_tokens=int(usage.input_tokens or 0),
        output_tokens=int(usage.output_tokens or 0),
        total_tokens=int(usage.total_tokens or 0),
        cache_write_tokens=int(usage.cache_creation_input_tokens or 0),
        cache_read_tokens=int(usage.cache_read_input_tokens or 0),
    )


def _tool_calls_to_agno(tool_calls: list[ToolCall]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, tc in enumerate(tool_calls):
        if not tc.name:
            continue
        args_json = tc.raw_arguments
        if not isinstance(args_json, str) or not args_json:
            args_json = json.dumps(tc.arguments, ensure_ascii=False)
        tool_call_id = tc.tool_call_id if isinstance(tc.tool_call_id, str) and tc.tool_call_id else f"call_{idx}"
        function: dict[str, Any] = {"name": tc.name, "arguments": args_json}
        call: dict[str, Any] = {
            "id": tool_call_id,
            "type": "function",
            "function": function,
        }
        if isinstance(tc.thought_signature, str) and tc.thought_signature.strip():
            # Preserve gateway-specific thought signature so it can be echoed back on subsequent
            # Gemini requests (some gateways enforce this for tool-call turns).
            sig = tc.thought_signature.strip()
            call["thought_signature"] = sig
            call["thoughtSignature"] = sig
            # Some agno internals may drop unknown top-level keys, so also mirror into the function object.
            function["thought_signature"] = sig
            function["thoughtSignature"] = sig
        out.append(call)
    return out


def _thought_signatures(tool_calls: list[ToolCall]) -> dict[str, str]:
    out: dict[str, str] = {}
    for tc in tool_calls:
        if not (isinstance(tc.tool_call_id, str) and tc.tool_call_id.strip()):
            continue
        sig = tc.thought_signature
        if isinstance(sig, str) and sig.strip():
            out[tc.tool_call_id.strip()] = sig.strip()
    return out


class AuraAgnoModel(Model):
    """
    Agno Model implementation backed by Aura's internal LLM client adapters.

    This lets Aura keep "openai format" vs "gemini format" endpoints, while still
    using agno.Agent for tool planning/external execution and run orchestration.
    """

    def __init__(self, *, profile: ModelProfile, project_root: Any | None = None, session_id: str | None = None) -> None:
        if Model is object:  # pragma: no cover
            raise RuntimeError("agno is required to use AuraAgnoModel.")

        super().__init__(id=profile.model_name, name="AuraLLM", provider="Aura")  # type: ignore[misc]
        self.profile = profile
        self._project_root = project_root
        self._session_id = session_id
        # Some Gemini gateways require a per-tool-call signature to be echoed back when replaying the
        # assistant's tool call in subsequent turns. agno may drop this metadata from message.tool_calls,
        # so Aura caches it here and re-injects it when preparing the next request.
        self._thought_signatures_by_tool_call_id: dict[str, str] = {}

    def _inject_thought_signatures(self, request: CanonicalRequest) -> CanonicalRequest:
        if self.profile.provider_kind is not ProviderKind.GEMINI:
            return request
        if not self._thought_signatures_by_tool_call_id:
            return request
        if not request.messages:
            return request

        changed = False
        new_messages: list[CanonicalMessage] = []
        for msg in request.messages:
            if msg.role is CanonicalMessageRole.ASSISTANT and msg.tool_calls:
                new_tool_calls: list[ToolCall] = []
                tc_changed = False
                for tc in msg.tool_calls:
                    tcid = tc.tool_call_id
                    if (
                        (tc.thought_signature is None or not tc.thought_signature.strip())
                        and isinstance(tcid, str)
                        and tcid.strip()
                        and tcid.strip() in self._thought_signatures_by_tool_call_id
                    ):
                        tc_changed = True
                        sig = self._thought_signatures_by_tool_call_id[tcid.strip()]
                        new_tool_calls.append(replace(tc, thought_signature=sig))
                    else:
                        new_tool_calls.append(tc)
                if tc_changed:
                    changed = True
                    new_messages.append(replace(msg, tool_calls=new_tool_calls))
                    continue
            new_messages.append(msg)

        if not changed:
            return request
        return replace(request, messages=new_messages)

    async def ainvoke(
        self,
        messages: List[Any],
        assistant_message: Any,  # agno.models.message.Message
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        run_response: Any = None,
        compress_tool_results: bool = False,
    ) -> Any:
        from agno.exceptions import ModelAuthenticationError, ModelProviderError
        from agno.models.response import ModelResponse

        canonical = _canonicalize_messages(messages)
        canonical = CanonicalRequest(
            system=canonical.system,
            messages=canonical.messages,
            tools=_tool_dicts_to_specs(tools),
            params={},
        )
        canonical = self._inject_thought_signatures(canonical)

        trace = None
        try:
            project_root = self._project_root
            session_id = self._session_id
            run_id = getattr(run_response, "run_id", None)
            if not (isinstance(run_id, str) and run_id.strip()):
                run_id = getattr(run_response, "id", None)
            meta = getattr(run_response, "metadata", None)
            turn_id = None
            if isinstance(meta, dict):
                tid = meta.get("aura_turn_id")
                turn_id = str(tid) if isinstance(tid, str) and tid.strip() else None
            if project_root is not None and isinstance(session_id, str) and session_id.strip() and isinstance(run_id, str) and run_id.strip():
                trace = LLMTrace.maybe_create(
                    project_root=project_root,
                    session_id=session_id,
                    request_id=run_id.strip(),
                    turn_id=turn_id,
                    step_id=None,
                )
                if trace is not None:
                    trace.record_canonical_request(canonical)
        except Exception:
            trace = None

        def _complete_sync() -> LLMResponse:
            kind = self.profile.provider_kind
            if kind is ProviderKind.OPENAI_COMPATIBLE:
                return complete_openai_compatible(
                    profile=self.profile,
                    request=canonical,
                    timeout_s=self.profile.timeout_s,
                    cancel=None,
                    trace=trace,
                )
            if kind is ProviderKind.OPENAI_CODEX:
                return complete_openai_codex(
                    profile=self.profile,
                    request=canonical,
                    timeout_s=self.profile.timeout_s,
                    cancel=None,
                    trace=trace,
                )
            if kind is ProviderKind.ANTHROPIC:
                return complete_anthropic(
                    profile=self.profile,
                    request=canonical,
                    timeout_s=self.profile.timeout_s,
                    cancel=None,
                    trace=trace,
                )
            if kind is ProviderKind.GEMINI:
                return complete_gemini(
                    profile=self.profile,
                    request=canonical,
                    timeout_s=self.profile.timeout_s,
                    cancel=None,
                    trace=trace,
                )
            raise ProviderAdapterError(f"Unsupported provider_kind: {kind}")

        try:
            resp = await asyncio.to_thread(_complete_sync)
        except LLMRequestError as e:
            status = int(e.status_code) if isinstance(e.status_code, int) else 502
            msg = str(e) or "Model request failed"
            if e.code.name == "AUTH":  # Best-effort mapping.
                raise ModelAuthenticationError(msg, status_code=status, model_name=self.name) from e
            raise ModelProviderError(msg, status_code=status, model_name=self.name, model_id=self.id) from e
        except ProviderAdapterError as e:
            raise ModelProviderError(str(e) or "Invalid model request", status_code=400, model_name=self.name, model_id=self.id) from e

        # Cache thought signatures for Gemini tool calls so we can echo them back on replay.
        if resp.provider_kind is ProviderKind.GEMINI and resp.tool_calls:
            for tc in resp.tool_calls:
                tcid = tc.tool_call_id
                sig = tc.thought_signature
                if isinstance(tcid, str) and tcid.strip() and isinstance(sig, str) and sig.strip():
                    self._thought_signatures_by_tool_call_id[tcid.strip()] = sig.strip()

        return ModelResponse(
            role="assistant",
            content=resp.text,
            tool_calls=_tool_calls_to_agno(list(resp.tool_calls or [])),
            response_usage=_usage_to_metrics(resp.usage),
            provider_data={
                "aura_profile_id": resp.profile_id,
                "aura_provider_kind": resp.provider_kind.value,
                # Some Gemini gateways (Vertex-style) require a thoughtSignature to be echoed back for each
                # functionCall part in subsequent turns. Preserve it here so Aura can attach it when it
                # rebuilds canonical history from agno's external tool execution records.
                "aura_thought_signatures": _thought_signatures(list(resp.tool_calls or [])),
            },
        )

    def invoke(
        self,
        messages: List[Any],
        assistant_message: Any,
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        run_response: Any = None,
        compress_tool_results: bool = False,
    ) -> Any:
        return asyncio.run(
            self.ainvoke(
                messages=messages,
                assistant_message=assistant_message,
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                run_response=run_response,
                compress_tool_results=compress_tool_results,
            )
        )

    def invoke_stream(
        self,
        messages: List[Any],
        assistant_message: Any,
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        run_response: Any = None,
        compress_tool_results: bool = False,
    ) -> Iterator[Any]:
        raise NotImplementedError("AuraAgnoModel streaming is not implemented.")

    async def ainvoke_stream(
        self,
        messages: List[Any],
        assistant_message: Any,
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        run_response: Any = None,
        compress_tool_results: bool = False,
    ) -> Any:
        raise NotImplementedError("AuraAgnoModel streaming is not implemented.")

    def _parse_provider_response(self, response: Any, **kwargs) -> Any:  # pragma: no cover
        raise NotImplementedError

    def _parse_provider_response_delta(self, response: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def build_aura_agno_model(*, profile: ModelProfile, project_root: Any | None = None, session_id: str | None = None) -> AuraAgnoModel:
    return AuraAgnoModel(profile=profile, project_root=project_root, session_id=session_id)
