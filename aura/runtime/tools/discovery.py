from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .builtins import _maybe_bool, _maybe_int, _maybe_str_list, _require_str, _resolve_in_project


def _default_ignored_dirs() -> set[str]:
    return {
        ".git",
        ".aura",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "node_modules",
        "dist",
        "build",
    }


def _path_included(rel_path: str, *, include_globs: list[str], exclude_globs: list[str]) -> bool:
    if include_globs and not any(fnmatch.fnmatch(rel_path, g) for g in include_globs):
        return False
    if exclude_globs and any(fnmatch.fnmatch(rel_path, g) for g in exclude_globs):
        return False
    return True


@dataclass(frozen=True, slots=True)
class ProjectListDirTool:
    name: str = "project__list_dir"
    description: str = (
        "List files and directories under the project root. "
        "Use this instead of shell commands for read-only directory discovery."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative directory path (default '.')."},
                "recursive": {"type": "boolean", "description": "Recurse into subdirectories (default false)."},
                "max_depth": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Maximum recursion depth when recursive=true (default 4).",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum entries to return (default 200).",
                },
                "include_globs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional include globs matched against project-relative path.",
                },
                "exclude_globs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional exclude globs matched against project-relative path.",
                },
                "include_ignored": {
                    "type": "boolean",
                    "description": "Include entries under ignored dirs like .git/.aura (default false).",
                },
            },
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        rel_base = str(args.get("path") or ".")
        base_dir = _resolve_in_project(project_root, rel_base)
        recursive = _maybe_bool(args, "recursive") or False
        max_depth = _maybe_int(args, "max_depth")
        if max_depth is None:
            max_depth = 4
        max_results = _maybe_int(args, "max_results") or 200
        include_globs = _maybe_str_list(args, "include_globs") or []
        exclude_globs = _maybe_str_list(args, "exclude_globs") or []
        include_ignored = _maybe_bool(args, "include_ignored") or False

        ignored = set() if include_ignored else _default_ignored_dirs()
        entries: list[dict[str, Any]] = []
        truncated = False

        base_resolved = base_dir.resolve()
        root_resolved = project_root.resolve()

        def _walk(current: Path, depth: int) -> None:
            nonlocal truncated
            if truncated:
                return
            if recursive and depth > max_depth:
                return
            try:
                for child in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    name = child.name
                    if not include_ignored and name in ignored:
                        continue
                    try:
                        rel_path = str(child.resolve().relative_to(root_resolved))
                    except Exception:
                        continue
                    if not _path_included(rel_path, include_globs=include_globs, exclude_globs=exclude_globs):
                        continue
                    try:
                        st = child.stat()
                    except OSError:
                        continue
                    entries.append(
                        {
                            "path": rel_path,
                            "name": name,
                            "is_dir": child.is_dir(),
                            "size_bytes": int(getattr(st, "st_size", 0)),
                            "mtime_ms": int(getattr(st, "st_mtime", 0) * 1000),
                        }
                    )
                    if len(entries) >= max_results:
                        truncated = True
                        return
                    if recursive and child.is_dir():
                        _walk(child, depth + 1)
                        if truncated:
                            return
            except OSError:
                return

        if not base_resolved.exists() or not base_resolved.is_dir():
            return {"ok": False, "path": str(Path(rel_base)), "error": "Directory not found."}

        _walk(base_resolved, 0)

        return {
            "ok": True,
            "path": str(Path(rel_base)),
            "recursive": recursive,
            "max_depth": max_depth,
            "truncated": truncated,
            "entries": entries,
        }


