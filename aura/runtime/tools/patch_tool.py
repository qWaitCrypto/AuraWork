from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .apply_patch_tool import ProjectApplyPatchTool


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if len(lines) < 2:
        return text
    if not lines[-1].strip().startswith("```"):
        return text
    inner = "\n".join(lines[1:-1]).strip()
    return inner


def _clean_unified_diff_path(raw: str) -> str:
    s = raw.strip()
    if not s:
        return s
    # Typical formats:
    # - a/path\tTIMESTAMP
    # - a/path
    # - /dev/null
    s = s.split("\t", 1)[0].strip()
    if s.startswith(("a/", "b/")) and len(s) > 2:
        s = s[2:]
    return s


def _parse_unified_diff_files(diff_text: str) -> list[dict[str, Any]]:
    """
    Parse a unified diff into file blocks.

    Returns a list of dicts:
    - kind: "add" | "delete" | "update"
    - path: str
    - hunks: list[list[str]]  (for update/add)
    """

    lines = diff_text.splitlines()
    out: list[dict[str, Any]] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if not line.startswith("--- "):
            i += 1
            continue

        if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
            i += 1
            continue

        old_raw = lines[i][4:].strip()
        new_raw = lines[i + 1][4:].strip()
        old_path = _clean_unified_diff_path(old_raw)
        new_path = _clean_unified_diff_path(new_raw)
        i += 2

        hunks: list[list[str]] = []
        current: list[str] | None = None
        while i < len(lines):
            l = lines[i]
            if l.startswith("--- "):
                break
            if l.startswith("@@"):
                if current:
                    if len(current) > 1:
                        hunks.append(current)
                current = ["@@"]
                i += 1
                continue
            if l.startswith("\\ No newline at end of file"):
                i += 1
                continue
            if current is not None and l.startswith((" ", "+", "-")):
                # Unified diff uses ' ' / '+' / '-' prefixes.
                current.append(l)
                i += 1
                continue
            i += 1

        if current and len(current) > 1:
            hunks.append(current)

        if old_path == "/dev/null" and new_path and new_path != "/dev/null":
            out.append({"kind": "add", "path": new_path, "hunks": hunks})
        elif new_path == "/dev/null" and old_path and old_path != "/dev/null":
            out.append({"kind": "delete", "path": old_path, "hunks": []})
        else:
            path = new_path or old_path
            if path:
                out.append({"kind": "update", "path": path, "hunks": hunks})

    return out


def unified_diff_target_paths(diff_text: str) -> list[str]:
    """
    Best-effort extraction of target paths from a unified diff.
    """

    targets: list[str] = []
    for item in _parse_unified_diff_files(diff_text):
        path = item.get("path")
        if isinstance(path, str) and path.strip():
            targets.append(path.strip())
    return targets


def _unified_diff_to_apply_patch(diff_text: str) -> str:
    blocks = _parse_unified_diff_files(diff_text)
    if not blocks:
        raise ValueError("No unified diff file blocks found (expected '---'/'+++' headers).")

    out_lines: list[str] = ["*** Begin Patch"]
    for b in blocks:
        kind = b.get("kind")
        path = b.get("path")
        hunks = b.get("hunks")
        if not isinstance(kind, str) or kind not in {"add", "delete", "update"}:
            continue
        if not isinstance(path, str) or not path.strip():
            continue
        path = path.strip()

        if kind == "delete":
            out_lines.append(f"*** Delete File: {path}")
            continue

        if not isinstance(hunks, list):
            raise ValueError(f"Invalid hunks for {path} (expected list).")

        if kind == "add":
            out_lines.append(f"*** Add File: {path}")
            # Reconstruct new file content from '+' (and ' ' context) lines.
            for hunk in hunks:
                if not isinstance(hunk, list):
                    continue
                for l in hunk[1:]:
                    if not isinstance(l, str) or not l:
                        continue
                    if l.startswith("+"):
                        out_lines.append("+" + l[1:])
                    elif l.startswith(" "):
                        out_lines.append("+" + l[1:])
            continue

        # update
        out_lines.append(f"*** Update File: {path}")
        if not hunks:
            raise ValueError(f"No hunks found for {path} (expected '@@' sections).")
        for hunk in hunks:
            if not isinstance(hunk, list) or not hunk:
                continue
            out_lines.append("@@")
            saw_change = False
            for l in hunk[1:]:
                if not isinstance(l, str) or not l:
                    continue
                if l.startswith((" ", "+", "-")):
                    out_lines.append(l)
                    if l[0] in {"+", "-"}:
                        saw_change = True
                elif l == "":
                    out_lines.append(" ")
                else:
                    continue
            if not saw_change:
                raise ValueError(f"Empty hunk found for {path} (no +/- lines).")

    out_lines.append("*** End Patch")
    return "\n".join(out_lines)


@dataclass(frozen=True, slots=True)
class ProjectPatchTool:
    """
    Apply patches using unified diff input.

    This is a compatibility wrapper around `project__apply_patch` that accepts
    standard unified diff (`---`/`+++`/`@@`) and converts it to Codex apply_patch.
    """

    name: str = "project__patch"
    description: str = (
        "Apply a patch to UTF-8 text files under the project root.\n\n"
        "Input format: unified diff (starts with '---'/'+++', with '@@' hunks) as produced by `git diff`.\n"
        "Do NOT include markdown ``` fences; pass raw diff text.\n\n"
        "Notes:\n"
        "- This tool converts unified diff into Aura's internal apply_patch format.\n"
        "- Prefer this tool over `project__apply_patch` (which expects a special DSL).\n"
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "diff": {
                    "type": "string",
                    "description": (
                        "Unified diff text. Must include '---' and '+++' headers per file.\n\n"
                        "Example:\n"
                        "--- a/README.md\n"
                        "+++ b/README.md\n"
                        "@@\\n"
                        "-old line\n"
                        "+new line"
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
            "required": ["diff"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        diff_text = args.get("diff")
        if not isinstance(diff_text, str) or not diff_text.strip():
            raise ValueError("Missing or invalid 'diff' (expected non-empty string).")

        diff_text = _strip_code_fences(diff_text)
        cleaned = diff_text.strip()

        # Also accept Codex apply_patch input as an escape hatch (kept internal).
        if cleaned.startswith("*** Begin Patch"):
            patch_text = cleaned
        else:
            patch_text = _unified_diff_to_apply_patch(cleaned)

        delegate = ProjectApplyPatchTool()
        delegate_args: dict[str, Any] = {"patch": patch_text}
        for k in ("dry_run", "max_diff_chars", "max_diffs"):
            if k in args:
                delegate_args[k] = args[k]
        return delegate.execute(args=delegate_args, project_root=project_root)
