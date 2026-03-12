from __future__ import annotations

from dataclasses import dataclass

from aura.runtime.dag_plan_runner import DAGPlanRunner
from aura.runtime.plan import PlanItem, PlanState, StepStatus


@dataclass
class _StubPlanStore:
    state: PlanState

    def get(self) -> PlanState:
        return self.state

    def set(self, items, *, goal=None, explanation=None) -> None:
        self.state = PlanState(plan=list(items), goal=goal, explanation=explanation)


def _item(*, item_id: str, status: StepStatus, depends_on: list[str] | None = None) -> PlanItem:
    return PlanItem(
        id=item_id,
        step=item_id,
        status=status,
        depends_on=list(depends_on or []),
    )


def test_topology_rebuild_keeps_inflight_dispatched_nodes() -> None:
    store = _StubPlanStore(
        state=PlanState(
            plan=[
                _item(item_id="a", status=StepStatus.PENDING),
                _item(item_id="b", status=StepStatus.PENDING, depends_on=["a"]),
            ]
        )
    )
    runner = DAGPlanRunner(plan_store=store, max_parallel=2)

    first = runner.get_dispatchable_nodes()
    assert [item.id for item in first] == ["a"]

    # Topology changes while "a" is still in-flight and still PENDING in store.
    store.state = PlanState(
        plan=[
            _item(item_id="a", status=StepStatus.PENDING),
            _item(item_id="b", status=StepStatus.PENDING, depends_on=["a"]),
            _item(item_id="c", status=StepStatus.PENDING),
        ]
    )

    second = runner.get_dispatchable_nodes()
    assert [item.id for item in second] == ["c"]
