from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..snapshots import GitSnapshotBackend


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
    return value


@dataclass(frozen=True, slots=True)
class SnapshotListTool:
    snapshots: GitSnapshotBackend
    name: str = "snapshot__list"
    description: str = (
        "List available internal snapshot labels (version points). "
        "These are lightweight git tags stored under .aura/state/git. Read-only."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum labels to return (default 50).",
                    "minimum": 1,
                }
            },
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        del project_root
        max_results = _maybe_int(args, "max_results") or 50
        return {
            "initialized": self.snapshots.is_initialized(),
            "labels": self.snapshots.list_labels(max_results=max_results),
        }


@dataclass(frozen=True, slots=True)
class SnapshotCreateTool:
    snapshots: GitSnapshotBackend
    name: str = "snapshot__create"
    description: str = (
        "Create an internal snapshot (version point). "
        "This writes an internal git commit under .aura/state/git, without requiring a user .git repo."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why this snapshot is created (for the commit message)."},
                "label": {"type": "string", "description": "Optional label/tag for this snapshot version point."},
                "force_label": {"type": "boolean", "description": "Overwrite existing label (default false)."},
            },
            "required": ["reason"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        del project_root
        reason = _require_str(args, "reason")
        label = args.get("label")
        force_label = args.get("force_label")
        if label is not None and not isinstance(label, str):
            raise ValueError("Invalid 'label' (expected string).")
        if force_label is None:
            force_label = False
        if not isinstance(force_label, bool):
            raise ValueError("Invalid 'force_label' (expected boolean).")

        clean_label = label.strip() if isinstance(label, str) and label.strip() else None
        if clean_label and not force_label:
            for item in self.snapshots.list_labels(max_results=1000):
                if item.get("label") == clean_label:
                    raise ValueError(f"Snapshot label already exists: {clean_label} (use force_label=true to overwrite).")

        snap = self.snapshots.snapshot_create(reason=reason)
        tagged = None
        if clean_label:
            tagged = self.snapshots.snapshot_label(label=clean_label)
        return {
            "ok": True,
            "commit": tagged.commit if tagged else snap.commit,
            "label": tagged.label if tagged else None,
            "reason": reason,
        }


@dataclass(frozen=True, slots=True)
class SnapshotReadTextTool:
    snapshots: GitSnapshotBackend
    name: str = "snapshot__read_text"
    description: str = (
        "Read a UTF-8 text file as it existed at a specific snapshot ref (label/tag or commit). "
        "Reads from the internal snapshot repository under .aura/state/git. Read-only."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Snapshot ref (label/tag or commit SHA)."},
                "path": {"type": "string", "description": "Relative path within the project root."},
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 8000).",
                    "minimum": 1,
                },
            },
            "required": ["ref", "path"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        del project_root
        ref = _require_str(args, "ref")
        path = _require_str(args, "path")
        max_chars = _maybe_int(args, "max_chars") or 8000
        text = self.snapshots.read_text(ref=ref, path=path)
        truncated = False
        if len(text) > max_chars:
            truncated = True
            text = text[:max_chars]
        return {"ref": ref, "path": path, "truncated": truncated, "content": text}


@dataclass(frozen=True, slots=True)
class SnapshotDiffTool:
    snapshots: GitSnapshotBackend
    name: str = "snapshot__diff"
    description: str = (
        "Show a unified diff between two snapshot refs (labels/tags or commit SHAs). "
        "Optionally limit to a subpath. Read-only."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "a": {"type": "string", "description": "Snapshot ref A (label/tag or commit SHA)."},
                "b": {"type": "string", "description": "Snapshot ref B (label/tag or commit SHA)."},
                "path": {"type": "string", "description": "Optional subpath to diff (e.g. 'spec/')."},
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 20000).",
                    "minimum": 1,
                },
            },
            "required": ["a", "b"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        del project_root
        a = _require_str(args, "a")
        b = _require_str(args, "b")
        path = args.get("path")
        if path is not None and not isinstance(path, str):
            raise ValueError("Invalid 'path' (expected string).")
        max_chars = _maybe_int(args, "max_chars") or 20000
        diff_text = self.snapshots.diff(a=a, b=b, path=path)
        truncated = False
        if len(diff_text) > max_chars:
            truncated = True
            diff_text = diff_text[:max_chars]
        return {"a": a, "b": b, "path": path, "truncated": truncated, "diff": diff_text}


@dataclass(frozen=True, slots=True)
class SnapshotRollbackTool:
    snapshots: GitSnapshotBackend
    name: str = "snapshot__rollback"
    description: str = (
        "Rollback the project working tree to a snapshot ref (label/tag or commit SHA). "
        "This is destructive and will overwrite current files; approval should be required."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Snapshot ref to rollback to (label/tag or commit SHA)."},
                "create_backup": {
                    "type": "boolean",
                    "description": "Create a backup snapshot before rollback (default true).",
                },
                "backup_label": {"type": "string", "description": "Optional label/tag for the backup snapshot."},
                "force_backup_label": {"type": "boolean", "description": "Overwrite existing backup label (default false)."},
            },
            "required": ["target"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        del project_root
        target = _require_str(args, "target")
        create_backup = args.get("create_backup")
        if create_backup is None:
            create_backup = True
        if not isinstance(create_backup, bool):
            raise ValueError("Invalid 'create_backup' (expected boolean).")

        backup_label = args.get("backup_label")
        force_backup_label = args.get("force_backup_label")
        if backup_label is not None and not isinstance(backup_label, str):
            raise ValueError("Invalid 'backup_label' (expected string).")
        if force_backup_label is None:
            force_backup_label = False
        if not isinstance(force_backup_label, bool):
            raise ValueError("Invalid 'force_backup_label' (expected boolean).")

        backup_commit: str | None = None
        backup_label_out: str | None = None
        clean_backup_label = backup_label.strip() if isinstance(backup_label, str) and backup_label.strip() else None
        if create_backup:
            if clean_backup_label and not force_backup_label:
                for item in self.snapshots.list_labels(max_results=1000):
                    if item.get("label") == clean_backup_label:
                        raise ValueError(
                            f"Backup label already exists: {clean_backup_label} (use force_backup_label=true to overwrite)."
                        )
            backup = self.snapshots.snapshot_create(reason=f"backup before rollback to {target}")
            backup_commit = backup.commit
            if clean_backup_label:
                tagged = self.snapshots.snapshot_label(label=clean_backup_label)
                backup_commit = tagged.commit
                backup_label_out = tagged.label

        self.snapshots.snapshot_rollback(target=target)
        return {
            "ok": True,
            "rolled_back_to": target,
            "backup_created": bool(create_backup),
            "backup_commit": backup_commit,
            "backup_label": backup_label_out,
        }
