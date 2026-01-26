from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ids import now_ts_ms
from .llm.types import CanonicalMessage, CanonicalMessageRole
from .project import RuntimePaths


class RunSnapshotError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PendingToolCall:
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"tool_call_id": self.tool_call_id, "tool_name": self.tool_name, "args": dict(self.args)}

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "PendingToolCall":
        tool_call_id = str(raw.get("tool_call_id") or "")
        tool_name = str(raw.get("tool_name") or "")
        args = raw.get("args")
        if not tool_call_id or not tool_name:
            raise RunSnapshotError("Invalid pending tool call (missing tool_call_id/tool_name).")
        if not isinstance(args, dict):
            args = {}
        return PendingToolCall(tool_call_id=tool_call_id, tool_name=tool_name, args=dict(args))


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    schema_version: str
    run_id: str
    session_id: str
    model_profile_id: str | None
    created_at: int
    turn_id: str | None = None
    approval_id: str | None = None
    messages: list[CanonicalMessage] = field(default_factory=list)
    pending_tools: list[PendingToolCall] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "messages": [_message_to_dict(m) for m in self.messages],
            "pending_tools": [t.to_dict() for t in self.pending_tools],
        }
        if self.model_profile_id is not None:
            out["model_profile_id"] = self.model_profile_id
        if self.turn_id is not None:
            out["turn_id"] = self.turn_id
        if self.approval_id is not None:
            out["approval_id"] = self.approval_id
        return out

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "RunSnapshot":
        schema_version = str(raw.get("schema_version") or "0.1")
        run_id = str(raw.get("run_id") or "")
        session_id = str(raw.get("session_id") or "")
        if not run_id or not session_id:
            raise RunSnapshotError("Invalid snapshot (missing run_id/session_id).")

        model_profile_id_raw = raw.get("model_profile_id")
        model_profile_id = str(model_profile_id_raw) if isinstance(model_profile_id_raw, str) and model_profile_id_raw else None
        turn_id_raw = raw.get("turn_id")
        turn_id = str(turn_id_raw) if isinstance(turn_id_raw, str) and turn_id_raw else None
        approval_id_raw = raw.get("approval_id")
        approval_id = str(approval_id_raw) if isinstance(approval_id_raw, str) and approval_id_raw else None

        created_at = raw.get("created_at")
        if not isinstance(created_at, int):
            created_at = now_ts_ms()

        messages_raw = raw.get("messages")
        messages: list[CanonicalMessage] = []
        if isinstance(messages_raw, list):
            for item in messages_raw:
                if not isinstance(item, dict):
                    continue
                messages.append(_message_from_dict(item))

        pending_raw = raw.get("pending_tools")
        pending: list[PendingToolCall] = []
        if isinstance(pending_raw, list):
            for item in pending_raw:
                if not isinstance(item, dict):
                    continue
                pending.append(PendingToolCall.from_dict(item))

        return RunSnapshot(
            schema_version=schema_version,
            run_id=run_id,
            session_id=session_id,
            model_profile_id=model_profile_id,
            created_at=int(created_at),
            turn_id=turn_id,
            approval_id=approval_id,
            messages=messages,
            pending_tools=pending,
        )


def run_snapshot_path(*, project_root: Path, run_id: str) -> Path:
    paths = RuntimePaths.for_project(project_root)
    return (paths.runs_dir / run_id / "snapshot.json").expanduser().resolve()


def write_run_snapshot(*, project_root: Path, snapshot: RunSnapshot) -> Path:
    path = run_snapshot_path(project_root=project_root, run_id=snapshot.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def read_run_snapshot(*, project_root: Path, run_id: str) -> RunSnapshot:
    path = run_snapshot_path(project_root=project_root, run_id=run_id)
    if not path.exists():
        raise FileNotFoundError(f"Run snapshot not found: {run_id}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RunSnapshotError("Run snapshot is not an object.")
    return RunSnapshot.from_dict(raw)


def delete_run_snapshot(*, project_root: Path, run_id: str) -> None:
    path = run_snapshot_path(project_root=project_root, run_id=run_id)
    try:
        path.unlink(missing_ok=True)
    except TypeError:
        # Python <3.8 compatibility (not expected here, but harmless).
        if path.exists():
            path.unlink()


def _message_to_dict(msg: CanonicalMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": msg.role.value, "content": msg.content}
    if msg.tool_call_id is not None:
        out["tool_call_id"] = msg.tool_call_id
    if msg.tool_name is not None:
        out["tool_name"] = msg.tool_name
    if msg.tool_calls is not None:
        out["tool_calls"] = [
            {
                "tool_call_id": tc.tool_call_id,
                "name": tc.name,
                "arguments": dict(tc.arguments),
                "raw_arguments": tc.raw_arguments,
                "thought_signature": tc.thought_signature,
            }
            for tc in msg.tool_calls
        ]
    if msg.reasoning_content is not None:
        out["reasoning_content"] = msg.reasoning_content
    return out


def _message_from_dict(raw: dict[str, Any]) -> CanonicalMessage:
    role_raw = str(raw.get("role") or CanonicalMessageRole.USER.value)
    try:
        role = CanonicalMessageRole(role_raw)
    except ValueError:
        role = CanonicalMessageRole.USER
    content = str(raw.get("content") or "")
    tool_call_id = raw.get("tool_call_id")
    tool_name = raw.get("tool_name")
    if not isinstance(tool_call_id, str):
        tool_call_id = None
    if not isinstance(tool_name, str):
        tool_name = None
    tool_calls_raw = raw.get("tool_calls")
    tool_calls = None
    if isinstance(tool_calls_raw, list):
        from .llm.types import ToolCall

        parsed: list[ToolCall] = []
        for item in tool_calls_raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            arguments = item.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, dict):
                continue
            parsed.append(
                ToolCall(
                    tool_call_id=item.get("tool_call_id"),
                    name=name,
                    arguments=dict(arguments),
                    raw_arguments=item.get("raw_arguments") if isinstance(item.get("raw_arguments"), str) else None,
                    thought_signature=item.get("thought_signature") if isinstance(item.get("thought_signature"), str) else None,
                )
            )
        tool_calls = parsed
    return CanonicalMessage(
        role=role,
        content=content,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_calls=tool_calls,
        reasoning_content=raw.get("reasoning_content") if isinstance(raw.get("reasoning_content"), str) else None,
    )