@dataclass(frozen=True, slots=True)
class ProjectGlobTool:
    name: str = "project__glob"
    description: str = "Find project files by glob patterns under the project root (read-only)."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Glob patterns (e.g. ['docs/**/*.md','src/**/*.py']).",
                },
                "base": {"type": "string", "description": "Relative base directory to match within (default '.')."},
                "exclude_globs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional exclude globs matched against project-relative path.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum paths to return (default 200).",
                },
                "include_ignored": {
                    "type": "boolean",
                    "description": "Include matches under ignored dirs like .git/.aura (default false).",
                },
            },
            "required": ["patterns"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        patterns = _maybe_str_list(args, "patterns")
        if not patterns:
            raise ValueError("Missing or invalid 'patterns' (expected list of non-empty strings).")
        base = str(args.get("base") or ".")
        base_dir = _resolve_in_project(project_root, base)
        exclude_globs = _maybe_str_list(args, "exclude_globs") or []
        max_results = _maybe_int(args, "max_results") or 200
        include_ignored = _maybe_bool(args, "include_ignored") or False

        ignored = set() if include_ignored else _default_ignored_dirs()
        root_resolved = project_root.resolve()
        base_dir_resolved = base_dir.resolve()

        matches: list[str] = []
        truncated = False

        def _is_ignored_path(rel_path: str) -> bool:
            parts = Path(rel_path).parts
            return any(p in ignored for p in parts)

        for pat in patterns:
            # Walk the filesystem ourselves to stay cross-platform and keep control over ignored dirs.
            for root, dirs, files in os.walk(base_dir):
                if not include_ignored:
                    dirs[:] = [d for d in dirs if d not in ignored]
                for fn in files:
                    full = Path(root) / fn
                    try:
                        rel = str(full.resolve().relative_to(root_resolved))
                    except Exception:
                        continue
                    if not include_ignored and _is_ignored_path(rel):
                        continue
                    if exclude_globs and any(fnmatch.fnmatch(rel, g) for g in exclude_globs):
                        continue
                    try:
                        rel_to_base = str(full.resolve().relative_to(base_dir_resolved))
                    except Exception:
                        rel_to_base = None
                    if fnmatch.fnmatch(rel, pat) or (isinstance(rel_to_base, str) and fnmatch.fnmatch(rel_to_base, pat)):
                        matches.append(rel)
                        if len(matches) >= max_results:
                            truncated = True
                            break
                if truncated:
                    break
            if truncated:
                break

        # Normalize and de-dup while preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for m in matches:
            if m not in seen:
                seen.add(m)
                out.append(m)

        return {
            "ok": True,
            "base": str(Path(base)),
            "patterns": patterns,
            "truncated": truncated,
            "paths": out,
        }


@dataclass(frozen=True, slots=True)
class ProjectReadTextManyTool:
    name: str = "project__read_text_many"
    description: str = (
        "Read multiple UTF-8 text files under the project root (read-only). "
        "Each file is truncated to max_chars_per_file and the overall output is bounded."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of project-relative file paths to read.",
                },
                "max_chars_per_file": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum characters per file (default 8000).",
                },
                "max_total_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum total characters across all files (default 24000).",
                },
            },
            "required": ["paths"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        paths = _maybe_str_list(args, "paths")
        if not paths:
            raise ValueError("Missing or invalid 'paths' (expected list of non-empty strings).")
        max_chars_per_file = _maybe_int(args, "max_chars_per_file") or 8000
        max_total_chars = _maybe_int(args, "max_total_chars") or 24000

        items: list[dict[str, Any]] = []
        total = 0
        total_truncated = False

        for rel in paths:
            if total >= max_total_chars:
                total_truncated = True
                break
            file_path = _resolve_in_project(project_root, rel)
            try:
                data = file_path.read_bytes()
            except OSError as e:
                items.append({"path": str(Path(rel)), "ok": False, "error": str(e)})
                continue
            text = data.decode("utf-8", errors="replace")
            truncated = False
            if len(text) > max_chars_per_file:
                truncated = True
                text = text[:max_chars_per_file]
            remaining = max_total_chars - total
            if len(text) > remaining:
                text = text[:remaining]
                total_truncated = True
                truncated = True
            total += len(text)
            items.append({"path": str(Path(rel)), "ok": True, "truncated": truncated, "content": text})

        return {
            "ok": True,
            "max_chars_per_file": max_chars_per_file,
            "max_total_chars": max_total_chars,
            "total_chars": total,
            "total_truncated": total_truncated,
            "items": items,
        }
