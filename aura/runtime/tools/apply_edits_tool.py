from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path
from typing import Any

from .apply_patch_engine import UpdateFileChunk, derive_new_contents_from_chunks


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


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ensure_trailing_newline(text: str) -> str:
    if text.endswith("\n"):
        return text
    return text + "\n"


def _split_lines_no_trailing_empty(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _join_lines_with_trailing_newline(lines: list[str]) -> str:
    return _ensure_trailing_newline("\n".join(lines))


def list_apply_edits_target_paths(args: dict[str, Any]) -> list[str]:
    ops = args.get("ops")
    if not isinstance(ops, list):
        return []
    out: list[str] = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "add_file":
            path = op.get("path")
            if isinstance(path, str) and path.strip():
                out.append(path.strip())
        elif kind == "delete_file":
            path = op.get("path")
            if isinstance(path, str) and path.strip():
                out.append(path.strip())
        elif kind == "move_file":
            src = op.get("from")
            dst = op.get("to")
            if isinstance(src, str) and src.strip():
                out.append(src.strip())
            if isinstance(dst, str) and dst.strip():
                out.append(dst.strip())
        elif kind == "update_file":
            path = op.get("path")
            if isinstance(path, str) and path.strip():
                out.append(path.strip())
            move_to = op.get("move_to")
            if isinstance(move_to, str) and move_to.strip():
                out.append(move_to.strip())
        else:
            path = op.get("path")
            if isinstance(path, str) and path.strip():
                out.append(path.strip())
    return out


def gemini_compatible_apply_edits_input_schema() -> dict[str, Any]:
    """
    Gemini gateways often support only a limited JSON schema subset for tool parameters.

    Constructs like `oneOf`/`anyOf`/`const` can be rejected with a 400 even before the model
    responds. For Gemini tool declarations, Aura uses this simplified schema and relies on
    runtime validation for strictness.
    """

    return {
        "type": "object",
        "properties": {
            "ops": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "Ordered list of edit operations.\n\n"
                    "Supported ops (all are objects with an `op` field):\n"
                    "- add_file: {path, content, overwrite?}\n"
                    "- delete_file: {path}\n"
                    "- move_file: {from, to}\n"
                    "- update_file: {path, chunks, move_to?} where chunks[] = {change_context?, old_lines, new_lines, is_end_of_file?}\n"
                    "- insert_before|insert_after: {path, anchor_lines, new_lines}\n"
                    "- prepend_lines|append_lines: {path, new_lines} (or {path, lines} for back-compat)\n"
                    "- replace_substring_first|replace_substring_all: {path, old, new, expected_count?}\n\n"
                    "IMPORTANT: Matching is exact. Copy anchors/old text from `project__read_text`/`project__search_text`."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": [
                                "add_file",
                                "delete_file",
                                "move_file",
                                "update_file",
                                "insert_before",
                                "insert_after",
                                "prepend_lines",
                                "append_lines",
                                "replace_substring_first",
                                "replace_substring_all",
                            ],
                            "description": "Operation kind.",
                        }
                    },
                    "required": ["op"],
                    "additionalProperties": True,
                },
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
        "required": ["ops"],
        "additionalProperties": False,
    }


def _require_str_field(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing or invalid '{key}' (expected non-empty string).")
    return value.strip()


def _require_bool_field(obj: dict[str, Any], key: str) -> bool:
    value = obj.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Missing or invalid '{key}' (expected boolean).")
    return value


def _optional_str_field(obj: dict[str, Any], key: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Invalid '{key}' (expected string).")
    s = value.strip()
    return s or None


def _optional_int_field(obj: dict[str, Any], key: str) -> int | None:
    value = obj.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Invalid '{key}' (expected integer).")
    return value


def _require_str_list_field(obj: dict[str, Any], key: str) -> list[str]:
    value = obj.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"Missing or invalid '{key}' (expected non-empty list of strings).")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"Invalid '{key}' (expected list of strings).")
        out.append(item)
    return out


def _optional_str_list_field(obj: dict[str, Any], key: str) -> list[str] | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"Invalid '{key}' (expected list of strings).")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"Invalid '{key}' (expected list of strings).")
        out.append(item)
    return out


