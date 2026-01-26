from __future__ import annotations

import json
import shutil
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator

from ..approval import ApprovalRecord, ApprovalStatus
from ..ids import new_id, now_ts_ms
from ..protocol import ArtifactRef, Event
from .base import ApprovalStore, ArtifactStore, EventLogStore, SessionStore


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


class FileSessionStore(SessionStore):
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.json"

    def create_session(self, meta: dict[str, Any]) -> str:
        session_id = str(meta.get("session_id") or new_id("sess"))
        now = now_ts_ms()
        meta_out = dict(meta)
        meta_out["session_id"] = session_id
        meta_out.setdefault("created_at", now)
        meta_out["updated_at"] = now
        _safe_write_json(self._path(session_id), meta_out)
        return session_id

    def get_session(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list_sessions(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        filters = dict(filters or {})
        for path in sorted(self._root.glob("*.json")):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if _matches_filters(meta, filters):
                out.append(meta)
        out.sort(key=lambda m: (m.get("updated_at") or 0, m.get("created_at") or 0), reverse=True)
        return out

    def update_session(self, session_id: str, patch: dict[str, Any]) -> None:
        meta = self.get_session(session_id)
        meta.update(patch)
        meta["updated_at"] = now_ts_ms()
        _safe_write_json(self._path(session_id), meta)


def _matches_filters(meta: dict[str, Any], filters: dict[str, Any]) -> bool:
    for key, expected in filters.items():
        if expected is None:
            continue
        if meta.get(key) != expected:
            return False
    return True


class FileArtifactStore(ArtifactStore):
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, content: str | bytes, *, kind: str, meta: dict[str, Any] | None = None) -> ArtifactRef:
        if isinstance(content, str):
            payload = _replace_surrogates(content).encode("utf-8")
        else:
            payload = content
        digest = sha256(payload).hexdigest()
        artifact_id = new_id("art")
        rel = f"{artifact_id}.bin"
        path = self._root / rel
        path.write_bytes(payload)
        return ArtifactRef(
            artifact_id=artifact_id,
            artifact_kind=kind,
            locator=rel,
            created_at=now_ts_ms(),
            sha256=digest,
            size_bytes=len(payload),
            mime=None,
            summary=(str(meta.get("summary")) if meta and meta.get("summary") is not None else None),
            meta=dict(meta or {}),
        )

    def get(self, artifact_ref: ArtifactRef) -> bytes:
        return self.open_locator(artifact_ref.locator)

    def open_locator(self, locator: str) -> bytes:
        path = (self._root / locator).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Artifact not found: {locator}")
        return path.read_bytes()

    def resolve_path(self, artifact_ref: ArtifactRef) -> Path:
        return (self._root / artifact_ref.locator).resolve()

    def prune(self, policy: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"deleted": 0, "policy": dict(policy or {})}


class FileEventLogStore(EventLogStore):
    def __init__(self, root: Path, *, artifact_store: ArtifactStore, session_store: SessionStore) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._artifact_store = artifact_store
        self._session_store = session_store

    def _path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.jsonl"

    def append(self, event: Event) -> None:
        path = self._path(event.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", errors="backslashreplace") as f:
            f.write(json.dumps(_sanitize_json_value(event.to_dict()), ensure_ascii=False))
            f.write("\n")

    def read(self, session_id: str, since_event_id: str | None = None) -> Iterator[Event]:
        path = self._path(session_id)
        if not path.exists():
            return iter(())
        seen_anchor = since_event_id is None
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = Event.from_dict(raw)
                if not seen_anchor:
                    if event.event_id == since_event_id:
                        seen_anchor = True
                    continue
                yield event

    def export_bundle(self, session_id: str, output_dir: Path) -> Path:
        output_dir = output_dir.expanduser().resolve()
        bundle_dir = output_dir / f"aura_bundle_{session_id}_{now_ts_ms()}"
        bundle_dir.mkdir(parents=True, exist_ok=False)

        session_meta = self._session_store.get_session(session_id)
        _safe_write_json(bundle_dir / "session.json", session_meta)

        events_path = self._path(session_id)
        if events_path.exists():
            shutil.copyfile(events_path, bundle_dir / "events.jsonl")
        else:
            (bundle_dir / "events.jsonl").write_text("", encoding="utf-8")

        artifact_refs = _collect_artifact_refs_from_events(self.read(session_id))
        artifacts_dir = bundle_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        for ref in artifact_refs:
            src = self._artifact_store.resolve_path(ref)
            if src.exists() and src.is_file():
                shutil.copyfile(src, artifacts_dir / Path(ref.locator).name)

        _safe_write_json(
            bundle_dir / "bundle.json",
            {
                "session_id": session_id,
                "exported_at": now_ts_ms(),
                "artifacts": [r.to_dict() for r in artifact_refs],
            },
        )
        return bundle_dir


class FileApprovalStore(ApprovalStore):
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, approval_id: str) -> Path:
        return self._root / f"{approval_id}.json"

    def create(self, record: ApprovalRecord) -> None:
        path = self._path(record.approval_id)
        if path.exists():
            raise FileExistsError(f"Approval already exists: {record.approval_id}")
        _safe_write_json(path, record.to_dict())

    def get(self, approval_id: str) -> ApprovalRecord:
        path = self._path(approval_id)
        if not path.exists():
            raise FileNotFoundError(f"Approval not found: {approval_id}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return ApprovalRecord.from_dict(raw)

    def list(
        self,
        *,
        session_id: str,
        status: ApprovalStatus | None = None,
        request_id: str | None = None,
    ) -> list[ApprovalRecord]:
        out: list[ApprovalRecord] = []
        for path in sorted(self._root.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            try:
                rec = ApprovalRecord.from_dict(raw)
            except Exception:
                continue
            if rec.session_id != session_id:
                continue
            if status is not None and rec.status is not status:
                continue
            if request_id is not None and rec.request_id != request_id:
                continue
            out.append(rec)
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out

    def update(self, record: ApprovalRecord) -> None:
        path = self._path(record.approval_id)
        if not path.exists():
            raise FileNotFoundError(f"Approval not found: {record.approval_id}")
        _safe_write_json(path, record.to_dict())


def _collect_artifact_refs_from_events(events: Iterator[Event]) -> list[ArtifactRef]:
    seen: set[str] = set()
    out: list[ArtifactRef] = []
    for event in events:
        for ref in _scan_for_artifact_refs(event.to_dict()):
            if ref.artifact_id in seen:
                continue
            seen.add(ref.artifact_id)
            out.append(ref)
    return out


def _scan_for_artifact_refs(value: Any) -> Iterator[ArtifactRef]:
    if isinstance(value, dict):
        if {"artifact_id", "artifact_kind", "locator", "created_at"} <= set(value.keys()):
            try:
                yield ArtifactRef.from_dict(value)
            except Exception:
                pass
        for v in value.values():
            yield from _scan_for_artifact_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from _scan_for_artifact_refs(v)
