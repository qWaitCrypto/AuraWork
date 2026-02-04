from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .runtime import WebRuntime
from .workspace_init import init_workspace
from .workspace_registry import WorkspaceRegistry


@dataclass(slots=True)
class RuntimeManager:
    registry: WorkspaceRegistry
    _cache: dict[str, WebRuntime]

    def __init__(self, *, registry: WorkspaceRegistry) -> None:
        self.registry = registry
        self._cache = {}

    def runtime_for_workspace(self, *, workspace_id: str) -> WebRuntime:
        wid = str(workspace_id or "").strip()
        if not wid:
            raise KeyError("workspace_id is required")
        rt = self._cache.get(wid)
        if rt is not None:
            return rt

        project_root: Path = self.registry.get_project_root(wid)
        # Backward-compat & robustness: ensure the workspace has `.aura/config/models.json`.
        init_workspace(project_root)

        rt = WebRuntime(project_root=project_root)
        self._cache[wid] = rt
        return rt
