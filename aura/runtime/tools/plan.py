from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import WorkSpec
from ..plan import PlanItem, PlanStore, StepStatus, validate_plan
from ..subagents.presets import get_preset, list_presets


@dataclass(frozen=True, slots=True)
class UpdatePlanTool:
    store: PlanStore
    name: str = "update_plan"
    description: str = (
        "Updates the task plan.\n"
        "Provide an optional explanation and a list of plan items, each with an id, step, and status.\n"
        "At most one step can be in_progress at a time."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Global goal for the DAG plan (required)."},
                "explanation": {"type": "string"},
                "plan": {
                    "type": "array",
                    "description": "The list of steps",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Node id (unique)."},
                            "step": {"type": "string"},
                            "status": {
                                "type": "string",
                                "description": "One of: pending, in_progress, completed",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                            "depends_on": {
                                "type": "array",
                                "description": "List of dependency node ids.",
                                "items": {"type": "string"},
                            },
                            "metadata": {
                                "type": "object",
                                "description": (
                                    "Per-node execution contract (required). "
                                    "Must include `preset` and `work_spec` for automated DAG execution."
                                ),
                                "properties": {
                                    "preset": {
                                        "type": "string",
                                        "enum": list_presets(),
                                        "description": "Subagent preset for this node.",
                                    },
                                    "work_spec": {
                                        "type": "object",
                                        "description": "WorkSpec for this node's delegated execution.",
                                        "properties": {
                                            "goal": {"type": "string", "description": "What/why/how/outputs for this node."},
                                            "intent_items": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "id": {"type": "string"},
                                                        "text": {"type": "string"},
                                                    },
                                                    "required": ["id", "text"],
                                                    "additionalProperties": False,
                                                },
                                            },
                                            "inputs": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "type": {"type": "string"},
                                                        "path": {"type": "string"},
                                                        "description": {"type": "string"},
                                                    },
                                                    "additionalProperties": True,
                                                },
                                            },
                                            "expected_outputs": {
                                                "type": "array",
                                                "minItems": 1,
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "type": {
                                                            "type": "string",
                                                            "enum": ["document", "spreadsheet", "index", "report", "other"],
                                                        },
                                                        "format": {"type": "string"},
                                                        "path": {"type": "string"},
                                                    },
                                                    "required": ["type", "format"],
                                                    "additionalProperties": True,
                                                },
                                            },
                                            "constraints": {
                                                "type": "object",
                                                "properties": {
                                                    "style": {"type": "string"},
                                                    "template": {"type": "string"},
                                                    "deadline": {"type": "string"},
                                                    "forbidden": {"type": "array", "items": {"type": "string"}},
                                                },
                                                "additionalProperties": True,
                                            },
                                            "resource_scope": {
                                                "type": "object",
                                                "properties": {
                                                    "workspace_roots": {"type": "array", "items": {"type": "string"}},
                                                    "file_type_allowlist": {"type": "array", "items": {"type": "string"}},
                                                    "domain_allowlist": {"type": "array", "items": {"type": "string"}},
                                                },
                                                "additionalProperties": True,
                                            },
                                            "approval_policy": {"type": "object", "additionalProperties": True},
                                        },
                                        "required": ["goal", "expected_outputs", "resource_scope"],
                                        "additionalProperties": True,
                                    },
                                },
                                "required": ["preset", "work_spec"],
                                "additionalProperties": True,
                            },
                        },
                        "required": ["id", "step", "status", "metadata"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["goal", "plan"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root) -> dict[str, Any]:
        del project_root
        raw_plan = args.get("plan")
        raw_goal = args.get("goal")
        explanation = args.get("explanation")

        if not isinstance(raw_plan, list):
            raise ValueError("Missing or invalid 'plan' (expected list).")
        if not isinstance(raw_goal, str) or not raw_goal.strip():
            raise ValueError("Missing or invalid 'goal' (expected non-empty string).")
        if explanation is not None and not isinstance(explanation, str):
            raise ValueError("Invalid 'explanation' (expected string).")

        goal = raw_goal.strip()

        items: list[PlanItem] = []
        for i, raw in enumerate(raw_plan):
            if not isinstance(raw, dict):
                raise ValueError("Invalid plan item (expected object).")
            raw_id = raw.get("id")
            if not isinstance(raw_id, str) or not raw_id.strip():
                raise ValueError("PlanItem.id must be a non-empty string.")
            step = raw.get("step")
            status = raw.get("status")
            if not isinstance(step, str) or not step.strip():
                raise ValueError("PlanItem.step must be a non-empty string.")
            if not isinstance(status, str) or not status:
                raise ValueError("PlanItem.status must be a non-empty string.")
            try:
                st = StepStatus(status)
            except ValueError as e:
                raise ValueError(f"Invalid plan status: {status!r}") from e

            depends_raw = raw.get("depends_on")
            depends_on: list[str] = []
            if depends_raw is None:
                depends_on = []
            elif isinstance(depends_raw, list):
                depends_on = [str(d).strip() for d in depends_raw if isinstance(d, str) and str(d).strip()]
            else:
                raise ValueError("Invalid depends_on (expected list of strings).")

            metadata_raw = raw.get("metadata")
            if not isinstance(metadata_raw, dict):
                raise ValueError("Invalid metadata (expected object).")
            metadata = dict(metadata_raw)

            preset_raw = metadata.get("preset")
            if not isinstance(preset_raw, str) or not preset_raw.strip():
                raise ValueError("Missing or invalid metadata.preset (expected non-empty string).")
            preset_name = preset_raw.strip()
            if get_preset(preset_name) is None:
                raise ValueError(f"Unknown metadata.preset: {preset_name!r}")
            metadata["preset"] = preset_name

            ws_raw = metadata.get("work_spec")
            if not isinstance(ws_raw, dict):
                raise ValueError("Missing or invalid metadata.work_spec (expected object).")
            try:
                ws = WorkSpec.model_validate(ws_raw)
            except Exception as e:
                raise ValueError(f"Invalid metadata.work_spec: {e}") from e
            metadata["work_spec"] = ws.model_dump(mode="json")

            items.append(PlanItem(id=raw_id.strip(), step=step.strip(), status=st, depends_on=depends_on, metadata=metadata))

        validate_plan(items)
        self.store.set(items, goal=goal, explanation=explanation)
        return {"ok": True, "message": "Plan updated", "steps": len(items)}
