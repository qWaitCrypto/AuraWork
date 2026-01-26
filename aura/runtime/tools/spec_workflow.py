from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from ..snapshots import GitSnapshotBackend
from ..ids import new_id, now_ts_ms
from ..spec_workflow import (
    SpecError,
    SpecProposal,
    SpecProposalStore,
    SpecState,
    SpecStateStore,
    SpecStore,
    build_unified_diff,
    store_diff_artifact,
)
from ..stores import ArtifactStore


@dataclass(frozen=True, slots=True)
class SpecQueryTool:
    store: SpecStore
    name: str = "spec__query"
    description: str = "Query spec entries by keyword (id/title/tags/aliases)."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1},
            },
            "required": ["query"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root) -> dict[str, Any]:
        del project_root
        query = args.get("query")
        max_results = args.get("max_results") or 20
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Missing or invalid 'query' (expected non-empty string).")
        if not isinstance(max_results, int) or isinstance(max_results, bool) or max_results < 1:
            raise ValueError("Invalid 'max_results' (expected int >= 1).")
        self.store.refresh()
        results = self.store.query(query, max_results=max_results)
        return {"ok": True, "query": query, "results": results, "warnings": self.store.warnings}


@dataclass(frozen=True, slots=True)
class SpecGetTool:
    store: SpecStore
    name: str = "spec__get"
    description: str = "Get a spec entry by id and return its markdown content."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root) -> dict[str, Any]:
        del project_root
        entry_id = args.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError("Missing or invalid 'id' (expected non-empty string).")
        self.store.refresh()
        entry, content = self.store.get(entry_id)
        return {
            "ok": True,
            "entry": {
                "id": entry.id,
                "title": entry.title,
                "path": entry.path,
                "tags": entry.tags,
                "aliases": entry.aliases,
            },
            "content": content,
        }


