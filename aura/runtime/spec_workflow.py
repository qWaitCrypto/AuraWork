from __future__ import annotations

import json
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Any

from .frontmatter import FrontmatterError, parse_markdown_frontmatter
from .ids import new_id, now_ts_ms
from .protocol import ArtifactRef
from .stores import ArtifactStore


class SpecError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SpecState:
    status: str  # open|sealed
    label: str | None = None
    sealed_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if self.label is not None:
            out["label"] = self.label
        if self.sealed_at is not None:
            out["sealed_at"] = self.sealed_at
        return out

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "SpecState":
        status = str(raw.get("status") or "open")
        if status not in {"open", "sealed"}:
            status = "open"
        label = raw.get("label")
        if label is not None and not isinstance(label, str):
            label = None
        sealed_at = raw.get("sealed_at")
        if sealed_at is not None:
            try:
                sealed_at = int(sealed_at)
            except Exception:
                sealed_at = None
        return SpecState(status=status, label=label, sealed_at=sealed_at)


class SpecStateStore:
    def __init__(self, *, project_root: Path) -> None:
        self._project_root = project_root.expanduser().resolve()
        self._path = self._project_root / ".aura" / "state" / "spec_state.json"

    def get(self) -> SpecState:
        if not self._path.exists():
            return SpecState(status="open")
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return SpecState(status="open")
        if not isinstance(raw, dict):
            return SpecState(status="open")
        return SpecState.from_dict(raw)

    def set(self, state: SpecState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._path)


@dataclass(frozen=True, slots=True)
class SpecEntry:
    id: str
    path: str
    title: str | None = None
    tags: list[str] | None = None
    aliases: list[str] | None = None


class SpecStore:
    def __init__(self, *, project_root: Path) -> None:
        self._project_root = project_root.expanduser().resolve()
        self._spec_root = self._project_root / "spec"
        self._by_id: dict[str, SpecEntry] = {}
        self._warnings: list[str] = []
        self._did_refresh = False

    @property
    def warnings(self) -> list[str]:
        self._ensure_ready()
        return list(self._warnings)

    def _ensure_ready(self) -> None:
        if not self._did_refresh:
            self.refresh()

    def refresh(self) -> None:
        self._by_id = {}
        self._warnings = []
        self._did_refresh = True
        if not self._spec_root.exists():
            return
        if not self._spec_root.is_dir():
            self._warnings.append("spec/ exists but is not a directory.")
            return

        for path in sorted(self._spec_root.rglob("*.md")):
            try:
                if not path.is_file() or path.is_symlink():
                    continue
            except OSError:
                continue
            rel = str(path.relative_to(self._project_root))
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                self._warnings.append(f"Failed to read spec entry: {rel}: {e}")
                continue
            try:
                parsed = parse_markdown_frontmatter(text)
            except FrontmatterError as e:
                self._warnings.append(f"Failed to parse spec frontmatter: {rel}: {e}")
                continue
            entry_id = parsed.data.get("id")
            if not isinstance(entry_id, str) or not entry_id.strip():
                self._warnings.append(f"Skipped spec entry missing id: {rel}")
                continue
            entry_id = entry_id.strip()
            if entry_id in self._by_id:
                self._warnings.append(f"Duplicate spec id '{entry_id}' at {rel}; keeping first.")
                continue
            title = parsed.data.get("title")
            title = title.strip() if isinstance(title, str) and title.strip() else None
            tags = _parse_str_list(parsed.data.get("tags"))
            aliases = _parse_str_list(parsed.data.get("aliases"))
            self._by_id[entry_id] = SpecEntry(
                id=entry_id,
                path=rel,
                title=title,
                tags=tags,
                aliases=aliases,
            )

    def get(self, entry_id: str) -> tuple[SpecEntry, str]:
        self._ensure_ready()
        entry_id = entry_id.strip()
        entry = self._by_id.get(entry_id)
        if entry is None:
            raise SpecError(f"Spec entry not found: {entry_id}")
        path = (self._project_root / entry.path).resolve()
        return entry, path.read_text(encoding="utf-8", errors="replace")

    def query(self, query: str, *, max_results: int = 20) -> list[dict[str, Any]]:
        self._ensure_ready()
        q = query.strip().lower()
        if not q:
            return []
        results: list[dict[str, Any]] = []
        for entry_id in sorted(self._by_id):
            entry = self._by_id[entry_id]
            hay = " ".join(
                [
                    entry.id,
                    entry.title or "",
                    " ".join(entry.tags or []),
                    " ".join(entry.aliases or []),
                ]
            ).lower()
            if q in hay:
                results.append(
                    {
                        "id": entry.id,
                        "title": entry.title,
                        "path": entry.path,
                    }
                )
                if len(results) >= max_results:
                    break
        return results

    def build_entry_text(
        self,
        *,
        entry_id: str,
        body: str,
        title: str | None = None,
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
    ) -> str:
        entry_id = entry_id.strip()
        if not entry_id:
            raise SpecError("Missing id.")
        if ".." in entry_id or entry_id.startswith("/") or entry_id.startswith("\\"):
            raise SpecError("Invalid id.")
        fm_lines = ["---", f'id: "{entry_id}"']
        if title:
            fm_lines.append(f'title: "{title.strip()}"')
        if tags:
            fm_lines.append("tags:")
            for t in tags:
                fm_lines.append(f"  - {t}")
        if aliases:
            fm_lines.append("aliases:")
            for a in aliases:
                fm_lines.append(f"  - {a}")
        fm_lines.append("---")
        text = "\n".join(fm_lines) + "\n\n" + body.lstrip("\n")
        if not text.endswith("\n"):
            text += "\n"
        return text

    def derive_entry_path(self, *, entry_id: str, rel_path: str | None = None) -> Path:
        if rel_path is None:
            rel_path = f"{entry_id}.md"
        rel = Path(rel_path)
        if rel.is_absolute():
            raise SpecError("Spec path must be relative.")
        if any(part in {"..", ""} for part in rel.parts):
            raise SpecError("Invalid spec path.")
        target = (self._spec_root / rel).resolve()
        spec_root = self._spec_root.resolve()
        if target != spec_root and spec_root not in target.parents:
            raise SpecError("Spec path escapes spec/ directory.")
        return target


