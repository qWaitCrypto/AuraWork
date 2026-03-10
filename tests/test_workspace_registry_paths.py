from __future__ import annotations

from pathlib import Path

from web.backend.aura_web.workspace_registry import WorkspaceRegistry


def test_resolve_project_root_converts_windows_drive_on_wsl(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("web.backend.aura_web.workspace_registry._is_wsl", lambda: True)
    registry = WorkspaceRegistry(state_dir=tmp_path / "state")

    resolved = registry.resolve_project_root(r"D:\Work\Proj")
    assert resolved == Path("/mnt/d/Work/Proj").resolve()


def test_resolve_project_root_resolves_relative_path_from_cwd(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    registry = WorkspaceRegistry(state_dir=tmp_path / "state")

    resolved = registry.resolve_project_root("workspace-a")
    assert resolved == (tmp_path / "workspace-a").resolve()
