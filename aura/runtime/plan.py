from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .ids import now_ts_ms
from .stores import SessionStore
from .dag.graph import DAG, DagError


class StepStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PlanItem:
    id: str
    step: str
    status: StepStatus
    depends_on: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    error_trace: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = {"id": self.id, "step": self.step, "status": self.status.value, "depends_on": list(self.depends_on)}
        if self.metadata:
            d["metadata"] = dict(self.metadata)
        if self.error_trace:
            d["error_trace"] = list(self.error_trace)
        return d

    @staticmethod
    def from_dict(raw: dict[str, Any], *, default_id: str | None = None) -> "PlanItem":
        raw_id = raw.get("id")
        if isinstance(raw_id, str) and raw_id.strip():
            item_id = raw_id.strip()
        else:
            item_id = str(default_id or "").strip()
            if not item_id:
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

        deps_raw = raw.get("depends_on")
        depends_on: list[str] = []
        if deps_raw is None:
            depends_on = []
        elif isinstance(deps_raw, list):
            for dep in deps_raw:
                if not isinstance(dep, str) or not dep.strip():
                    raise ValueError("PlanItem.depends_on must be a list of non-empty strings.")
                depends_on.append(dep.strip())
        else:
            raise ValueError("PlanItem.depends_on must be a list of strings.")

        metadata = raw.get("metadata") or {}
        error_trace = raw.get("error_trace") or []
        return PlanItem(id=item_id, step=step.strip(), status=st, depends_on=depends_on,
                        metadata=metadata if isinstance(metadata, dict) else {},
                        error_trace=error_trace if isinstance(error_trace, list) else [])


@dataclass(frozen=True, slots=True)
class PlanState:
    plan: list[PlanItem]
    goal: str | None = None
    explanation: str | None = None
    updated_at: int | None = None


def validate_plan(items: list[PlanItem]) -> None:
    in_progress = sum(1 for t in items if t.status is StepStatus.IN_PROGRESS)
    if in_progress > 1:
        raise ValueError("Plan can contain at most one item with status='in_progress'.")

    _validate_plan_dag(items)


def _validate_plan_dag(items: list[PlanItem]) -> None:
    ids: list[str] = []
    seen: set[str] = set()
    for it in items:
        item_id = it.id.strip()
        if not item_id:
            raise ValueError("PlanItem.id must be a non-empty string.")
        if item_id in seen:
            raise ValueError(f"Duplicate PlanItem.id: {item_id!r}")
        seen.add(item_id)
        ids.append(item_id)

    dag: DAG[str] = DAG()
    for item_id in ids:
        dag.add_node(item_id)

    for it in items:
        for dep in it.depends_on:
            dep_id = dep.strip()
            if not dep_id:
                raise ValueError("depends_on entries must be non-empty strings.")
            if dep_id == it.id:
                raise ValueError(f"PlanItem {it.id!r} cannot depend on itself.")
            if dep_id not in seen:
                raise ValueError(f"PlanItem {it.id!r} depends on unknown id {dep_id!r}.")
            # Dependency edge: dep -> node
            dag.add_edge(dep_id, it.id)

    try:
        dag.assert_acyclic()
    except DagError as e:
        raise ValueError("Plan contains a dependency cycle.") from e


def get_ready_item_ids(items: list[PlanItem]) -> list[str]:
    """
    Return node ids that are ready to run:
    - pending
    - all dependencies are completed
    """

    by_id = {it.id: it for it in items}
    ready: list[str] = []
    for it in items:
        if it.status is not StepStatus.PENDING:
            continue
        if all(by_id[dep].status is StepStatus.COMPLETED for dep in it.depends_on):
            ready.append(it.id)
    return ready


@dataclass(frozen=True, slots=True)
class TodoState:
    todo: list[PlanItem]
    explanation: str | None = None
    updated_at: int | None = None


