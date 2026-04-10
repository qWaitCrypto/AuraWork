from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aura.runtime.plan import PlanStore
from aura.runtime.tools.plan import UpdatePlanTool


@dataclass
class _StubSessionStore:
    session_id: str
    meta: dict[str, Any]

    def get_session(self, session_id: str) -> dict[str, Any]:
        assert session_id == self.session_id
        return dict(self.meta)

    def update_session(self, session_id: str, patch: dict[str, Any]) -> None:
        assert session_id == self.session_id
        self.meta.update(patch)


def _work_spec(goal: str) -> dict[str, Any]:
    return {
        "goal": goal,
        "expected_outputs": [{"type": "report", "format": "json"}],
        "resource_scope": {"workspace_roots": ["."], "file_type_allowlist": ["json"], "domain_allowlist": []},
    }


def test_update_plan_preserves_node_result_for_completed_nodes() -> None:
    session_id = "sess_test"
    initial_plan = [
        {
            "id": "collect",
            "step": "collect",
            "status": "completed",
            "depends_on": [],
            "metadata": {
                "preset": "browser_worker",
                "work_spec": _work_spec("collect"),
                "node_result": {"report": {"status": "completed", "items": [1]}},
            },
        }
    ]
    session_store = _StubSessionStore(session_id=session_id, meta={"session_id": session_id, "plan": initial_plan})
    store = PlanStore(session_store=session_store, session_id=session_id)
    tool = UpdatePlanTool(store=store)

    result = tool.execute(
        args={
            "goal": "g",
            "plan": [
                {
                    "id": "collect",
                    "step": "collect",
                    "status": "completed",
                    "depends_on": [],
                    "metadata": {
                        "preset": "browser_worker",
                        "work_spec": _work_spec("collect"),
                    },
                }
            ],
        },
        project_root=".",
    )

    assert result["ok"] is True
    plan_state = store.get()
    item = plan_state.plan[0]
    assert item.metadata.get("node_result") == {"report": {"status": "completed", "items": [1]}}


def test_update_plan_clears_node_result_when_node_reopened() -> None:
    session_id = "sess_test"
    initial_plan = [
        {
            "id": "collect",
            "step": "collect",
            "status": "completed",
            "depends_on": [],
            "metadata": {
                "preset": "browser_worker",
                "work_spec": _work_spec("collect"),
                "node_result": {"report": {"status": "completed"}},
            },
        }
    ]
    session_store = _StubSessionStore(session_id=session_id, meta={"session_id": session_id, "plan": initial_plan})
    store = PlanStore(session_store=session_store, session_id=session_id)
    tool = UpdatePlanTool(store=store)

    result = tool.execute(
        args={
            "goal": "g",
            "plan": [
                {
                    "id": "collect",
                    "step": "collect rerun",
                    "status": "pending",
                    "depends_on": [],
                    "metadata": {
                        "preset": "browser_worker",
                        "work_spec": _work_spec("collect rerun"),
                    },
                }
            ],
        },
        project_root=".",
    )

    assert result["ok"] is True
    plan_state = store.get()
    item = plan_state.plan[0]
    assert "node_result" not in item.metadata
