from __future__ import annotations

from .registry import ToolRegistry
from .runtime import (
    InspectionDecision,
    InspectionResult,
    PlannedToolCall,
    ToolApprovalMode,
    ToolExecutionContext,
    ToolRuntime,
    ToolRuntimeError,
)
from typing import Any
import importlib

__all__ = [
    "ToolRegistry",
    "ToolRuntime",
    "ToolRuntimeError",
    "InspectionDecision",
    "InspectionResult",
    "PlannedToolCall",
    "ToolApprovalMode",
    "ToolExecutionContext",
    # Lazily imported tool implementations (see __getattr__).
    "BrowserRunTool",
    "DAGExecuteNextTool",
    "ProjectApplyEditsTool",
    "ProjectApplyPatchTool",
    "ProjectGlobTool",
    "ProjectListDirTool",
    "ProjectPatchTool",
    "ProjectReadTextManyTool",
    "ProjectReadTextTool",
    "ProjectSearchTextTool",
    "ProjectTextStatsTool",
    "SessionExportTool",
    "SessionSearchTool",
    "ShellRunTool",
    "SkillListTool",
    "SkillLoadTool",
    "SkillReadFileTool",
    "SnapshotCreateTool",
    "SnapshotDiffTool",
    "SnapshotListTool",
    "SnapshotReadTextTool",
    "SnapshotRollbackTool",
    "SpecApplyTool",
    "SpecGetTool",
    "SpecProposeTool",
    "SpecQueryTool",
    "SpecSealTool",
    "UpdatePlanTool",
    "UpdateTodoTool",
]


_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ProjectReadTextTool": (".builtins", "ProjectReadTextTool"),
    "ProjectSearchTextTool": (".builtins", "ProjectSearchTextTool"),
    "ShellRunTool": (".builtins", "ShellRunTool"),
    "BrowserRunTool": (".browser", "BrowserRunTool"),
    "DAGExecuteNextTool": (".dag_execute", "DAGExecuteNextTool"),
    "ProjectGlobTool": (".discovery", "ProjectGlobTool"),
    "ProjectListDirTool": (".discovery", "ProjectListDirTool"),
    "ProjectReadTextManyTool": (".discovery", "ProjectReadTextManyTool"),
    "SkillListTool": (".skills", "SkillListTool"),
    "SkillLoadTool": (".skills", "SkillLoadTool"),
    "SkillReadFileTool": (".skills", "SkillReadFileTool"),
    "UpdatePlanTool": (".plan", "UpdatePlanTool"),
    "UpdateTodoTool": (".todo", "UpdateTodoTool"),
    "SpecApplyTool": (".spec_workflow", "SpecApplyTool"),
    "SpecGetTool": (".spec_workflow", "SpecGetTool"),
    "SpecProposeTool": (".spec_workflow", "SpecProposeTool"),
    "SpecQueryTool": (".spec_workflow", "SpecQueryTool"),
    "SpecSealTool": (".spec_workflow", "SpecSealTool"),
    "SnapshotCreateTool": (".snapshot_tools", "SnapshotCreateTool"),
    "SnapshotDiffTool": (".snapshot_tools", "SnapshotDiffTool"),
    "SnapshotListTool": (".snapshot_tools", "SnapshotListTool"),
    "SnapshotReadTextTool": (".snapshot_tools", "SnapshotReadTextTool"),
    "SnapshotRollbackTool": (".snapshot_tools", "SnapshotRollbackTool"),
    "SessionExportTool": (".session_tools", "SessionExportTool"),
    "SessionSearchTool": (".session_tools", "SessionSearchTool"),
    "ProjectTextStatsTool": (".text_stats", "ProjectTextStatsTool"),
    "ProjectApplyPatchTool": (".apply_patch_tool", "ProjectApplyPatchTool"),
    "ProjectApplyEditsTool": (".apply_edits_tool", "ProjectApplyEditsTool"),
    "ProjectPatchTool": (".patch_tool", "ProjectPatchTool"),
}


def __getattr__(name: str) -> Any:
    """
    Keep `aura.runtime.tools` import fast by deferring tool implementation imports.

    This is important because `aura.runtime.tools.runtime` is imported very early
    (e.g. during CLI startup), and Python loads the package `__init__` first.
    """

    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attr = target
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals().keys()) | set(_LAZY_EXPORTS.keys()))
