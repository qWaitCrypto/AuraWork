from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..approval import ApprovalRecord, ApprovalStatus
from ..protocol import ArtifactRef, Event


class SessionStore(ABC):
    @abstractmethod
    def create_session(self, meta: dict[str, Any]) -> str: ...

    @abstractmethod
    def get_session(self, session_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def list_sessions(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def update_session(self, session_id: str, patch: dict[str, Any]) -> None: ...


class EventLogStore(ABC):
    @abstractmethod
    def append(self, event: Event) -> None: ...

    @abstractmethod
    def read(self, session_id: str, since_event_id: str | None = None) -> Iterator[Event]: ...

    @abstractmethod
    def export_bundle(self, session_id: str, output_dir: Path) -> Path: ...


class ArtifactStore(ABC):
    @abstractmethod
    def put(self, content: str | bytes, *, kind: str, meta: dict[str, Any] | None = None) -> ArtifactRef: ...

    @abstractmethod
    def get(self, artifact_ref: ArtifactRef) -> bytes: ...

    @abstractmethod
    def open_locator(self, locator: str) -> bytes: ...

    @abstractmethod
    def resolve_path(self, artifact_ref: ArtifactRef) -> Path: ...

    @abstractmethod
    def prune(self, policy: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def iter_paths(self, refs: Iterable[ArtifactRef]) -> Iterator[Path]:
        for ref in refs:
            yield self.resolve_path(ref)


class ApprovalStore(ABC):
    @abstractmethod
    def create(self, record: ApprovalRecord) -> None: ...

    @abstractmethod
    def get(self, approval_id: str) -> ApprovalRecord: ...

    @abstractmethod
    def list(
        self,
        *,
        session_id: str,
        status: ApprovalStatus | None = None,
        request_id: str | None = None,
    ) -> list[ApprovalRecord]: ...

    @abstractmethod
    def update(self, record: ApprovalRecord) -> None: ...