def _flatten_embedded_newlines(lines: list[str]) -> list[str]:
    """
    Some callers provide a list with a single item containing embedded newlines.

    The edit engine expects a list of *individual lines*, so we normalize this
    by splitting on '\\n'. This preserves strict matching semantics at the line
    level while avoiding surprising failures for well-intentioned callers.
    """

    out: list[str] = []
    for item in lines:
        if "\n" not in item:
            out.append(item)
            continue
        parts = item.split("\n")
        if parts and parts[-1] == "":
            parts.pop()
        out.extend(parts)
    return out


def _apply_insert_relative(
    *,
    content: str,
    anchor_lines: list[str],
    new_lines: list[str],
    before: bool,
    path_for_errors: str,
) -> str:
    if not anchor_lines:
        raise ValueError("anchor_lines must be a non-empty list.")
    lines = _split_lines_no_trailing_empty(content)
    pattern = list(anchor_lines)
    matches: list[int] = []
    for i in range(0, len(lines) - len(pattern) + 1):
        if lines[i : i + len(pattern)] == pattern:
            matches.append(i)
    if not matches:
        raise ValueError(f"Failed to find anchor_lines in {path_for_errors}:\n" + "\n".join(anchor_lines))
    if len(matches) > 1:
        raise ValueError(
            f"anchor_lines is ambiguous in {path_for_errors} (matched {len(matches)} times). "
            "Provide a longer/more specific anchor."
        )
    start = matches[0]
    insert_at = start if before else start + len(pattern)
    for offset, line in enumerate(list(new_lines)):
        lines.insert(insert_at + offset, line)
    return _join_lines_with_trailing_newline(lines)


