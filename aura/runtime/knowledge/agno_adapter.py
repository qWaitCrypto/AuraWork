from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import KnowledgeConfig
from .interface import SearchResult


class KnowledgeInitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _NullEmbedder:
    """
    Offline-safe embedder that returns zero vectors.

    This exists so a keyword-only setup can avoid agno's default OpenAI embedder when a VectorDb
    requires an embedder instance even if only keyword search is used.
    """

    dimensions: int = 32
    enable_batch: bool = False
    batch_size: int = 100

    def get_embedding(self, _text: str) -> list[float]:
        return [0.0] * int(self.dimensions)

    def get_embedding_and_usage(self, text: str):
        return self.get_embedding(text), None

    async def async_get_embedding(self, text: str) -> list[float]:
        return self.get_embedding(text)

    async def async_get_embedding_and_usage(self, text: str):
        return self.get_embedding(text), None


class AgnoKnowledgeStore:
    """
    Agno Knowledge adapter.

    Implements Aura's KnowledgeStore Protocol, but is not auto-wired into the main engine.
    """

    def __init__(self, *, config: KnowledgeConfig, project_root: Path) -> None:
        self._config = config
        self.project_root = project_root.expanduser().resolve()

        if config.knowledge_dir is None:
            self._kb_dir = self.project_root / ".aura" / "knowledge"
        else:
            self._kb_dir = Path(config.knowledge_dir).expanduser().resolve()
        self._kb_dir.mkdir(parents=True, exist_ok=True)

        self._knowledge = self._init_agno_knowledge()

        if config.auto_index_on_startup:
            self.refresh()

    def _init_agno_knowledge(self):
        try:
            from agno.knowledge.knowledge import Knowledge
        except Exception as e:  # pragma: no cover
            raise KnowledgeInitError(f"agno knowledge is not available: {e}") from e

        vector_db = self._create_vector_db()
        return Knowledge(
            name="aura_knowledge",
            description="Aura project knowledge base",
            vector_db=vector_db,
            max_results=int(max(1, self._config.max_results)),
        )

    def _create_vector_db(self):
        db_type = str(self._config.vector_db or "").strip().lower()

        search_type = self._search_type()

        if db_type == "lancedb":
            try:
                from agno.vectordb.lancedb import LanceDb
            except Exception as e:  # pragma: no cover
                raise KnowledgeInitError(
                    "LanceDb backend is not available (missing dependency). "
                    "Install `lancedb` and `pyarrow` to enable."
                ) from e

            embedder = self._create_embedder()
            # Use a dedicated internal path under `.aura/` to avoid polluting repo root.
            uri = str(self.project_root / ".aura" / "vectordb")

            return LanceDb(
                table_name="aura_knowledge",
                uri=uri,
                embedder=embedder,
                search_type=search_type,
            )

        if db_type == "chromadb":
            try:
                from agno.vectordb.chroma import ChromaDb
            except Exception as e:  # pragma: no cover
                raise KnowledgeInitError(
                    "ChromaDb backend is not available (missing dependency). "
                    "Install `chromadb` to enable."
                ) from e

            embedder = self._create_embedder()
            return ChromaDb(
                collection="aura_knowledge",
                embedder=embedder,
                search_type=search_type,
            )

        raise KnowledgeInitError(f"Unsupported vector_db: {db_type}")

    def _search_type(self):
        from agno.vectordb.search import SearchType

        mode = str(self._config.retriever or "").strip().lower()
        if mode == "bm25":
            return SearchType.keyword
        if mode == "embedding":
            return SearchType.vector
        if mode == "hybrid":
            return SearchType.hybrid
        return SearchType.hybrid

    def _create_embedder(self):
        retriever = str(self._config.retriever or "").strip().lower()
        if retriever == "bm25":
            # Avoid network dependencies by default.
            return _NullEmbedder()

        embed_model = str(self._config.embed_model or "").strip()
        if not embed_model:
            raise KnowledgeInitError("Missing embed_model.")

        if embed_model.startswith("openai:"):
            try:
                from agno.knowledge.embedder.openai import OpenAIEmbedder
            except Exception as e:  # pragma: no cover
                raise KnowledgeInitError(f"OpenAIEmbedder is not available: {e}") from e

            model = embed_model.split(":", 1)[1].strip() or None
            return OpenAIEmbedder(model=model) if model else OpenAIEmbedder()

        raise KnowledgeInitError(f"Unsupported embed_model: {embed_model!r}")

    def search(self, query: str, *, max_results: int = 5, min_score: float = 0.0) -> list[SearchResult]:
        query = str(query or "").strip()
        if not query:
            return []

        docs = self._knowledge.search(query=query, max_results=int(max(1, max_results)))
        out: list[SearchResult] = []
        for doc in docs or []:
            content = getattr(doc, "content", None)
            if not isinstance(content, str) or not content.strip():
                continue
            source = getattr(doc, "name", None)
            if not isinstance(source, str) or not source:
                source = "unknown"
            meta = getattr(doc, "meta_data", None)
            metadata = dict(meta) if isinstance(meta, dict) else None
            score = getattr(doc, "reranking_score", None)
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                score = float(metadata.get("rrf_score", 0.0)) if isinstance(metadata, dict) else 0.0
            score_f = float(score)
            if score_f < float(min_score):
                continue
            out.append(SearchResult(content=content, source=source, score=score_f, metadata=metadata))
        return out

    def add_document(self, content: str, *, source: str, metadata: dict[str, Any] | None = None) -> None:
        source = str(source or "").strip()
        if not source:
            raise ValueError("Missing source.")
        content = str(content or "")
        if not content.strip():
            return
        self._knowledge.add_content(
            name=source,
            text_content=content,
            metadata=dict(metadata or {}),
            upsert=True,
            skip_if_exists=False,
        )

    def remove_document(self, source: str) -> bool:
        # agno Knowledge does not provide a portable delete-by-name API across vector DBs.
        return False

    def refresh(self) -> None:
        include = self._config.include
        exclude = self._config.exclude
        if include is None:
            include = ["**/*.md", "**/*.txt"]
        self._knowledge.add_contents(
            paths=[str(self._kb_dir)],
            include=list(include) if include is not None else None,
            exclude=list(exclude) if exclude is not None else None,
            upsert=True,
            skip_if_exists=False,
        )

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": bool(self._config.enabled),
            "knowledge_dir": str(self._kb_dir),
            "retriever": self._config.retriever,
            "vector_db": self._config.vector_db,
            "embed_model": self._config.embed_model,
        }