@dataclass(frozen=True, slots=True)
class SpecProposal:
    proposal_id: str
    entry_id: str
    rel_path: str
    new_text: str
    old_text: str
    diff_ref: dict[str, Any] | None
    reason: str | None = None
    citations: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "entry_id": self.entry_id,
            "rel_path": self.rel_path,
            "new_text": self.new_text,
            "old_text": self.old_text,
            "diff_ref": self.diff_ref,
            "reason": self.reason,
            "citations": self.citations,
            "created_at": now_ts_ms(),
        }


class SpecProposalStore:
    def __init__(self, *, project_root: Path) -> None:
        self._project_root = project_root.expanduser().resolve()
        self._root = self._project_root / ".aura" / "state" / "spec" / "proposals"
        self._root.mkdir(parents=True, exist_ok=True)

    def create(self, proposal: SpecProposal) -> None:
        path = self._root / f"{proposal.proposal_id}.json"
        if path.exists():
            raise SpecError(f"Proposal already exists: {proposal.proposal_id}")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(proposal.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def get(self, proposal_id: str) -> dict[str, Any]:
        path = self._root / f"{proposal_id}.json"
        if not path.exists():
            raise SpecError(f"Proposal not found: {proposal_id}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SpecError("Invalid proposal record.")
        return raw


def build_unified_diff(*, rel_path: str, old: str, new: str) -> str:
    lines = list(
        unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
    )
    return "".join(lines) or "(no diff)"


def store_diff_artifact(*, artifact_store: ArtifactStore, diff_text: str, summary: str) -> ArtifactRef:
    return artifact_store.put(diff_text, kind="diff", meta={"summary": summary})


def _parse_str_list(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if item:
            out.append(item)
    return out or None
