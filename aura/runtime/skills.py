from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str
    skill_dir: Path
    public_skill_dir: str
    skill_md_path: Path
    allowed_tools: list[str] | None = None
    metadata: dict[str, Any] | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    meta: SkillMetadata
    instructions: str
    resources: list[str]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.meta.name,
            "description": self.meta.description,
            "allowed_tools": list(self.meta.allowed_tools) if self.meta.allowed_tools is not None else None,
            "metadata": dict(self.meta.metadata) if self.meta.metadata is not None else None,
            "skill_root": self.meta.public_skill_dir,
            "instructions": self.instructions,
            "resources": list(self.resources),
        }


class SkillStore:
    def __init__(self, *, project_root: Path) -> None:
        self._project_root = project_root.expanduser().resolve()
        self._skills_root = self._project_root / ".aura" / "skills"
        self._by_name: dict[str, SkillMetadata] = {}
        self._loaded: dict[str, Any] = {}
        self._source_by_name: dict[str, str] = {}
        self._warnings: list[str] = []
        self._did_refresh = False

    @property
    def warnings(self) -> list[str]:
        self._ensure_ready()
        return list(self._warnings)

    def _ensure_ready(self) -> None:
        if not self._did_refresh:
            self.refresh()

    def refresh(self) -> None:
        self._by_name = {}
        self._loaded = {}
        self._source_by_name = {}
        self._warnings = []
        self._did_refresh = True

        try:
            from agno.skills.errors import SkillValidationError
            from agno.skills.loaders.local import LocalSkills
        except Exception as e:
            self._warnings.append(f"Agno skills loader unavailable: {e}")
            return

        try:
            seed_builtin_skills(project_root=self._project_root)
        except Exception as e:
            self._warnings.append(f"Failed to seed built-in skills into .aura/skills: {e}")

        skill_sources: list[tuple[str, list[Path]]] = []

        builtin_root = _builtin_skills_root()
        if builtin_root.exists() and builtin_root.is_dir():
            skill_sources.append(("builtin", _discover_skill_dirs(builtin_root)))

        if self._skills_root.exists():
            if not self._skills_root.is_dir():
                self._warnings.append("Skills root is not a directory: .aura/skills")
            else:
                skill_sources.append(("project", _discover_skill_dirs(self._skills_root)))

        for source, skill_dirs in skill_sources:
            for skill_dir in skill_dirs:
                loader = LocalSkills(str(skill_dir), validate=True)
                try:
                    loaded = loader.load()
                except SkillValidationError as e:
                    self._warnings.append(f"Skipped invalid skill at {skill_dir}: {e}")
                    continue
                except Exception as e:
                    self._warnings.append(f"Failed to load skill at {skill_dir}: {e}")
                    continue

                if not loaded:
                    continue
                if len(loaded) > 1:
                    self._warnings.append(f"Multiple skills discovered under {skill_dir}; using the first.")

                skill = loaded[0]
                name_raw = getattr(skill, "name", None)
                description_raw = getattr(skill, "description", None)
                if not isinstance(name_raw, str) or not name_raw.strip():
                    self._warnings.append(f"Skipped skill missing name: {skill_dir / 'SKILL.md'}")
                    continue
                if not isinstance(description_raw, str) or not description_raw.strip():
                    self._warnings.append(f"Skipped skill missing description: {skill_dir / 'SKILL.md'}")
                    continue

                name = _sanitize_single_line(name_raw.strip())
                description = _sanitize_single_line(description_raw.strip())

                allowed_tools_raw = getattr(skill, "allowed_tools", None)
                allowed_tools: list[str] | None = None
                if isinstance(allowed_tools_raw, list):
                    cleaned = [str(item).strip() for item in allowed_tools_raw if isinstance(item, str) and item.strip()]
                    allowed_tools = cleaned or None

                meta_val = getattr(skill, "metadata", None)
                meta = meta_val if isinstance(meta_val, dict) else None

                if name in self._by_name:
                    existing_source = self._source_by_name.get(name, "unknown")
                    existing = self._by_name[name].skill_md_path
                    # Project-local skills override built-ins silently.
                    if source == "project" and existing_source == "builtin":
                        pass
                    else:
                        self._warnings.append(
                            f"Duplicate skill name '{name}' at {skill_dir / 'SKILL.md'} (already have {existing}); keeping first."
                        )
                        continue

                skill_md_path = (skill_dir / "SKILL.md").resolve()
                resolved_dir = skill_dir.resolve()
                try:
                    public_skill_dir = str(resolved_dir.relative_to(self._project_root))
                except Exception:
                    public_skill_dir = str(resolved_dir)
                self._by_name[name] = SkillMetadata(
                    name=name,
                    description=description.strip(),
                    skill_dir=resolved_dir,
                    public_skill_dir=public_skill_dir,
                    skill_md_path=skill_md_path,
                    allowed_tools=allowed_tools,
                    metadata=meta,
                )
                self._loaded[name] = skill
                self._source_by_name[name] = source

    def list(self) -> list[SkillMetadata]:
        self._ensure_ready()
        return [self._by_name[name] for name in sorted(self._by_name)]

    def get(self, name: str) -> SkillMetadata | None:
        self._ensure_ready()
        return self._by_name.get(name)

    def load(self, name: str) -> LoadedSkill:
        self._ensure_ready()
        meta = self._by_name.get(name)
        if meta is None:
            raise ValueError(f"Unknown skill: {name}")
        skill = self._loaded.get(meta.name)
        if skill is None:
            raise ValueError(f"Skill not loaded: {name}")
        instructions = getattr(skill, "instructions", "")
        if not isinstance(instructions, str):
            instructions = str(instructions)
        resources = _list_resources(meta.skill_dir)
        return LoadedSkill(meta=meta, instructions=instructions, resources=resources)


