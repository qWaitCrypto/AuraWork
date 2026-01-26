from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import strictyaml


class FrontmatterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedFrontmatter:
    data: dict[str, Any]
    body: str


def parse_markdown_frontmatter(text: str) -> ParsedFrontmatter:
    """
    Parse a Markdown file that starts with YAML frontmatter delimited by `---`.

    YAML parsing is delegated to `strictyaml` to support:
    - multi-line scalars (`|` / `|-`)
    - nested mappings (e.g. `metadata:` blocks)
    - lists
    """

    # Normalize newlines but keep original body formatting.
    lines = text.splitlines(keepends=True)
    if not lines:
        raise FrontmatterError("Missing YAML frontmatter (file is empty).")

    first = lines[0].strip()
    if first != "---":
        raise FrontmatterError("Missing YAML frontmatter delimiter '---' at file start.")

    fm_lines: list[str] = []
    found_end = False
    idx = 1
    while idx < len(lines):
        line = lines[idx]
        if line.strip() == "---":
            found_end = True
            idx += 1
            break
        fm_lines.append(line)
        idx += 1

    if not found_end:
        raise FrontmatterError("Missing closing YAML frontmatter delimiter '---'.")

    fm_text = "".join(fm_lines)
    try:
        parsed = strictyaml.load(fm_text)
        data = parsed.data
    except strictyaml.YAMLError as e:
        raise FrontmatterError(f"Invalid YAML in frontmatter: {e}") from e
    if not isinstance(data, dict):
        raise FrontmatterError("YAML frontmatter must be a mapping.")
    body = "".join(lines[idx:])
    return ParsedFrontmatter(data=data, body=body)
