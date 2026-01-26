from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .builtins import _maybe_int, _require_str, _resolve_in_project


def _is_han(ch: str) -> bool:
    cp = ord(ch)
    # Common Han ranges (best-effort, not exhaustive):
    # - CJK Unified Ideographs: 4E00–9FFF
    # - CJK Unified Ideographs Extension A: 3400–4DBF
    # - CJK Compatibility Ideographs: F900–FAFF
    return (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF) or (0xF900 <= cp <= 0xFAFF)


def _is_ascii_letter(ch: str) -> bool:
    o = ord(ch)
    return (0x41 <= o <= 0x5A) or (0x61 <= o <= 0x7A)


@dataclass(frozen=True, slots=True)
class ProjectTextStatsTool:
    name: str = "project__text_stats"
    description: str = "Compute writing statistics for a UTF-8 text file under the project root (read-only)."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within the project root."},
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional maximum characters to analyze (default: analyze full file).",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        rel = _require_str(args, "path")
        max_chars = _maybe_int(args, "max_chars")
        file_path = _resolve_in_project(project_root, rel)
        data = file_path.read_bytes()
        text = data.decode("utf-8", errors="replace")

        truncated = False
        if isinstance(max_chars, int) and max_chars > 0 and len(text) > max_chars:
            truncated = True
            text = text[:max_chars]

        char_count = len(text)
        han_count = 0
        punctuation_count = 0
        letter_count = 0

        # English word count: sequences of ASCII letters, allowing internal apostrophes.
        word_count = 0
        in_word = False
        saw_letter = False
        prev_was_apostrophe = False

        for ch in text:
            if _is_han(ch):
                han_count += 1
            cat = unicodedata.category(ch)
            if cat.startswith("P"):
                punctuation_count += 1
            if _is_ascii_letter(ch):
                letter_count += 1

            if _is_ascii_letter(ch):
                if not in_word:
                    in_word = True
                    saw_letter = True
                    prev_was_apostrophe = False
                else:
                    saw_letter = True
                    prev_was_apostrophe = False
                continue

            if ch == "'" and in_word and saw_letter and not prev_was_apostrophe:
                prev_was_apostrophe = True
                continue

            if in_word and saw_letter:
                word_count += 1
            in_word = False
            saw_letter = False
            prev_was_apostrophe = False

        if in_word and saw_letter:
            word_count += 1

        return {
            "ok": True,
            "path": str(Path(rel)),
            "truncated": truncated,
            "counts": {
                "chars": char_count,
                "han": han_count,
                "punctuation": punctuation_count,
                "letters": letter_count,
                "words": word_count,
            },
        }

