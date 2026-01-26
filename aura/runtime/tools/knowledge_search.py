from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..knowledge.interface import KnowledgeStore


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing or invalid '{key}' (expected non-empty string).")
    return value.strip()


def _maybe_int(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Invalid '{key}' (expected int).")
    return int(value)


def _maybe_float(args: dict[str, Any], key: str) -> float | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid '{key}' (expected number).")
    return float(value)


@dataclass(frozen=True, slots=True)
class KnowledgeSearchTool:
    """
    Optional tool: project knowledge retrieval.

    Not registered by default; you must opt-in by wiring a KnowledgeStore into the engine/tool registry.
    """

    store: KnowledgeStore
    name: str = "knowledge__search"
    description: str = "Search project knowledge base for relevant context."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 5."},
                "min_score": {"type": "number", "description": "Default 0.0."},
            },
            "required": ["query"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        query = _require_str(args, "query")
        max_results = _maybe_int(args, "max_results") or 5
        min_score = _maybe_float(args, "min_score") or 0.0
        results = self.store.search(query, max_results=max_results, min_score=min_score)
        return {
            "ok": True,
            "results": [
                {
                    "content": r.content,
                    "source": r.source,
                    "score": r.score,
                    "metadata": r.metadata,
                }
                for r in results
            ],
        }

