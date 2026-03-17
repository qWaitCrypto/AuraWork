from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from ..skills import SkillStore


@dataclass(frozen=True, slots=True)
class SkillListTool:
    store: SkillStore
    name: str = "skill__list"
    description: str = "List discovered skills as {name, description} metadata."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root) -> dict[str, Any]:
        del args, project_root
        skills = [m.to_public_dict() for m in self.store.list()]
        skills_text = "\n".join(
            f"- {skill['name']}: {skill['description']}" for skill in skills if skill.get("name") and skill.get("description")
        )
        warnings = list(self.store.warnings)
        warnings_text = "\n".join(f"- {w}" for w in warnings)
        return {
            "ok": True,
            "skills": skills,
            "warnings": warnings,
            "skills_text": skills_text,
            "warnings_text": warnings_text,
        }


@dataclass(frozen=True, slots=True)
class SkillLoadTool:
    store: SkillStore
    name: str = "skill__load"
    description: str = (
        "Load a skill by name and return its full instructions (SKILL.md body) "
        "plus a list of supporting files in the skill directory."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill id/name from skill__list."},
            },
            "required": ["name"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root) -> dict[str, Any]:
        del project_root
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Missing or invalid 'name' (expected non-empty string).")
        loaded = self.store.load(name.strip())
        skill_payload = loaded.to_public_dict()
        skill_payload["access_hints"] = _skill_access_hints(loaded.meta)
        skill_payload["entrypoints"] = _skill_entrypoints(loaded.meta)
        skill_payload["recommended_reads"] = _recommended_skill_reads(loaded.meta)
        return {
            "ok": True,
            "skill": skill_payload,
        }


@dataclass(frozen=True, slots=True)
class SkillReadFileTool:
    store: SkillStore
    name: str = "skill__read_file"
    description: str = "Read a UTF-8 text resource file from within a skill directory."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill id/name from skill__list."},
                "path": {"type": "string", "description": "Relative path within the skill directory."},
                "max_chars": {"type": "integer", "minimum": 1, "description": "Maximum chars to return (default 12000)."},
            },
            "required": ["name", "path"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root) -> dict[str, Any]:
        del project_root
        name = args.get("name")
        rel = args.get("path")
        max_chars = args.get("max_chars") or 12000

        if not isinstance(name, str) or not name.strip():
            raise ValueError("Missing or invalid 'name' (expected non-empty string).")
        if not isinstance(rel, str) or not rel.strip():
            raise ValueError("Missing or invalid 'path' (expected non-empty string).")
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
            raise ValueError("Invalid 'max_chars' (expected int >= 1).")

        meta = self.store.get(name.strip())
        if meta is None:
            raise ValueError(f"Unknown skill: {name.strip()}")

        rel_path = Path(rel)
        if rel_path.is_absolute():
            raise PermissionError("Path must be relative to the skill directory.")
        target = (meta.skill_dir / rel_path).resolve()
        base = meta.skill_dir.resolve()
        if target != base and base not in target.parents:
            raise PermissionError("Path escapes the skill directory.")
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"Skill resource not found: {rel}")

        text = target.read_text(encoding="utf-8", errors="replace")
        truncated = False
        if len(text) > max_chars:
            truncated = True
            text = text[:max_chars]

        return {
            "ok": True,
            "skill": meta.name,
            "path": str(rel_path),
            "truncated": truncated,
            "content": text,
        }


def _recommended_skill_reads(meta: SkillMetadata) -> list[str]:
    candidates = [
        "docs/plan_spec.md",
        "docs/cli_reference.md",
        "docs/python_docx_guide.md",
        "docs/safety.md",
        "reference_ooxml.md",
        "scripts/run.py",
    ]
    out: list[str] = []
    for rel in candidates:
        if (meta.skill_dir / rel).is_file():
            out.append(rel)
    return out


def _skill_access_hints(meta: SkillMetadata) -> dict[str, Any]:
    return {
        "skill_root": meta.public_skill_dir,
        "preferred_read_tool": "skill__read_file",
        "path_style": "Use paths relative to the skill root.",
        "notes": [
            "Prefer skill__read_file for files inside this skill directory; it stays inside the skill sandbox.",
            "If this skill exposes scripts/run.py, execute that runner directly via shell__run as one command.",
            "Do not wrap skill runners in inline Python, shell heredocs, or shell redirection.",
        ],
    }


def _skill_entrypoints(meta: SkillMetadata) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if (meta.skill_dir / "scripts" / "run.py").is_file():
        out["runner_script"] = "scripts/run.py"
        out["cwd"] = "."
        out["run_command_template"] = (
            'python "<skill_root>/scripts/run.py" <INPUT_PATH_OR_-> <PLAN_JSON_PATH> '
            '--out <OUTPUT_PATH> --artifacts-dir <RUN_ARTIFACTS_DIR>'
        )
    plan_spec = meta.skill_dir / "docs" / "plan_spec.md"
    if plan_spec.is_file():
        out["plan_spec"] = "docs/plan_spec.md"
    return out
