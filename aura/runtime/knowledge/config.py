from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class KnowledgeConfig:
    """
    RAG configuration.

    Note: default is disabled; Aura does not auto-inject knowledge into the main chat flow.
    """

    enabled: bool = False
    knowledge_dir: Path | None = None  # default: <project>/.aura/knowledge

    # "bm25" | "embedding" | "hybrid"
    retriever: str = "hybrid"

    # agno VectorDb backend identifier (implementation-dependent)
    vector_db: str = "lancedb"

    # embedder identifier/config (backend-dependent)
    embed_model: str = "openai:text-embedding-3-small"

    max_results: int = 5
    min_score: float = 0.0
    auto_index_on_startup: bool = False

    # Include/exclude patterns passed to agno Knowledge readers (optional)
    include: list[str] | None = None
    exclude: list[str] | None = None

