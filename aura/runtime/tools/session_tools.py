from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .builtins import _maybe_int, _maybe_bool, _maybe_str_list, _require_str, _resolve_in_project
from ..ids import now_ts_ms


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _extract_artifact_locators(obj: Any) -> list[str]:
    locators: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith("_ref") and isinstance(v, dict):
                locator = v.get("locator")
                if isinstance(locator, str) and locator:
                    locators.append(locator)
            locators.extend(_extract_artifact_locators(v))
    if isinstance(obj, list):
        for item in obj:
            locators.extend(_extract_artifact_locators(item))
    return locators


def _iter_session_ids(project_root: Path) -> list[str]:
    sessions_dir = project_root / ".aura" / "sessions"
    if not sessions_dir.exists():
        return []
    out: list[str] = []
    for p in sorted(sessions_dir.glob("sess_*.json")):
        out.append(p.stem)
    return out


def _load_session_meta(project_root: Path, session_id: str) -> dict[str, Any] | None:
    path = project_root / ".aura" / "sessions" / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _artifact_text(project_root: Path, locator: str, *, max_chars: int) -> str:
    art_path = project_root / ".aura" / "artifacts" / locator
    try:
        data = art_path.read_bytes()
    except Exception:
        return ""
    text = data.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


def _find_snippet(text: str, query: str, *, max_chars: int) -> str:
    if not text:
        return ""
    low = text.lower()
    q = query.lower()
    idx = low.find(q)
    if idx == -1:
        return ""
    start = max(0, idx - max_chars // 2)
    end = min(len(text), start + max_chars)
    snippet = text[start:end]
    snippet = snippet.replace("\n", " ").replace("\r", " ")
    snippet = re.sub(r"\s+", " ", snippet).strip()
    return snippet


@dataclass(frozen=True, slots=True)
class SessionSearchTool:
    name: str = "session__search"
    description: str = (
        "Search recorded chat sessions in this project for a keyword by scanning session events and referenced artifacts."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search string (case-insensitive)."},
                "session_id": {"type": "string", "description": "Optional session id to search within."},
                "max_results": {"type": "integer", "minimum": 1, "description": "Maximum matches to return (default 20)."},
                "max_chars_per_snippet": {
                    "type": "integer",
                    "minimum": 20,
                    "description": "Maximum characters per snippet (default 160).",
                },
                "max_chars_per_artifact": {
                    "type": "integer",
                    "minimum": 100,
                    "description": "Maximum characters to read per referenced artifact (default 8000).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        query = _require_str(args, "query")
        max_results = _maybe_int(args, "max_results") or 20
        max_chars_per_snippet = _maybe_int(args, "max_chars_per_snippet") or 160
        max_chars_per_artifact = _maybe_int(args, "max_chars_per_artifact") or 8000

        sid_raw = args.get("session_id")
        session_ids = [str(sid_raw).strip()] if isinstance(sid_raw, str) and sid_raw.strip() else _iter_session_ids(project_root)

        matches: list[dict[str, Any]] = []
        truncated = False

        for sid in session_ids:
            events_path = project_root / ".aura" / "events" / f"{sid}.jsonl"
            if not events_path.exists():
                continue
            for ev in _read_json_lines(events_path):
                if len(matches) >= max_results:
                    truncated = True
                    break
                payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
                locs = _extract_artifact_locators(payload)
                for locator in locs:
                    if len(matches) >= max_results:
                        truncated = True
                        break
                    text = _artifact_text(project_root, locator, max_chars=max_chars_per_artifact)
                    snippet = _find_snippet(text, query, max_chars=max_chars_per_snippet)
                    if not snippet:
                        continue
                    matches.append(
                        {
                            "session_id": sid,
                            "event_kind": ev.get("kind"),
                            "timestamp": ev.get("timestamp"),
                            "artifact_locator": locator,
                            "snippet": snippet,
                        }
                    )
                if truncated:
                    break
            if truncated:
                break

        return {
            "ok": True,
            "query": query,
            "session_ids_scanned": session_ids,
            "truncated": truncated,
            "matches": matches,
        }


@dataclass(frozen=True, slots=True)
class SessionExportTool:
    name: str = "session__export"
    description: str = (
        "Export a session into a portable bundle directory under the project root (events + session meta + artifacts). "
        "This is a high-risk write operation and typically requires user approval."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session id to export."},
                "out_dir": {
                    "type": "string",
                    "description": "Project-relative output directory (default 'out').",
                },
                "include_artifacts": {
                    "type": "boolean",
                    "description": "Include referenced artifacts (default true).",
                },
            },
            "required": ["session_id"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        session_id = _require_str(args, "session_id").strip()
        out_dir = str(args.get("out_dir") or "out")
        include_artifacts = _maybe_bool(args, "include_artifacts")
        if include_artifacts is None:
            include_artifacts = True

        meta_path = project_root / ".aura" / "sessions" / f"{session_id}.json"
        events_path = project_root / ".aura" / "events" / f"{session_id}.jsonl"
        if not meta_path.exists() or not events_path.exists():
            return {"ok": False, "session_id": session_id, "error": "Session not found."}

        out_base = _resolve_in_project(project_root, out_dir)
        out_base.mkdir(parents=True, exist_ok=True)
        bundle_dir = out_base / f"aura_bundle_{session_id}_{now_ts_ms()}"
        bundle_dir.mkdir(parents=True, exist_ok=True)

        shutil.copy2(meta_path, bundle_dir / "session.json")
        shutil.copy2(events_path, bundle_dir / "events.jsonl")

        artifacts_copied = 0
        locators: set[str] = set()
        if include_artifacts:
            for ev in _read_json_lines(events_path):
                payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
                for locator in _extract_artifact_locators(payload):
                    locators.add(locator)

            artifacts_out = bundle_dir / "artifacts"
            artifacts_out.mkdir(parents=True, exist_ok=True)
            for locator in sorted(locators):
                src = project_root / ".aura" / "artifacts" / locator
                if not src.exists():
                    continue
                shutil.copy2(src, artifacts_out / locator)
                artifacts_copied += 1

        manifest = {
            "session_id": session_id,
            "includes": {
                "session_json": "session.json",
                "events_jsonl": "events.jsonl",
                "artifacts_dir": "artifacts" if include_artifacts else None,
            },
            "artifacts_count": artifacts_copied,
        }
        (bundle_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        return {
            "ok": True,
            "session_id": session_id,
            "bundle_dir": str(bundle_dir.relative_to(project_root)),
            "artifacts_included": include_artifacts,
            "artifacts_count": artifacts_copied,
        }