@dataclass(frozen=True, slots=True)
class SpecProposeTool:
    spec_store: SpecStore
    proposal_store: SpecProposalStore
    state_store: SpecStateStore
    artifact_store: ArtifactStore
    name: str = "spec__propose"
    description: str = (
        "Create a spec change proposal (diff + reason) without applying it. "
        "Use spec__apply to apply after approval."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Spec entry id (stable)."},
                "body": {"type": "string", "description": "Markdown body for the entry."},
                "title": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "aliases": {"type": "array", "items": {"type": "string"}},
                "path": {"type": "string", "description": "Optional path under spec/ (defaults to <id>.md)."},
                "reason": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["id", "body"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root) -> dict[str, Any]:
        project_root = Path(project_root).expanduser().resolve()
        entry_id = args.get("id")
        body = args.get("body")
        title = args.get("title")
        tags = args.get("tags")
        aliases = args.get("aliases")
        rel_path = args.get("path")
        reason = args.get("reason")
        citations = args.get("citations")

        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError("Missing or invalid 'id' (expected non-empty string).")
        if not isinstance(body, str):
            raise ValueError("Missing or invalid 'body' (expected string).")
        if title is not None and not isinstance(title, str):
            raise ValueError("Invalid 'title' (expected string).")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("Invalid 'reason' (expected string).")
        if rel_path is not None and not isinstance(rel_path, str):
            raise ValueError("Invalid 'path' (expected string).")
        if tags is not None and not isinstance(tags, list):
            raise ValueError("Invalid 'tags' (expected list of strings).")
        if aliases is not None and not isinstance(aliases, list):
            raise ValueError("Invalid 'aliases' (expected list of strings).")
        if citations is not None and not isinstance(citations, list):
            raise ValueError("Invalid 'citations' (expected list of strings).")

        cleaned_rel_path = None
        if isinstance(rel_path, str) and rel_path.strip():
            normalized = rel_path.strip().replace("\\", "/").lstrip("/")
            if normalized.startswith("spec/"):
                normalized = normalized[len("spec/") :]
            if normalized and Path(normalized).suffix != ".md":
                normalized = normalized + ".md"
            cleaned_rel_path = normalized or None

        self.spec_store.refresh()
        target = self.spec_store.derive_entry_path(entry_id=entry_id.strip(), rel_path=cleaned_rel_path)
        rel = str(target.relative_to(project_root))

        new_text = self.spec_store.build_entry_text(
            entry_id=entry_id.strip(),
            body=body,
            title=title if isinstance(title, str) and title.strip() else None,
            tags=[t for t in tags if isinstance(t, str) and t.strip()] if isinstance(tags, list) else None,
            aliases=[a for a in aliases if isinstance(a, str) and a.strip()] if isinstance(aliases, list) else None,
        )

        old_text = ""
        if target.exists() and target.is_file():
            old_text = target.read_text(encoding="utf-8", errors="replace")

        diff_text = build_unified_diff(rel_path=rel, old=old_text, new=new_text)
        diff_ref = store_diff_artifact(artifact_store=self.artifact_store, diff_text=diff_text, summary=f"Spec proposal diff {entry_id}")

        proposal_id = new_id("sp")
        proposal = SpecProposal(
            proposal_id=proposal_id,
            entry_id=entry_id.strip(),
            rel_path=rel,
            new_text=new_text,
            old_text=old_text,
            diff_ref=diff_ref.to_dict(),
            reason=reason.strip() if isinstance(reason, str) and reason.strip() else None,
            citations=[c for c in citations if isinstance(c, str) and c.strip()] if isinstance(citations, list) else None,
        )
        self.proposal_store.create(proposal)

        state = self.state_store.get()
        if state.status == "sealed":
            sealed_note = "Spec is sealed; you can propose, but applying will be blocked until ChangeSet workflow exists."
        else:
            sealed_note = None

        return {
            "ok": True,
            "proposal_id": proposal_id,
            "entry_id": proposal.entry_id,
            "path": proposal.rel_path,
            "diff_ref": proposal.diff_ref,
            "reason": proposal.reason,
            "citations": proposal.citations,
            "note": sealed_note,
        }


@dataclass(frozen=True, slots=True)
class SpecApplyTool:
    proposal_store: SpecProposalStore
    state_store: SpecStateStore
    name: str = "spec__apply"
    description: str = "Apply a previously created spec proposal by proposal_id (approval required)."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"proposal_id": {"type": "string"}},
            "required": ["proposal_id"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root) -> dict[str, Any]:
        project_root = Path(project_root).expanduser().resolve()
        proposal_id = args.get("proposal_id")
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            raise ValueError("Missing or invalid 'proposal_id' (expected non-empty string).")

        state = self.state_store.get()
        if state.status == "sealed":
            raise SpecError("Spec is sealed; cannot apply proposals without ChangeSet workflow.")

        proposal = self.proposal_store.get(proposal_id.strip())
        rel_path = proposal.get("rel_path")
        new_text = proposal.get("new_text")
        if not isinstance(rel_path, str) or not rel_path:
            raise SpecError("Invalid proposal (missing rel_path).")
        if not isinstance(new_text, str):
            raise SpecError("Invalid proposal (missing new_text).")
        diff_ref = proposal.get("diff_ref") if isinstance(proposal.get("diff_ref"), dict) else None
        reason = proposal.get("reason") if isinstance(proposal.get("reason"), str) else None
        citations = proposal.get("citations") if isinstance(proposal.get("citations"), list) else None

        target = (project_root / rel_path).resolve()
        spec_root = (project_root / "spec").resolve()
        if target != spec_root and spec_root not in target.parents:
            raise SpecError("Proposal path escapes spec/ directory.")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_text, encoding="utf-8")
        return {
            "ok": True,
            "applied": True,
            "proposal_id": proposal_id.strip(),
            "path": rel_path,
            "diff_ref": diff_ref,
            "reason": reason,
            "citations": citations,
        }


@dataclass(frozen=True, slots=True)
class SpecSealTool:
    state_store: SpecStateStore
    snapshots: GitSnapshotBackend
    name: str = "spec__seal"
    description: str = "Seal spec into a version label (approval required). Creates a snapshot and marks spec read-only."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root) -> dict[str, Any]:
        del project_root
        label = args.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("Missing or invalid 'label' (expected non-empty string).")

        state = self.state_store.get()
        if state.status == "sealed":
            raise SpecError(f"Spec is already sealed ({state.label}).")

        snap = self.snapshots.snapshot_create(reason=f"spec seal {label.strip()}")
        tagged = self.snapshots.snapshot_label(label=label.strip())
        self.state_store.set(SpecState(status="sealed", label=tagged.label, sealed_at=now_ts_ms()))
        return {"ok": True, "status": "sealed", "label": tagged.label, "commit": tagged.commit}


def load_proposal_diff_ref(*, project_root: Path, proposal_id: str) -> ArtifactRef | None:
    """
    Load a proposal record and return its diff_ref (if present and well-formed).

    Used by the inspector to attach approval previews without recomputing diffs.
    """

    store = SpecProposalStore(project_root=project_root)
    record = store.get(proposal_id)
    raw = record.get("diff_ref")
    if not isinstance(raw, dict):
        return None
    try:
        return ArtifactRef.from_dict(raw)
    except Exception:
        return None
