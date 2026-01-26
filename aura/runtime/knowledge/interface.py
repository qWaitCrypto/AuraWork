from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Single retrieval result."""

    content: str
    source: str
    score: float = 0.0
    metadata: dict[str, Any] | None = None


class KnowledgeStore(Protocol):
    """Project knowledge retrieval interface (default not enabled)."""

    project_root: Path

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        min_score: float = 0.0,
    ) -> list[SearchResult]:
        """Search the knowledge base and return relevant chunks."""

    def add_document(
        self,
        content: str,
        *,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add a document to the knowledge base."""

    def remove_document(self, source: str) -> bool:
        """Remove a document from the knowledge base."""

    def refresh(self) -> None:
        """(Re)index from the knowledge directory."""

    def stats(self) -> dict[str, Any]:
        """Return basic stats (doc count, index location, etc)."""