def seed_builtin_skills(*, project_root: Path) -> list[str]:
    """
    Seed the built-in skill library into `<project>/.aura/skills/`.

    Built-in skills are shipped as a directory tree under `aura/builtin/skills/`.
    Each skill is a directory containing `SKILL.md` plus optional resource files.

    This function is intentionally conservative: it never overwrites existing skill directories.
    """

    skills_root = project_root.expanduser().resolve() / ".aura" / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)

    builtin_root = _builtin_skills_root()
    if not builtin_root.exists() or not builtin_root.is_dir():
        # Skills are optional; projects can manage their own `.aura/skills/` without bundled defaults.
        return []

    skipped: list[str] = []
    for skill_md in sorted(builtin_root.rglob("SKILL.md")):
        try:
            rel_dir = skill_md.parent.relative_to(builtin_root)
        except Exception:
            continue
        if not rel_dir.parts:
            continue
        if any(part.startswith(".") for part in rel_dir.parts):
            continue
        target_dir = (skills_root / rel_dir).resolve()
        if target_dir.exists():
            skipped.append(str(Path(".aura/skills") / rel_dir))
            continue
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            skill_md.parent,
            target_dir,
            dirs_exist_ok=False,
            ignore=shutil.ignore_patterns(".*"),
        )
    return skipped


def _sanitize_single_line(raw: str) -> str:
    return " ".join(raw.split())


def _list_resources(skill_dir: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(skill_dir.rglob("*")):
        try:
            if not path.is_file() or path.is_symlink():
                continue
        except OSError:
            continue
        if path.name == "SKILL.md":
            continue
        if any(part.startswith(".") for part in path.relative_to(skill_dir).parts):
            continue
        out.append(str(path.relative_to(skill_dir)))
    return out


def _discover_skill_dirs(root: Path) -> list[Path]:
    """Discover skill directories under `root` by finding `SKILL.md` recursively."""

    skill_dirs: list[Path] = []
    seen: set[Path] = set()
    for skill_md in sorted(root.rglob("SKILL.md")):
        try:
            if not skill_md.is_file() or skill_md.is_symlink():
                continue
        except OSError:
            continue
        try:
            rel_dir = skill_md.parent.relative_to(root)
        except Exception:
            continue
        if not rel_dir.parts:
            continue
        if any(part.startswith(".") for part in rel_dir.parts):
            continue
        resolved_dir = skill_md.parent.resolve()
        if resolved_dir in seen:
            continue
        seen.add(resolved_dir)
        skill_dirs.append(skill_md.parent)
    return skill_dirs


def _builtin_skills_root() -> Path:
    package_root = Path(__file__).resolve().parent.parent
    return package_root / "builtin" / "skills"
