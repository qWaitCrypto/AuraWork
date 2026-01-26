from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


BEGIN_PATCH_MARKER = "*** Begin Patch"
END_PATCH_MARKER = "*** End Patch"
ADD_FILE_MARKER = "*** Add File: "
DELETE_FILE_MARKER = "*** Delete File: "
UPDATE_FILE_MARKER = "*** Update File: "
MOVE_TO_MARKER = "*** Move to: "
EOF_MARKER = "*** End of File"
CHANGE_CONTEXT_MARKER = "@@ "
EMPTY_CHANGE_CONTEXT_MARKER = "@@"


PatchOpKind = Literal["add", "delete", "update"]


class PatchParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UpdateFileChunk:
    change_context: str | None
    old_lines: list[str]
    new_lines: list[str]
    is_end_of_file: bool


@dataclass(frozen=True, slots=True)
class PatchOp:
    kind: PatchOpKind
    path: str
    move_to: str | None = None
    contents: str | None = None
    chunks: list[UpdateFileChunk] | None = None


@dataclass(frozen=True, slots=True)
class ApplyPatchArgs:
    patch: str
    hunks: list[PatchOp]


def list_patch_target_paths(patch_text: str) -> list[str]:
    args = parse_patch(patch_text)
    out: list[str] = []
    for op in args.hunks:
        out.append(op.path)
        if op.kind == "update" and op.move_to:
            out.append(op.move_to)
    return out


def _check_start_and_end_lines_strict(first_line: str | None, last_line: str | None) -> None:
    if first_line == BEGIN_PATCH_MARKER and last_line == END_PATCH_MARKER:
        return
    if first_line != BEGIN_PATCH_MARKER:
        got = "" if first_line is None else repr(first_line)
        raise PatchParseError(f"The first line of the patch must be '*** Begin Patch' (got {got})")
    got = "" if last_line is None else repr(last_line)
    raise PatchParseError(f"The last line of the patch must be '*** End Patch' (got {got})")


def _check_patch_boundaries_lenient(original_lines: list[str], original_error: PatchParseError) -> list[str]:
    if len(original_lines) < 4:
        raise original_error
    first, last = original_lines[0], original_lines[-1]
    if first not in {"<<EOF", "<<'EOF'", "<<\"EOF\""} or not last.endswith("EOF"):
        raise original_error
    inner = original_lines[1:-1]
    if not inner:
        raise original_error
    _check_start_and_end_lines_strict(inner[0], inner[-1])
    return inner


def parse_patch(patch_text: str) -> ApplyPatchArgs:
    if not isinstance(patch_text, str) or not patch_text.strip():
        raise PatchParseError("Missing patch text.")

    trimmed = patch_text.strip()
    lines = trimmed.splitlines()
    if not lines:
        raise PatchParseError("Missing patch text.")

    # Provide a targeted error for a common misformat: unified diff (diff -u) instead of apply_patch.
    if lines[0].startswith("--- ") and len(lines) >= 2 and lines[1].startswith("+++ "):
        raise PatchParseError(
            "This looks like a unified diff (starts with '---'/'+++'). "
            "project__apply_patch expects Codex apply_patch format.\n\n"
            "Use this structure:\n"
            "*** Begin Patch\n"
            "*** Update File: <relative/path>\n"
            "@@\n"
            "-<old line>\n"
            "+<new line>\n"
            "*** End Patch"
        )

    # Another common misformat: patch wrapped in markdown code fences.
    if lines[0].startswith("```"):
        raise PatchParseError(
            "Patch text must not be wrapped in ``` fences; pass the raw apply_patch body.\n\n"
            "Expected first line: *** Begin Patch"
        )

    try:
        try:
            _check_start_and_end_lines_strict(lines[0], lines[-1])
            effective_lines = lines
        except PatchParseError as e:
            effective_lines = _check_patch_boundaries_lenient(lines, e)
    except PatchParseError as e:
        preview_n = min(5, len(lines))
        preview = "\n".join(lines[:preview_n])
        raise PatchParseError(f"{e}\nPatch preview (first {preview_n} line(s)):\n{preview}") from None

    last_line_index = max(len(effective_lines) - 1, 0)
    remaining = effective_lines[1:last_line_index]
    hunks: list[PatchOp] = []
    line_number = 2
    i = 0
    while i < len(remaining):
        hunk, consumed = _parse_one_hunk(remaining[i:], line_number)
        hunks.append(hunk)
        i += consumed
        line_number += consumed

    normalized_patch = "\n".join(effective_lines)
    return ApplyPatchArgs(patch=normalized_patch, hunks=hunks)


