from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from aura.runtime.models.workspec import WorkSpec
from aura.runtime.skills import LoadedSkill, SkillMetadata
from aura.runtime.stores.fs import FileArtifactStore
from aura.runtime.tools.registry import ToolRegistry
from aura.runtime.tools.runtime import InspectionDecision, ToolApprovalMode, ToolRuntime
from aura.runtime.tools.skills import SkillLoadTool


@dataclass
class _DummyTool:
    name: str
    description: str = "dummy"
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})

    def execute(self, *, args: dict[str, Any], project_root, context=None) -> dict[str, Any]:
        _ = args
        _ = project_root
        _ = context
        return {"ok": True}


class _StubSkillStore:
    def load(self, name: str) -> LoadedSkill:
        skill_dir = Path("/tmp/project/.aura/skills") / name
        meta = SkillMetadata(
            name=name,
            description="stub",
            skill_dir=skill_dir,
            public_skill_dir=f".aura/skills/{name}",
            skill_md_path=skill_dir / "SKILL.md",
        )
        return LoadedSkill(meta=meta, instructions="stub", resources=["docs/plan_spec.md", "scripts/run.py"])


def _work_spec() -> WorkSpec:
    return WorkSpec.model_validate(
        {
            "goal": "doc",
            "expected_outputs": [{"type": "document", "format": "docx", "path": "artifacts/out.docx"}],
            "resource_scope": {
                "workspace_roots": ["."],
                "file_type_allowlist": ["docx", "json", "txt", "md"],
                "domain_allowlist": [],
            },
        }
    )


def test_work_spec_allows_skill_tree_reads_without_opening_python_globally() -> None:
    with TemporaryDirectory() as tmp:
        project_root = Path(tmp)
        (project_root / ".aura/skills/aura-docx/scripts").mkdir(parents=True)
        (project_root / ".aura/skills/aura-docx/docs").mkdir(parents=True)
        (project_root / "src").mkdir(parents=True)
        (project_root / ".aura/skills/aura-docx/scripts/run.py").write_text("print('ok')\n", encoding="utf-8")
        (project_root / ".aura/skills/aura-docx/docs/plan_spec.md").write_text("# plan\n", encoding="utf-8")
        (project_root / "src/main.py").write_text("print('nope')\n", encoding="utf-8")

        registry = ToolRegistry()
        registry.register(_DummyTool("project__read_text"))
        registry.register(_DummyTool("project__read_text_many"))
        runtime = ToolRuntime(
            project_root=project_root,
            registry=registry,
            artifact_store=FileArtifactStore(project_root / ".artifacts"),
            approval_mode=ToolApprovalMode.STANDARD,
        )

        with runtime.work_spec_context(_work_spec()):
            skill_read = runtime.inspect(
                runtime.plan(
                    tool_execution_id="tool-1",
                    tool_name="project__read_text_many",
                    tool_call_id="call-1",
                    arguments={
                        "paths": [
                            ".aura/skills/aura-docx/docs/plan_spec.md",
                            ".aura/skills/aura-docx/scripts/run.py",
                        ]
                    },
                )
            )
            repo_py_read = runtime.inspect(
                runtime.plan(
                    tool_execution_id="tool-2",
                    tool_name="project__read_text",
                    tool_call_id="call-2",
                    arguments={"path": "src/main.py"},
                )
            )

        assert skill_read.decision is InspectionDecision.ALLOW
        assert repo_py_read.decision is InspectionDecision.DENY
        assert repo_py_read.reason == "WorkSpec scope violation: file type not in allowlist: src/main.py"


def test_skill_load_exposes_entrypoints_and_access_hints() -> None:
    with TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / ".aura" / "skills" / "aura-docx"
        (skill_dir / "scripts").mkdir(parents=True)
        (skill_dir / "docs").mkdir(parents=True)
        (skill_dir / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
        (skill_dir / "docs" / "plan_spec.md").write_text("# plan\n", encoding="utf-8")

        meta = SkillMetadata(
            name="aura-docx",
            description="stub",
            skill_dir=skill_dir,
            public_skill_dir=f".aura/skills/aura-docx",
            skill_md_path=skill_dir / "SKILL.md",
        )

        class _LocalSkillStore:
            def load(self, name: str) -> LoadedSkill:
                return LoadedSkill(meta=meta, instructions="stub", resources=["docs/plan_spec.md", "scripts/run.py"])

        tool = SkillLoadTool(store=_LocalSkillStore())
        result = tool.execute(args={"name": "aura-docx"}, project_root=".")

        skill = result["skill"]
        assert skill["access_hints"]["preferred_read_tool"] == "skill__read_file"
        assert "heredoc" in " ".join(skill["access_hints"]["notes"]).lower()
        assert skill["entrypoints"]["runner_script"] == "scripts/run.py"
        assert skill["entrypoints"]["plan_spec"] == "docs/plan_spec.md"
        assert "<skill_root>/scripts/run.py" in skill["entrypoints"]["run_command_template"]
