from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Protocol

from .models.audit_event import AuditEvent, AuditEventType


class AuditStore(Protocol):
    def append(self, event: AuditEvent) -> None: ...

    def query(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
        event_types: Iterable[AuditEventType] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[AuditEvent]: ...


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


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return iter(())
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except Exception:
                continue
            if isinstance(raw, dict):
                yield raw


@dataclass(slots=True)
class FileAuditStore:
    """
    JSONL-backed audit store under `<project>/.aura/audit/`.

    Storage strategy:
    - If event.run_id is present, append to `<audit_dir>/<run_id>.jsonl`.
    - Otherwise, append to `<audit_dir>/audit.jsonl`.
    """

    audit_dir: Path

    def __post_init__(self) -> None:
        self.audit_dir = self.audit_dir.expanduser().resolve()
        self.audit_dir.mkdir(parents=True, exist_ok=True)

    def _path_for_run(self, run_id: str | None) -> Path:
        if isinstance(run_id, str) and run_id.strip():
            safe = "".join(ch for ch in run_id.strip() if ch.isalnum() or ch in {"-", "_", "."})
            if safe:
                return self.audit_dir / f"{safe}.jsonl"
        return self.audit_dir / "audit.jsonl"

    def append(self, event: AuditEvent) -> None:
        path = self._path_for_run(event.run_id)
        obj = _sanitize_json_value(event.model_dump(mode="json"))
        line = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8", errors="backslashreplace") as f:
            f.write(line)
            f.write("\n")

    def query(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
        event_types: Iterable[AuditEventType] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[AuditEvent]:
        if limit is not None and limit < 1:
            return []

        type_set = set(event_types) if event_types is not None else None

        paths: list[Path]
        if run_id is not None and str(run_id).strip():
            paths = [self._path_for_run(str(run_id))]
        else:
            paths = sorted(self.audit_dir.glob("*.jsonl"))

        out: list[AuditEvent] = []
        for path in paths:
            for raw in _iter_jsonl(path):
                try:
                    event = AuditEvent.model_validate(raw)
                except Exception:
                    continue

                if run_id is not None and str(run_id).strip():
                    if event.run_id != str(run_id).strip():
                        continue
                if task_id is not None and str(task_id).strip():
                    if event.task_id != str(task_id).strip():
                        continue
                if node_id is not None and str(node_id).strip():
                    if event.node_id != str(node_id).strip():
                        continue
                if type_set is not None and event.event_type not in type_set:
                    continue
                if since is not None and event.timestamp < since:
                    continue
                if until is not None and event.timestamp > until:
                    continue

                out.append(event)
                if limit is not None and len(out) >= limit:
                    return out
        return out