def _parse_one_hunk(lines: list[str], line_number: int) -> tuple[PatchOp, int]:
    if not lines:
        raise PatchParseError("invalid hunk: empty")
    first_line = lines[0].strip()

    # Common misformat: unified diff headers inside an apply_patch envelope.
    if first_line.startswith("--- ") and len(lines) >= 2 and lines[1].strip().startswith("+++ "):
        raise PatchParseError(
            "This looks like a unified diff hunk header ('---'/'+++'). "
            "project__apply_patch expects Codex apply_patch format.\n\n"
            "Use this structure:\n"
            "*** Begin Patch\n"
            "*** Update File: <relative/path>\n"
            "@@\n"
            "-<old line>\n"
            "+<new line>\n"
            "*** End Patch"
        )

    if first_line.startswith(ADD_FILE_MARKER):
        path = first_line[len(ADD_FILE_MARKER) :]
        contents = ""
        parsed = 1
        for add_line in lines[1:]:
            if add_line.startswith("+"):
                contents += add_line[1:] + "\n"
                parsed += 1
                continue
            break
        return PatchOp(kind="add", path=path, contents=contents), parsed

    if first_line.startswith(DELETE_FILE_MARKER):
        path = first_line[len(DELETE_FILE_MARKER) :]
        return PatchOp(kind="delete", path=path), 1

    if first_line.startswith(UPDATE_FILE_MARKER):
        path = first_line[len(UPDATE_FILE_MARKER) :]
        remaining = lines[1:]
        parsed = 1

        move_to: str | None = None
        if remaining and remaining[0].startswith(MOVE_TO_MARKER):
            move_to = remaining[0][len(MOVE_TO_MARKER) :]
            remaining = remaining[1:]
            parsed += 1

        chunks: list[UpdateFileChunk] = []
        while remaining:
            if remaining[0].strip() == "":
                remaining = remaining[1:]
                parsed += 1
                continue
            if remaining[0].startswith("***"):
                break
            chunk, chunk_lines = _parse_update_file_chunk(
                remaining, line_number + parsed, allow_missing_context=not chunks
            )
            chunks.append(chunk)
            remaining = remaining[chunk_lines:]
            parsed += chunk_lines

        if not chunks:
            raise PatchParseError(f"invalid hunk at line {line_number}, Update file hunk for path '{path}' is empty")

        return PatchOp(kind="update", path=path, move_to=move_to, chunks=chunks), parsed

    raise PatchParseError(
        f"invalid hunk at line {line_number}, '{first_line}' is not a valid hunk header. "
        "Valid hunk headers: '*** Add File: {path}', '*** Delete File: {path}', '*** Update File: {path}'"
    )


def _parse_update_file_chunk(
    lines: list[str], line_number: int, *, allow_missing_context: bool
) -> tuple[UpdateFileChunk, int]:
    if not lines:
        raise PatchParseError(f"invalid hunk at line {line_number}, Update hunk does not contain any lines")

    if lines[0] == EMPTY_CHANGE_CONTEXT_MARKER:
        change_context: str | None = None
        start_index = 1
    elif lines[0].startswith(CHANGE_CONTEXT_MARKER):
        # Many models mistakenly emit unified diff range headers (e.g. "@@ -10,6 +11,7 @@").
        # Treat these as "no explicit context" and rely on the +/- old_lines matcher instead.
        change_context_raw = lines[0][len(CHANGE_CONTEXT_MARKER) :]
        if (
            isinstance(change_context_raw, str)
            and change_context_raw.lstrip().startswith("-")
            and " +" in change_context_raw
            and change_context_raw.strip().endswith("@@")
        ):
            change_context = None
        else:
            change_context = change_context_raw
        start_index = 1
    else:
        if not allow_missing_context:
            raise PatchParseError(
                f"invalid hunk at line {line_number}, Expected update hunk to start with a @@ context marker, got: '{lines[0]}'"
            )
        change_context = None
        start_index = 0

    if start_index >= len(lines):
        raise PatchParseError(f"invalid hunk at line {line_number + 1}, Update hunk does not contain any lines")

    old_lines: list[str] = []
    new_lines: list[str] = []
    is_end_of_file = False
    parsed_lines = 0

    for raw in lines[start_index:]:
        if raw == EOF_MARKER:
            if parsed_lines == 0:
                raise PatchParseError(f"invalid hunk at line {line_number + 1}, Update hunk does not contain any lines")
            is_end_of_file = True
            parsed_lines += 1
            break

        if raw == "":
            old_lines.append("")
            new_lines.append("")
            parsed_lines += 1
            continue

        prefix = raw[0]
        rest = raw[1:]
        if prefix == " ":
            old_lines.append(rest)
            new_lines.append(rest)
            parsed_lines += 1
            continue
        if prefix == "+":
            new_lines.append(rest)
            parsed_lines += 1
            continue
        if prefix == "-":
            old_lines.append(rest)
            parsed_lines += 1
            continue

        if parsed_lines == 0:
            raise PatchParseError(
                f"invalid hunk at line {line_number + 1}, Unexpected line found in update hunk: '{raw}'. "
                "Every line should start with ' ' (context line), '+' (added line), or '-' (removed line)"
            )
        break

    return (
        UpdateFileChunk(
            change_context=change_context,
            old_lines=old_lines,
            new_lines=new_lines,
            is_end_of_file=is_end_of_file,
        ),
        parsed_lines + start_index,
    )


