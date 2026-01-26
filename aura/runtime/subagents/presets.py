from __future__ import annotations

import importlib.resources
from dataclasses import dataclass, field
from typing import Any


def load_prompt_asset(name: str) -> str:
    return (
        importlib.resources.files("aura.runtime")
        .joinpath("prompts", name)
        .read_text(encoding="utf-8", errors="replace")
    )


@dataclass(frozen=True, slots=True)
class SubagentLimits:
    max_turns: int
    max_tool_calls: int


@dataclass(frozen=True, slots=True)
class SubagentPreset:
    name: str
    prompt_asset: str
    default_allowlist: list[str]
    limits: SubagentLimits
    safe_shell_prefixes: list[str] = field(default_factory=list)
    auto_approve_tools: list[str] = field(default_factory=list)

    def load_prompt(self) -> str:
        return load_prompt_asset(self.prompt_asset)


_PRESETS: dict[str, SubagentPreset] = {
    "file_ops_worker": SubagentPreset(
        name="file_ops_worker",
        prompt_asset="subagent_file_ops_worker.md",
        default_allowlist=[
            "project__list_dir",
            "project__glob",
            "project__read_text",
            "project__read_text_many",
            "project__apply_edits",
            "shell__run",
            "snapshot__create",
            "snapshot__diff",
        ],
        limits=SubagentLimits(max_turns=12, max_tool_calls=24),
    ),
    "doc_worker": SubagentPreset(
        name="doc_worker",
        prompt_asset="subagent_doc_worker.md",
        default_allowlist=[
            # Skills (docx/pdf)
            "skill__list",
            "skill__load",
            "skill__read_file",

            # Project IO
            "project__list_dir",
            "project__glob",
            "project__read_text",
            "project__read_text_many",
            "project__apply_edits",
            "project__search_text",

            # Execute skill scripts (approval-gated via ToolRuntime + approval agent)
            "shell__run",

            "snapshot__create",
            "snapshot__diff",
        ],
        limits=SubagentLimits(max_turns=12, max_tool_calls=24),
        # Auto-approve running the *skill runner* script (see subagents/runner.py safety checks).
        safe_shell_prefixes=[
            ".aura/skills/aura-docx/scripts/run.py",
            ".aura/skills/aura-pdf/scripts/run.py",
        ],
    ),
    "sheet_worker": SubagentPreset(
        name="sheet_worker",
        prompt_asset="subagent_sheet_worker.md",
        default_allowlist=[
            # Skills (xlsx)
            "skill__list",
            "skill__load",
            "skill__read_file",

            # Project IO
            "project__list_dir",
            "project__glob",
            "project__read_text",
            "project__read_text_many",
            "project__apply_edits",
            "project__search_text",

            # Execute skill scripts (approval-gated via ToolRuntime + approval agent)
            "shell__run",

            "snapshot__create",
            "snapshot__diff",
        ],
        limits=SubagentLimits(max_turns=12, max_tool_calls=24),
        # Auto-approve running the *skill runner* script (see subagents/runner.py safety checks).
        safe_shell_prefixes=[
            ".aura/skills/aura-xlsx/scripts/run.py",
        ],
    ),
    "browser_worker": SubagentPreset(
        name="browser_worker",
        prompt_asset="subagent_browser_worker.md",
        default_allowlist=[
            "skill__list",
            "skill__load",
            "skill__read_file",
            "browser__run",
        ],
        limits=SubagentLimits(max_turns=20, max_tool_calls=50),
        safe_shell_prefixes=[],
        # Testing mode: allow browser automation without approval prompts.
        auto_approve_tools=["browser__run"],
    ),
    "verifier": SubagentPreset(
        name="verifier",
        prompt_asset="subagent_verifier.md",
        default_allowlist=[
            "project__list_dir",
            "project__glob",
            "project__read_text",
            "project__read_text_many",
            "project__search_text",
            "snapshot__list",
            "spec__query",
            "spec__get",
            "snapshot__read_text",
            "snapshot__diff",
            "session__search",
        ],
        limits=SubagentLimits(max_turns=6, max_tool_calls=12),
    ),
}


def get_preset(name: str) -> SubagentPreset | None:
    return _PRESETS.get(str(name or "").strip())


def list_presets() -> list[str]:
    return sorted(_PRESETS)


def preset_input_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "enum": list_presets(),
        "description": "Subagent preset.",
    }
