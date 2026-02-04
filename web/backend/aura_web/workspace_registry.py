from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from aura.runtime.ids import now_ts_ms

from .workspace_init import init_workspace


def _is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except Exception:
        return False


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    workspace_id: str
    project_root: str
    last_used_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "project_root": self.project_root,
            "last_used_at": self.last_used_at,
        }


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class WorkspaceRegistry:
    """Global registry of workspace_id -> project_root for the web backend.

    Design choice (per latest confirmed requirement):
    - This is a local tool; users can pick ANY local directory as a workspace.
    - We do NOT enforce a hard-coded allowlist root.

    The registry itself is persisted under ~/.aura/web/ so it survives restarts.
    """

    def __init__(self, *, state_dir: Path | None = None) -> None:
        self.state_dir = (
            (state_dir or (Path.home() / ".aura" / "web")).expanduser().resolve()
        )
        self._path = self.state_dir / "workspaces.json"
        self._workspaces: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._workspaces = {}
            return
        except Exception:
            self._workspaces = {}
            return

        try:
            data = json.loads(raw)
        except Exception:
            self._workspaces = {}
            return

        ws = data.get("workspaces") if isinstance(data, dict) else None
        if not isinstance(ws, dict):
            self._workspaces = {}
            return

        cleaned: dict[str, dict[str, Any]] = {}
        for wid, rec in ws.items():
            if not isinstance(wid, str) or not wid.strip():
                continue
            if not isinstance(rec, dict):
                continue
            pr = rec.get("project_root")
            if not isinstance(pr, str) or not pr.strip():
                continue
            last_used = rec.get("last_used_at")
            cleaned[wid.strip()] = {
                "project_root": pr,
                "last_used_at": last_used if isinstance(last_used, int) else None,
            }
        self._workspaces = cleaned

    def _save(self) -> None:
        payload = {"version": 1, "workspaces": self._workspaces}
        _atomic_write_text(self._path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def resolve_project_root(self, raw: str) -> Path:
        s = str(raw or "").strip()
        if not s:
            raise ValueError("project_root must be non-empty")

        # Support common Windows path formats when the backend runs under WSL.
        # Examples:
        #   - D:\Work\Proj   -> /mnt/d/Work/Proj
        #   - D:/Work/Proj    -> /mnt/d/Work/Proj
        #   - /mnt/d/Work/Proj (already WSL)
        win_drive = re.match(r"^([a-zA-Z]):[\\/](.*)$", s)
        if win_drive and _is_wsl():
            drive = win_drive.group(1).lower()
            rest = win_drive.group(2)
            # Normalize to posix-like segments.
            rest = str(PureWindowsPath(rest)).replace("\\", "/")
            s = f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"

        p = Path(s).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p)
        resolved = p.resolve()
        return resolved

    def workspace_id_for_project_root(self, project_root: Path) -> str:
        resolved = project_root.expanduser().resolve()
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
        return f"ws_{digest[:12]}"

    def register(self, *, project_root: str) -> WorkspaceRecord:
        resolved = self.resolve_project_root(project_root)

        if resolved.exists() and not resolved.is_dir():
            raise ValueError(f"path exists and is not a directory: {resolved}")

        resolved.mkdir(parents=True, exist_ok=True)

        # Initialize `.aura` scaffolding.
        init_workspace(resolved)

        wid = self.workspace_id_for_project_root(resolved)
        ts = now_ts_ms()
        rec = self._workspaces.get(wid) or {}
        rec["project_root"] = str(resolved)
        rec["last_used_at"] = ts
        self._workspaces[wid] = rec
        self._save()

        return WorkspaceRecord(workspace_id=wid, project_root=str(resolved), last_used_at=ts)

    def touch(self, workspace_id: str) -> None:
        wid = str(workspace_id or "").strip()
        if not wid:
            return
        rec = self._workspaces.get(wid)
        if not rec:
            return
        rec["last_used_at"] = now_ts_ms()
        self._workspaces[wid] = rec
        try:
            self._save()
        except Exception:
            return

    def get_project_root(self, workspace_id: str) -> Path:
        wid = str(workspace_id or "").strip()
        rec = self._workspaces.get(wid)
        if not rec:
            raise KeyError(wid)
        pr = rec.get("project_root")
        if not isinstance(pr, str) or not pr.strip():
            raise KeyError(wid)
        resolved = Path(pr).expanduser().resolve()
        return resolved

    def list_workspaces(self) -> list[WorkspaceRecord]:
        out: list[WorkspaceRecord] = []
        for wid, rec in self._workspaces.items():
            pr = rec.get("project_root")
            if not isinstance(pr, str) or not pr.strip():
                continue
            last_used = rec.get("last_used_at")
            out.append(
                WorkspaceRecord(
                    workspace_id=wid,
                    project_root=pr,
                    last_used_at=last_used if isinstance(last_used, int) else None,
                )
            )
        out.sort(key=lambda r: r.last_used_at or 0, reverse=True)
        return out
