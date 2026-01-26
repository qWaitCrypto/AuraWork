from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..plan import PlanItem, StepStatus, TodoStore, validate_todo


@dataclass(frozen=True, slots=True)
class UpdateTodoTool:
    store: TodoStore
    name: str = "update_todo"
    description: str = (
        "Updates a linear todo checklist (no dependencies).\n"
        "Provide an optional explanation and a list of todo items.\n"
        "At most one item can be in_progress at a time."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "explanation": {"type": "string"},
                "todo": {
                    "type": "array",
                    "description": "The todo items list (no depends_on).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Todo id (unique; optional)."},
                            "step": {"type": "string"},
                            "status": {
                                "type": "string",
                                "description": "One of: pending, in_progress, completed",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                        "required": ["step", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["todo"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root) -> dict[str, Any]:
        del project_root
        raw_todo = args.get("todo")
        explanation = args.get("explanation")

        if not isinstance(raw_todo, list):
            raise ValueError("Missing or invalid 'todo' (expected list).")
        if explanation is not None and not isinstance(explanation, str):
            raise ValueError("Invalid 'explanation' (expected string).")

        items: list[PlanItem] = []
        for i, raw in enumerate(raw_todo):
            if not isinstance(raw, dict):
                raise ValueError("Invalid todo item (expected object).")

            raw_id = raw.get("id")
            if raw_id is None:
                raw = dict(raw)
                raw["id"] = f"todo-{i+1}"

            step = raw.get("step")
            status = raw.get("status")
            if not isinstance(step, str) or not step.strip():
                raise ValueError("TodoItem.step must be a non-empty string.")
            if not isinstance(status, str) or not status:
                raise ValueError("TodoItem.status must be a non-empty string.")
            try:
                st = StepStatus(status)
            except ValueError as e:
                raise ValueError(f"Invalid todo status: {status!r}") from e
            if st is StepStatus.FAILED:
                raise ValueError("TodoItem.status cannot be 'failed'.")

            item_id = raw.get("id")
            if not isinstance(item_id, str) or not item_id.strip():
                raise ValueError("TodoItem.id must be a non-empty string.")

            items.append(PlanItem(id=item_id.strip(), step=step.strip(), status=st, depends_on=[]))

        validate_todo(items)
        self.store.set(items, explanation=explanation)
        return {"ok": True, "message": "Todo updated", "items": len(items)}

