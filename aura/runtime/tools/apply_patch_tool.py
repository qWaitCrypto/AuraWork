from __future__ import annotations

from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path
from typing import Any

from .apply_patch_engine import derive_new_contents_from_chunks, list_patch_target_paths, parse_patch


def _resolve_in_project(project_root: Path, rel_path: str) -> Path:
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise ValueError("Path must be a non-empty string.")
    rel_path = rel_path.strip()
    rel = Path(rel_path)
    if rel.is_absolute():
        raise PermissionError("File references can only be relative, never absolute.")
    target = (project_root / rel).resolve()
    root = project_root.resolve()
    if target != root and root not in target.parents:
        raise PermissionError("Path escapes project root.")
    return target


def _render_unified_diff(old: str, new: str, *, path: str, context: int = 1) -> str:
    diff_lines = list(
        unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context,
        )
    )
    return "".join(diff_lines) or "(no diff)"


def _truncate_text(s: str, *, max_chars: int) -> tuple[str, bool]:
    if max_chars < 1:
        return "", True
    if len(s) <= max_chars:
        return s, False
    return s[:max_chars], True


@dataclass(frozen=True, slots=True)
class ProjectApplyPatchTool:
    """
    Apply a multi-file patch using the Codex apply_patch format.

    This provides robust block-based edits using explicit context and +/- lines,
    and supports Add/Delete/Update/Move operations.
    """

    name: str = "project__apply_patch"
    description: str = (
        "Apply a multi-file patch in Codex apply_patch format to UTF-8 text files under the project root. "
        "The patch MUST be raw text (no ``` fences) and MUST start with '*** Begin Patch' and end with '*** End Patch'. "
        "This is NOT unified diff: do not use '---'/'+++', and do not use range headers like '@@ -10,6 +11,7 @@'. "
        "Hunk headers must be one of: '*** Add File: <path>', '*** Delete File: <path>', '*** Update File: <path>'. "
        "Inside an update hunk, every change line must start with ' ' (context), '+' (add), or '-' (remove)."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": (
                        "Raw patch text in apply_patch format. "
                        "Do not wrap in ``` fences. Must start with '*** Begin Patch'.\n\n"
                        "Example:\n"
                        "*** Begin Patch\n"
                        "*** Update File: README.md\n"
                        "@@\n"
                        "-old line\n"
                        "+new line\n"
                        "*** End Patch"
                    ),
                },
                "dry_run": {"type": "boolean", "description": "Validate and preview without writing files (default false)."},
                "max_diff_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Max characters of diff text returned per changed file (default 8000).",
                },
                "max_diffs": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Max number of per-file diffs to return (default 10).",
                },
            },
            "required": ["patch"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        patch_text = args.get("patch")
        if not isinstance(patch_text, str) or not patch_text.strip():
            raise ValueError("Missing or invalid 'patch' (expected non-empty string).")
        dry_run = bool(args.get("dry_run")) if isinstance(args.get("dry_run"), bool) else False
        max_diff_chars = int(args.get("max_diff_chars") or 8000)
        max_diffs = int(args.get("max_diffs") or 10)
        if max_diff_chars < 1:
            max_diff_chars = 8000
        if max_diffs < 1:
            max_diffs = 10

        parsed = parse_patch(patch_text)

        # First pass: compute proposed changes in-memory (Codex "verified" style).
        state: dict[str, str | None] = {}
        per_file_diffs: list[dict[str, Any]] = []
        changed_files: list[str] = []

        def read_current(rel: str) -> str:
            if rel in state:
                v = state[rel]
                if v is None:
                    raise FileNotFoundError(rel)
                return v
            p = _resolve_in_project(project_root, rel)
            if not p.exists() or not p.is_file():
                raise FileNotFoundError(rel)
            txt = p.read_text(encoding="utf-8")
            state[rel] = txt
            return txt

        for hunk in parsed.hunks:
            if hunk.kind == "add":
                contents = hunk.contents or ""
                state[hunk.path] = contents
                changed_files.append(hunk.path)
                if len(per_file_diffs) < max_diffs:
                    diff_full = _render_unified_diff("", contents, path=hunk.path, context=1)
                    diff, truncated = _truncate_text(diff_full, max_chars=max_diff_chars)
                    per_file_diffs.append({"path": hunk.path, "diff": diff, "truncated": truncated})
                continue

            if hunk.kind == "delete":
                old = read_current(hunk.path)
                state[hunk.path] = None
                changed_files.append(hunk.path)
                if len(per_file_diffs) < max_diffs:
                    diff_full = _render_unified_diff(old, "", path=hunk.path, context=1)
                    diff, truncated = _truncate_text(diff_full, max_chars=max_diff_chars)
                    per_file_diffs.append({"path": hunk.path, "diff": diff, "truncated": truncated})
                continue

            if hunk.kind == "update":
                old = read_current(hunk.path)
                if not hunk.chunks:
                    raise ValueError(f"Update file hunk for path '{hunk.path}' is empty")
                new = derive_new_contents_from_chunks(old, hunk.chunks, path_for_errors=hunk.path)

                dst_path = hunk.move_to.strip() if isinstance(hunk.move_to, str) and hunk.move_to.strip() else None
                if dst_path and dst_path != hunk.path:
                    state[dst_path] = new
                    state[hunk.path] = None
                    changed_files.append(dst_path)
                    if len(per_file_diffs) < max_diffs:
                        diff_full = _render_unified_diff(old, new, path=dst_path, context=1)
                        diff, truncated = _truncate_text(diff_full, max_chars=max_diff_chars)
                        per_file_diffs.append({"path": dst_path, "diff": diff, "truncated": truncated, "moved_from": hunk.path})
                else:
                    state[hunk.path] = new
                    changed_files.append(hunk.path)
                    if len(per_file_diffs) < max_diffs:
                        diff_full = _render_unified_diff(old, new, path=hunk.path, context=1)
                        diff, truncated = _truncate_text(diff_full, max_chars=max_diff_chars)
                        per_file_diffs.append({"path": hunk.path, "diff": diff, "truncated": truncated})
                continue

        if dry_run:
            return {"ok": True, "dry_run": True, "changed_files": changed_files, "diffs": per_file_diffs}

        # Second pass: write changes to disk.
        for rel, content in state.items():
            p = _resolve_in_project(project_root, rel)
            if content is None:
                if p.exists():
                    p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

        return {"ok": True, "dry_run": False, "changed_files": changed_files, "diffs": per_file_diffs}