def _seek_sequence(lines: list[str], pattern: list[str], *, start: int, eof: bool) -> int | None:
    if not pattern:
        return min(max(start, 0), len(lines))
    if len(pattern) > len(lines):
        return None

    max_start = len(lines) - len(pattern)
    search_start = max_start if eof and len(lines) >= len(pattern) else start
    if search_start < 0:
        search_start = 0

    for i in range(search_start, max_start + 1):
        if lines[i : i + len(pattern)] == pattern:
            return i

    for i in range(search_start, max_start + 1):
        ok = True
        for p_idx, pat in enumerate(pattern):
            if lines[i + p_idx].rstrip() != pat.rstrip():
                ok = False
                break
        if ok:
            return i

    for i in range(search_start, max_start + 1):
        ok = True
        for p_idx, pat in enumerate(pattern):
            if lines[i + p_idx].strip() != pat.strip():
                ok = False
                break
        if ok:
            return i

    def normalise(s: str) -> str:
        out_chars: list[str] = []
        for c in s.strip():
            if c in {"\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015", "\u2212"}:
                out_chars.append("-")
            elif c in {"\u2018", "\u2019", "\u201A", "\u201B"}:
                out_chars.append("'")
            elif c in {"\u201C", "\u201D", "\u201E", "\u201F"}:
                out_chars.append('"')
            elif c in {
                "\u00A0",
                "\u2002",
                "\u2003",
                "\u2004",
                "\u2005",
                "\u2006",
                "\u2007",
                "\u2008",
                "\u2009",
                "\u200A",
                "\u202F",
                "\u205F",
                "\u3000",
            }:
                out_chars.append(" ")
            else:
                out_chars.append(c)
        return "".join(out_chars)

    for i in range(search_start, max_start + 1):
        ok = True
        for p_idx, pat in enumerate(pattern):
            if normalise(lines[i + p_idx]) != normalise(pat):
                ok = False
                break
        if ok:
            return i

    return None


def derive_new_contents_from_chunks(original_contents: str, chunks: list[UpdateFileChunk], *, path_for_errors: str) -> str:
    original_lines = [s for s in original_contents.split("\n")]
    if original_lines and original_lines[-1] == "":
        original_lines.pop()

    replacements: list[tuple[int, int, list[str]]] = []
    line_index = 0

    for chunk in chunks:
        if chunk.change_context is not None:
            idx = _seek_sequence(original_lines, [chunk.change_context], start=line_index, eof=False)
            if idx is None:
                raise ValueError(f"Failed to find context '{chunk.change_context}' in {path_for_errors}")
            line_index = idx + 1

        if not chunk.old_lines:
            insertion_idx = len(original_lines) - 1 if original_lines and original_lines[-1] == "" else len(original_lines)
            replacements.append((insertion_idx, 0, list(chunk.new_lines)))
            continue

        pattern = list(chunk.old_lines)
        found = _seek_sequence(original_lines, pattern, start=line_index, eof=chunk.is_end_of_file)
        new_slice = list(chunk.new_lines)

        if found is None and pattern and pattern[-1] == "":
            pattern = pattern[:-1]
            if new_slice and new_slice[-1] == "":
                new_slice = new_slice[:-1]
            found = _seek_sequence(original_lines, pattern, start=line_index, eof=chunk.is_end_of_file)

        if found is None:
            raise ValueError(f"Failed to find expected lines in {path_for_errors}:\n" + "\n".join(chunk.old_lines))

        replacements.append((found, len(pattern), new_slice))
        line_index = found + len(pattern)

    replacements.sort(key=lambda r: r[0])

    lines = list(original_lines)
    for start_idx, old_len, new_segment in reversed(replacements):
        for _ in range(old_len):
            if start_idx < len(lines):
                lines.pop(start_idx)
        for offset, new_line in enumerate(new_segment):
            lines.insert(start_idx + offset, new_line)

    if not (lines and lines[-1] == ""):
        lines.append("")
    return "\n".join(lines)
