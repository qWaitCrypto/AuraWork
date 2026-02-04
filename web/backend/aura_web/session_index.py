from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aura.runtime.ids import now_ts_ms


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True, slots=True)
class SessionIndexRecord:
    session_id: str
    workspace_id: str
    created_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "created_at": self.created_at,
        }


class SessionIndex:
    """Persistent mapping: session_id -> workspace_id."""

    def __init__(self, *, state_dir: Path) -> None:
        self._path = state_dir.expanduser().resolve() / "session_index.json"
        self._sessions: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._sessions = {}
            return
        except Exception:
            self._sessions = {}
            return

        try:
            data = json.loads(raw)
        except Exception:
            self._sessions = {}
            return

        sessions = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(sessions, dict):
            self._sessions = {}
            return

        cleaned: dict[str, dict[str, Any]] = {}
        for sid, rec in sessions.items():
            if not isinstance(sid, str) or not sid.strip():
                continue
            if not isinstance(rec, dict):
                continue
            wid = rec.get("workspace_id")
            if not isinstance(wid, str) or not wid.strip():
                continue
            created_at = rec.get("created_at")
            cleaned[sid.strip()] = {
                "workspace_id": wid.strip(),
                "created_at": created_at if isinstance(created_at, int) else None,
            }
        self._sessions = cleaned

    def _save(self) -> None:
        payload = {"version": 1, "sessions": self._sessions}
        _atomic_write_text(self._path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def get_workspace_id(self, session_id: str) -> str | None:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        rec = self._sessions.get(sid)
        if not rec:
            return None
        wid = rec.get("workspace_id")
        return wid if isinstance(wid, str) and wid.strip() else None

    def set(self, *, session_id: str, workspace_id: str) -> None:
        sid = str(session_id or "").strip()
        wid = str(workspace_id or "").strip()
        if not sid or not wid:
            raise ValueError("session_id and workspace_id are required")

        rec = self._sessions.get(sid) or {}
        rec.setdefault("created_at", now_ts_ms())
        rec["workspace_id"] = wid
        self._sessions[sid] = rec
        self._save()

    def has(self, session_id: str) -> bool:
        return self.get_workspace_id(session_id) is not None

    def delete(self, session_id: str) -> bool:
        sid = str(session_id or "").strip()
        if not sid:
            return False
        if sid not in self._sessions:
            return False
        self._sessions.pop(sid, None)
        self._save()
        return True
