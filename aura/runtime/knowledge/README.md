# Knowledge / RAG (default disabled)

This module provides Aura’s **RAG infrastructure** behind a small Aura-layer abstraction (`KnowledgeStore`).

## Principles
- Implemented but **NOT enabled by default** (no automatic prompt injection).
- Session/history/compaction stays independent from knowledge.
- Minimal “magic”: Aura controls when retrieval happens.
- Interface-first: swap implementations without changing the engine contract.

## Files
- `aura/runtime/knowledge/interface.py`: `KnowledgeStore` protocol + `SearchResult`
- `aura/runtime/knowledge/config.py`: `KnowledgeConfig`
- `aura/runtime/knowledge/agno_adapter.py`: `AgnoKnowledgeStore` (uses agno `Knowledge`)

## Storage layout
Default knowledge directory is:
- `<project>/.aura/knowledge/`

Vector DB storage (backend-dependent) defaults to:
- `<project>/.aura/vectordb/` (when using LanceDb)

## Not integrated by default
`AgnoAsyncEngine` keeps a placeholder for a knowledge store, but it is **not used** in the main chat flow unless you wire it in explicitly.

## How to enable (examples)

### A) As a tool (recommended first step)
Implement a tool like `knowledge__search` that calls `store.search(...)`, and register it only when you opt in.

### B) Prompt injection (explicit)
Before invoking the model, call `store.search(user_query)` and append retrieved context into the system message.

## Dependencies
Agno knowledge requires a VectorDB backend. Many backends require extra packages.

Examples:
- LanceDb: `pip install lancedb pyarrow` (keyword/hybrid also needs `tantivy-py`)
- ChromaDb: `pip install chromadb`

Aura does not auto-install any of these.

