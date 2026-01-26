from __future__ import annotations

import json
import os
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ids import now_ts_ms
from .config_io import parse_env_text
from .types import CanonicalRequest, LLMResponse, LLMStreamEvent, ProviderKind


def _replace_surrogates(text: str) -> str:
    out: list[str] = []
    changed = False
    for ch in text:
        o = ord(ch)
        if 0xD800 <= o <= 0xDFFF:
            out.append("\uFFFD")
            changed = True
        else:
            out.append(ch)
    return "".join(out) if changed else text


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return _replace_surrogates(value)
    if isinstance(value, list):
        return [_sanitize_json_value(v) for v in value]
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            key = _replace_surrogates(k) if isinstance(k, str) else k
            out[key] = _sanitize_json_value(v)
        return out
    return value


def _safe_write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(_sanitize_json_value(obj), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
        errors="backslashreplace",
    )
    tmp.replace(path)


def _to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]

    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            return _to_jsonable(model_dump())
        except Exception:
            pass
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            return _to_jsonable(to_dict())
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        try:
            return _to_jsonable(vars(obj))
        except Exception:
            pass
    return {"__type__": type(obj).__name__, "__repr__": repr(obj)}


def _truthy(value: str) -> bool:
    v = value.strip().lower()
    return v in {"1", "true", "yes", "y", "on"}


def _read_project_env(project_root: Path) -> dict[str, str]:
    env_path = project_root / ".aura" / "config" / "env"
    if not env_path.exists():
        return {}
    return parse_env_text(env_path.read_text(encoding="utf-8"))


def llm_trace_enabled(project_root: Path) -> bool:
    override = os.environ.get("NOVELAIRE_TRACE_LLM")
    if override is not None and override.strip() != "":
        return _truthy(override)
    env = _read_project_env(project_root)
    value = env.get("NOVELAIRE_TRACE_LLM")
    if value is None or value.strip() == "":
        return False
    return _truthy(value)


def llm_trace_root(project_root: Path) -> Path:
    override = os.environ.get("NOVELAIRE_TRACE_LLM_DIR")
    if override is not None and override.strip() != "":
        path = Path(os.path.expanduser(override.strip()))
        if not path.is_absolute():
            path = project_root / path
        return path
    env = _read_project_env(project_root)
    value = env.get("NOVELAIRE_TRACE_LLM_DIR")
    if value is not None and value.strip() != "":
        path = Path(os.path.expanduser(value.strip()))
        if not path.is_absolute():
            path = project_root / path
        return path
    return project_root / ".aura" / "cache" / "llm_trace"


@dataclass(slots=True)
class LLMTrace:
    trace_dir: Path
    session_id: str
    request_id: str
    turn_id: str | None
    step_id: str | None
    started_at_ms: int = field(default_factory=now_ts_ms)
    _meta: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def maybe_create(
        *,
        project_root: Path,
        session_id: str,
        request_id: str,
        turn_id: str | None,
        step_id: str | None,
    ) -> "LLMTrace | None":
        if not llm_trace_enabled(project_root):
            return None
        root = llm_trace_root(project_root)
        trace_dir = (root / session_id / request_id).resolve()
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace = LLMTrace(
            trace_dir=trace_dir,
            session_id=session_id,
            request_id=request_id,
            turn_id=turn_id,
            step_id=step_id,
        )
        trace.record_meta(status="started")
        return trace

    @property
    def meta_path(self) -> Path:
        return self.trace_dir / "meta.json"

    def record_meta(self, **patch: Any) -> None:
        self._meta.update(patch)
        payload = {
            "session_id": self.session_id,
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "step_id": self.step_id,
            "started_at_ms": self.started_at_ms,
            **self._meta,
        }
        _safe_write_json(self.meta_path, payload)

    def write_json(self, name: str, obj: Any) -> None:
        _safe_write_json(self.trace_dir / name, _to_jsonable(obj))

    def append_jsonl(self, name: str, obj: Any) -> None:
        path = self.trace_dir / name
        line = json.dumps(_sanitize_json_value(_to_jsonable(obj)), ensure_ascii=False)
        with path.open("a", encoding="utf-8", errors="backslashreplace") as f:
            f.write(line)
            f.write("\n")

    def record_canonical_request(self, request: CanonicalRequest) -> None:
        self.write_json(
            "canonical_request.json",
            {
                "system": request.system,
                "messages": [
                    {
                        "role": m.role.value,
                        "content": m.content,
                        "tool_call_id": m.tool_call_id,
                        "tool_name": m.tool_name,
                        "reasoning_content": m.reasoning_content,
                        "tool_calls": (
                            [
                                {
                                    "tool_call_id": tc.tool_call_id,
                                    "name": tc.name,
                                    "arguments": tc.arguments,
                                    "raw_arguments": tc.raw_arguments,
                                    "thought_signature": tc.thought_signature,
                                }
                                for tc in (m.tool_calls or [])
                            ]
                            if m.tool_calls
                            else None
                        ),
                    }
                    for m in request.messages
                ],
                "tools": [t.__dict__ for t in request.tools],
                "params": dict(request.params),
            },
        )

    def record_prepared_request(
        self,
        *,
        provider_kind: ProviderKind,
        profile_id: str,
        base_url: str,
        model: str,
        stream: bool,
        timeout_s: float | None,
        payload: dict[str, Any],
    ) -> None:
        self.record_meta(
            provider_kind=provider_kind.value,
            profile_id=profile_id,
            base_url=base_url,
            model=model,
            stream=stream,
            timeout_s=timeout_s,
        )
        self.write_json(
            "prepared_request.json",
            {
                "provider_kind": provider_kind.value,
                "profile_id": profile_id,
                "base_url": base_url,
                "model": model,
                "stream": stream,
                "timeout_s": timeout_s,
                "payload": payload,
            },
        )

    def record_provider_item(self, item: Any) -> None:
        self.append_jsonl("provider_stream.jsonl", {"ts_ms": now_ts_ms(), "item": item})

    def record_stream_event(self, event: LLMStreamEvent) -> None:
        payload: dict[str, Any] = {"kind": event.kind.value}
        if event.text_delta is not None:
            payload["text_delta"] = event.text_delta
        if event.thinking_delta is not None:
            payload["thinking_delta"] = event.thinking_delta
        if event.tool_call_delta is not None:
            payload["tool_call_delta"] = event.tool_call_delta.__dict__
        if event.tool_call is not None:
            payload["tool_call"] = event.tool_call.__dict__
        if event.response is not None:
            payload["response"] = event.response.__dict__
        self.append_jsonl("canonical_stream.jsonl", {"ts_ms": now_ts_ms(), "event": payload})

    def record_response(self, response: LLMResponse) -> None:
        self.write_json("response.json", response.__dict__)
        self.record_meta(status="completed", finished_at_ms=now_ts_ms(), stop_reason=response.stop_reason)

    def record_cancelled(self, *, reason: str | None = None, code: str | None = None) -> None:
        self.write_json(
            "cancelled.json",
            {
                "reason": reason,
                "code": code,
            },
        )
        self.record_meta(status="cancelled", finished_at_ms=now_ts_ms(), cancel_reason=reason, error_code=code)

    def record_error(self, err: BaseException, *, code: str | None = None) -> None:
        self.write_json(
            "error.json",
            {
                "type": type(err).__name__,
                "message": str(err),
                "code": code,
                "traceback": traceback.format_exception(type(err), err, err.__traceback__),
            },
        )
        self.record_meta(status="failed", finished_at_ms=now_ts_ms(), error_type=type(err).__name__, error_code=code)