def validate_todo(items: list[PlanItem]) -> None:
    """
    Validate a todo list (linear checklist).

    Todo items:
    - must not have dependencies (depends_on must be empty)
    - can contain at most one in_progress item (for UI consistency)
    """

    in_progress = sum(1 for t in items if t.status is StepStatus.IN_PROGRESS)
    if in_progress > 1:
        raise ValueError("Todo can contain at most one item with status='in_progress'.")

    seen: set[str] = set()
    for it in items:
        item_id = it.id.strip()
        if not item_id:
            raise ValueError("TodoItem.id must be a non-empty string.")
        if item_id in seen:
            raise ValueError(f"Duplicate TodoItem.id: {item_id!r}")
        seen.add(item_id)
        if it.depends_on:
            raise ValueError("Todo items cannot have depends_on.")


class PlanStore:
    """
    Session-scoped plan persistence (Codex-style update_plan + Goose-style persistence).

    Reference storage: SessionStore JSON meta under keys:
    - plan: list[{id,step,status,depends_on}]
    - plan_explanation: str?
    - plan_updated_at: int (ms)
    """

    def __init__(self, *, session_store: SessionStore, session_id: str) -> None:
        self._session_store = session_store
        self._session_id = session_id

    def get(self) -> PlanState:
        meta = self._session_store.get_session(self._session_id)
        raw_plan = meta.get("plan")
        raw_goal = meta.get("plan_goal")
        raw_expl = meta.get("plan_explanation")
        raw_updated = meta.get("plan_updated_at")

        items: list[PlanItem] = []
        if isinstance(raw_plan, list):
            for i, item in enumerate(raw_plan):
                if not isinstance(item, dict):
                    continue
                try:
                    items.append(PlanItem.from_dict(item, default_id=f"step-{i+1}"))
                except ValueError:
                    continue

        goal = raw_goal if isinstance(raw_goal, str) and raw_goal.strip() else None
        explanation = raw_expl if isinstance(raw_expl, str) and raw_expl.strip() else None
        updated_at = raw_updated if isinstance(raw_updated, int) else None
        return PlanState(plan=items, goal=goal, explanation=explanation, updated_at=updated_at)

    def set(self, items: list[PlanItem], *, goal: str | None = None, explanation: str | None = None) -> None:
        validate_plan(items)
        payload: dict[str, Any] = {
            "plan": [t.to_dict() for t in items],
            "plan_updated_at": now_ts_ms(),
            "plan_goal": goal,
            "plan_explanation": explanation,
        }
        self._session_store.update_session(self._session_id, payload)


class TodoStore:
    """
    Session-scoped todo persistence (simple checklist for main-agent-only tasks).

    Reference storage: SessionStore JSON meta under keys:
    - todo: list[{id,step,status}]
    - todo_explanation: str?
    - todo_updated_at: int (ms)
    """

    def __init__(self, *, session_store: SessionStore, session_id: str) -> None:
        self._session_store = session_store
        self._session_id = session_id

    def get(self) -> TodoState:
        meta = self._session_store.get_session(self._session_id)
        raw_todo = meta.get("todo")
        raw_expl = meta.get("todo_explanation")
        raw_updated = meta.get("todo_updated_at")

        items: list[PlanItem] = []
        if isinstance(raw_todo, list):
            for i, item in enumerate(raw_todo):
                if not isinstance(item, dict):
                    continue
                try:
                    parsed = PlanItem.from_dict(item, default_id=f"todo-{i+1}")
                except ValueError:
                    continue
                # Force linear semantics: ignore any stored dependencies.
                if parsed.depends_on:
                    parsed = PlanItem(
                        id=parsed.id,
                        step=parsed.step,
                        status=parsed.status,
                        depends_on=[],
                        metadata=parsed.metadata,
                        error_trace=parsed.error_trace,
                    )
                items.append(parsed)

        explanation = raw_expl if isinstance(raw_expl, str) and raw_expl.strip() else None
        updated_at = raw_updated if isinstance(raw_updated, int) else None
        return TodoState(todo=items, explanation=explanation, updated_at=updated_at)

    def set(self, items: list[PlanItem], *, explanation: str | None = None) -> None:
        validate_todo(items)
        payload: dict[str, Any] = {
            "todo": [t.to_dict() for t in items],
            "todo_updated_at": now_ts_ms(),
            "todo_explanation": explanation,
        }
        self._session_store.update_session(self._session_id, payload)