@dataclass(frozen=True, slots=True)
class ProjectApplyEditsTool:
    """
    Apply structured edit operations (JSON) to UTF-8 text files under the project root.

    This is a schema-constrained alternative to `project__apply_patch`. It reuses the same
    deterministic line/block matching semantics where applicable, and returns unified diffs
    for auditability and UI rendering.
    """

    name: str = "project__apply_edits"
    description: str = (
        "Apply structured edit operations (JSON) to UTF-8 text files under the project root. "
        "This tool is strict and safe: edits only apply when anchors and old text match EXACTLY. "
        "Before calling, use `project__read_text`/`project__search_text` to copy exact `anchor_lines`/`old_lines`/substrings; never guess or paraphrase. "
        "Prefer `insert_before`/`insert_after`/`prepend_lines`/`append_lines` for simple line insertions. "
        "Use `update_file` only when you can provide an explicit non-empty `chunks` list. "
        "For deletion via `update_file`, set `chunks[].new_lines` to []. "
        "Returns per-file unified diffs for audit/UI. "
        "Operations are transactional: all ops are validated/applied in-memory first; writes happen only if all succeed."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "ops": {
                    "type": "array",
                    "minItems": 1,
                    "description": (
                        "Ordered list of edit operations.\n\n"
                        "Supported ops (all are objects with an `op` field):\n"
                        "- add_file: {path, content, overwrite?}\n"
                        "- delete_file: {path}\n"
                        "- move_file: {from, to}\n"
                        "- update_file: {path, chunks, move_to?} where chunks[] = {change_context?, old_lines, new_lines, is_end_of_file?} (chunks MUST be non-empty)\n"
                        "- insert_before|insert_after: {path, anchor_lines, new_lines}\n"
                        "- prepend_lines|append_lines: {path, new_lines} (or {path, lines} for back-compat)\n"
                        "- replace_substring_first|replace_substring_all: {path, old, new, expected_count?}\n\n"
                        "IMPORTANT: Matching is exact. For ops that reference existing content "
                        "(`update_file`, `insert_*`, `replace_substring_*`), you MUST copy the "
                        "exact lines/substrings from the current file via `project__read_text`/`project__search_text` "
                        "(including punctuation/whitespace). Do not infer or rewrite them."
                    ),
                    "items": {
                        "oneOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "op": {"const": "add_file"},
                                    "path": {"type": "string"},
                                    "content": {"type": "string"},
                                    "overwrite": {"type": "boolean"},
                                },
                                "required": ["op", "path", "content"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {"op": {"const": "delete_file"}, "path": {"type": "string"}},
                                "required": ["op", "path"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "op": {"const": "move_file"},
                                    "from": {"type": "string"},
                                    "to": {"type": "string"},
                                    "expected_sha256": {"type": "string"},
                                },
                                "required": ["op", "from", "to"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "op": {"const": "update_file"},
                                    "path": {"type": "string"},
                                    "move_to": {"type": "string"},
                                    "expected_sha256": {"type": "string"},
                                    "chunks": {
                                        "type": "array",
                                        "minItems": 1,
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "change_context": {"type": ["string", "null"]},
                                                "old_lines": {"type": "array", "items": {"type": "string"}},
                                                "new_lines": {"type": "array", "items": {"type": "string"}},
                                                "is_end_of_file": {"type": "boolean"},
                                            },
                                            "required": ["old_lines", "new_lines"],
                                            "additionalProperties": False,
                                        },
                                    },
                                },
                                "required": ["op", "path", "chunks"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "op": {"const": "insert_before"},
                                    "path": {"type": "string"},
                                    "anchor_lines": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                                    "new_lines": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                                    "expected_sha256": {"type": "string"},
                                },
                                "required": ["op", "path", "anchor_lines", "new_lines"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "op": {"const": "insert_after"},
                                    "path": {"type": "string"},
                                    "anchor_lines": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                                    "new_lines": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                                    "expected_sha256": {"type": "string"},
                                },
                                "required": ["op", "path", "anchor_lines", "new_lines"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "op": {"const": "prepend_lines"},
                                    "path": {"type": "string"},
                                    "new_lines": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                                    "lines": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                                    "expected_sha256": {"type": "string"},
                                },
                                "required": ["op", "path"],
                                "anyOf": [{"required": ["new_lines"]}, {"required": ["lines"]}],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "op": {"const": "append_lines"},
                                    "path": {"type": "string"},
                                    "new_lines": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                                    "lines": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                                    "expected_sha256": {"type": "string"},
                                },
                                "required": ["op", "path"],
                                "anyOf": [{"required": ["new_lines"]}, {"required": ["lines"]}],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "op": {"const": "replace_substring_first"},
                                    "path": {"type": "string"},
                                    "old": {"type": "string"},
                                    "new": {"type": "string"},
                                    "expected_sha256": {"type": "string"},
                                    "expected_count": {"type": "integer", "minimum": 0},
                                },
                                "required": ["op", "path", "old", "new"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "op": {"const": "replace_substring_all"},
                                    "path": {"type": "string"},
                                    "old": {"type": "string"},
                                    "new": {"type": "string"},
                                    "expected_sha256": {"type": "string"},
                                    "expected_count": {"type": "integer", "minimum": 0},
                                },
                                "required": ["op", "path", "old", "new"],
                                "additionalProperties": False,
                            },
                        ]
                    },
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
            "required": ["ops"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        ops = args.get("ops")
        if not isinstance(ops, list) or not ops:
            raise ValueError("Missing or invalid 'ops' (expected non-empty list).")

        dry_run = bool(args.get("dry_run")) if isinstance(args.get("dry_run"), bool) else False
        max_diff_chars = int(args.get("max_diff_chars") or 8000)
        max_diffs = int(args.get("max_diffs") or 10)
        if max_diff_chars < 1:
            max_diff_chars = 8000
        if max_diffs < 1:
            max_diffs = 10

        # Current in-memory contents: path -> text (str) or None (deleted).
        state: dict[str, str | None] = {}
        # Baseline contents for diffs: path -> text (str). For moved files, we copy baseline to the destination path.
        baseline: dict[str, str] = {}
        # Record move origin for UI.
        moved_from: dict[str, str] = {}
        # Paths whose *final* state should be included in changed_files/diffs (preserve op order, unique).
        changed_paths: list[str] = []
        changed_set: set[str] = set()

        def _file_exists(rel: str) -> bool:
            if rel in state:
                return state[rel] is not None
            p = _resolve_in_project(project_root, rel)
            return p.exists() and p.is_file()

        def _read_current(rel: str) -> str:
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
            baseline.setdefault(rel, txt)
            return txt

        def _set_current(rel: str, txt: str | None) -> None:
            state[rel] = txt

        def _mark_changed(rel: str) -> None:
            if rel in changed_set:
                return
            changed_set.add(rel)
            changed_paths.append(rel)

        def _check_expected_sha256(rel: str, expected_sha256: str | None) -> None:
            if expected_sha256 is None:
                return
            expected = expected_sha256.strip().lower()
            if not expected:
                return
            cur = _read_current(rel)
            got = _sha256_hex(cur)
            if got != expected:
                raise ValueError(f"expected_sha256 mismatch for {rel}: expected {expected}, got {got}")

        for raw_op in ops:
            if not isinstance(raw_op, dict):
                raise ValueError("Each item in 'ops' must be an object.")
            op_kind = raw_op.get("op")
            if not isinstance(op_kind, str) or not op_kind.strip():
                raise ValueError("Each op must include a non-empty string field 'op'.")
            op_kind = op_kind.strip()

            if op_kind == "add_file":
                path = _require_str_field(raw_op, "path")
                content = raw_op.get("content")
                if not isinstance(content, str):
                    raise ValueError("Missing or invalid 'content' (expected string).")
                expected_sha256 = _optional_str_field(raw_op, "expected_sha256")
                overwrite = bool(raw_op.get("overwrite")) if isinstance(raw_op.get("overwrite"), bool) else False

                _resolve_in_project(project_root, path)
                if _file_exists(path):
                    if not overwrite:
                        raise ValueError(f"File already exists: {path} (set overwrite=true to replace)")
                    _check_expected_sha256(path, expected_sha256)
                    baseline.setdefault(path, _read_current(path))
                else:
                    if expected_sha256 is not None:
                        raise ValueError(f"expected_sha256 provided but file does not exist: {path}")
                    baseline.setdefault(path, "")

                _set_current(path, _ensure_trailing_newline(content))
                _mark_changed(path)
                continue

            if op_kind == "delete_file":
                path = _require_str_field(raw_op, "path")
                expected_sha256 = _optional_str_field(raw_op, "expected_sha256")
                _resolve_in_project(project_root, path)
                _check_expected_sha256(path, expected_sha256)
                baseline.setdefault(path, _read_current(path))
                _set_current(path, None)
                _mark_changed(path)
                continue

            if op_kind == "move_file":
                src = _require_str_field(raw_op, "from")
                dst = _require_str_field(raw_op, "to")
                expected_sha256 = _optional_str_field(raw_op, "expected_sha256")
                _resolve_in_project(project_root, src)
                _resolve_in_project(project_root, dst)
                if _file_exists(dst):
                    raise ValueError(f"Destination already exists: {dst}")
                _check_expected_sha256(src, expected_sha256)
                src_txt = _read_current(src)
                baseline.setdefault(src, src_txt)
                baseline.setdefault(dst, baseline.get(src, src_txt))
                _set_current(dst, src_txt)
                _set_current(src, None)
                moved_from[dst] = src
                _mark_changed(dst)
                continue

            if op_kind == "update_file":
                path = _require_str_field(raw_op, "path")
                move_to = _optional_str_field(raw_op, "move_to")
                chunks_raw = raw_op.get("chunks")
                if not isinstance(chunks_raw, list) or not chunks_raw:
                    raise ValueError("Missing or invalid 'chunks' (expected non-empty list).")
                expected_sha256 = _optional_str_field(raw_op, "expected_sha256")

                _resolve_in_project(project_root, path)
                _check_expected_sha256(path, expected_sha256)
                old = _read_current(path)
                baseline.setdefault(path, old)

                chunks: list[UpdateFileChunk] = []
                for idx, c in enumerate(chunks_raw):
                    if not isinstance(c, dict):
                        raise ValueError(f"Invalid chunk at index {idx} (expected object).")
                    change_context = c.get("change_context")
                    if change_context is not None and not isinstance(change_context, str):
                        raise ValueError(f"Invalid change_context at chunk {idx} (expected string or null).")
                    old_lines = _optional_str_list_field(c, "old_lines") or []
                    new_lines = _optional_str_list_field(c, "new_lines") or []
                    old_lines = _flatten_embedded_newlines(list(old_lines))
                    new_lines = _flatten_embedded_newlines(list(new_lines))
                    is_end_of_file = c.get("is_end_of_file")
                    if is_end_of_file is None:
                        is_end_of_file = False
                    if not isinstance(is_end_of_file, bool):
                        raise ValueError(f"Invalid is_end_of_file at chunk {idx} (expected boolean).")
                    chunks.append(
                        UpdateFileChunk(
                            change_context=change_context,
                            old_lines=list(old_lines),
                            new_lines=list(new_lines),
                            is_end_of_file=is_end_of_file,
                        )
                    )

                new = derive_new_contents_from_chunks(old, chunks, path_for_errors=path)

                if move_to and move_to != path:
                    _resolve_in_project(project_root, move_to)
                    if _file_exists(move_to):
                        raise ValueError(f"Destination already exists: {move_to}")
                    baseline.setdefault(move_to, baseline.get(path, old))
                    _set_current(move_to, new)
                    _set_current(path, None)
                    moved_from[move_to] = path
                    _mark_changed(move_to)
                else:
                    _set_current(path, new)
                    _mark_changed(path)
                continue

            if op_kind in {"insert_before", "insert_after"}:
                path = _require_str_field(raw_op, "path")
                anchor_lines = _flatten_embedded_newlines(_require_str_list_field(raw_op, "anchor_lines"))
                new_lines = _flatten_embedded_newlines(_require_str_list_field(raw_op, "new_lines"))
                expected_sha256 = _optional_str_field(raw_op, "expected_sha256")

                _resolve_in_project(project_root, path)
                _check_expected_sha256(path, expected_sha256)
                old = _read_current(path)
                baseline.setdefault(path, old)
                new = _apply_insert_relative(
                    content=old,
                    anchor_lines=anchor_lines,
                    new_lines=new_lines,
                    before=(op_kind == "insert_before"),
                    path_for_errors=path,
                )
                _set_current(path, new)
                _mark_changed(path)
                continue

            if op_kind in {"prepend_lines", "append_lines"}:
                path = _require_str_field(raw_op, "path")
                # Back-compat: accept both `new_lines` (documented) and `lines` (older tool callers).
                if raw_op.get("new_lines") is not None:
                    lines_to_add = _flatten_embedded_newlines(_require_str_list_field(raw_op, "new_lines"))
                else:
                    lines_to_add = _flatten_embedded_newlines(_require_str_list_field(raw_op, "lines"))
                expected_sha256 = _optional_str_field(raw_op, "expected_sha256")

                _resolve_in_project(project_root, path)
                _check_expected_sha256(path, expected_sha256)
                old = _read_current(path)
                baseline.setdefault(path, old)
                lines = _split_lines_no_trailing_empty(old)
                if op_kind == "prepend_lines":
                    lines = list(lines_to_add) + lines
                else:
                    lines = lines + list(lines_to_add)
                _set_current(path, _join_lines_with_trailing_newline(lines))
                _mark_changed(path)
                continue

            if op_kind in {"replace_substring_first", "replace_substring_all"}:
                path = _require_str_field(raw_op, "path")
                old_s = raw_op.get("old")
                new_s = raw_op.get("new")
                if not isinstance(old_s, str) or old_s == "":
                    raise ValueError("Missing or invalid 'old' (expected non-empty string).")
                if not isinstance(new_s, str):
                    raise ValueError("Missing or invalid 'new' (expected string).")
                expected_count = _optional_int_field(raw_op, "expected_count")
                expected_sha256 = _optional_str_field(raw_op, "expected_sha256")

                _resolve_in_project(project_root, path)
                _check_expected_sha256(path, expected_sha256)
                old = _read_current(path)
                baseline.setdefault(path, old)

                count = old.count(old_s)
                if expected_count is not None and count != expected_count:
                    raise ValueError(f"expected_count mismatch for {path}: expected {expected_count}, got {count}")
                if count < 1:
                    raise ValueError(f"Substring not found in {path}.")
                if op_kind == "replace_substring_first":
                    new = old.replace(old_s, new_s, 1)
                else:
                    new = old.replace(old_s, new_s)
                _set_current(path, _ensure_trailing_newline(new))
                _mark_changed(path)
                continue

            raise ValueError(f"Unknown op: {op_kind}")

        # Compute per-file diffs from baseline to final state.
        diffs: list[dict[str, Any]] = []
        for rel in changed_paths:
            if len(diffs) >= max_diffs:
                break
            old = baseline.get(rel, "")
            new = state.get(rel, None)
            new_txt = "" if new is None else str(new)
            diff_full = _render_unified_diff(old, new_txt, path=rel, context=1)
            diff, truncated = _truncate_text(diff_full, max_chars=max_diff_chars)
            entry: dict[str, Any] = {"path": rel, "diff": diff, "truncated": truncated}
            if rel in moved_from:
                entry["moved_from"] = moved_from[rel]
            diffs.append(entry)

        if dry_run:
            return {"ok": True, "dry_run": True, "changed_files": list(changed_paths), "diffs": diffs}

        # Apply writes to disk.
        for rel, content in state.items():
            p = _resolve_in_project(project_root, rel)
            if content is None:
                if p.exists():
                    p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(content), encoding="utf-8")

        return {"ok": True, "dry_run": False, "changed_files": list(changed_paths), "diffs": diffs}
