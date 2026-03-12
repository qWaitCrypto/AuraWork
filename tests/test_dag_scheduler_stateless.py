from __future__ import annotations

from aura.runtime.dag.graph import DAG
from aura.runtime.dag.scheduler import Scheduler
from aura.runtime.plan import PlanItem, PlanState, StepStatus


def _item(*, item_id: str, status: StepStatus, depends_on: list[str] | None = None) -> PlanItem:
    return PlanItem(
        id=item_id,
        step=item_id,
        status=status,
        depends_on=list(depends_on or []),
    )


def test_failed_dependency_does_not_unlock_downstream() -> None:
    dag: DAG[str] = DAG()
    dag.add_edge("a", "b")

    plan_state = PlanState(
        plan=[
            _item(item_id="a", status=StepStatus.FAILED),
            _item(item_id="b", status=StepStatus.PENDING, depends_on=["a"]),
        ]
    )

    scheduler = Scheduler(dag=dag, max_parallel=2)
    ready = scheduler.get_ready_nodes(plan_state, frozenset({"a"}))
    assert ready == []


def test_extra_running_reduces_capacity_and_excludes_running_nodes() -> None:
    dag: DAG[str] = DAG()
    dag.add_node("a")
    dag.add_node("b")

    plan_state = PlanState(
        plan=[
            _item(item_id="a", status=StepStatus.PENDING),
            _item(item_id="b", status=StepStatus.PENDING),
        ]
    )

    scheduler = Scheduler(dag=dag, max_parallel=2)
    ready = scheduler.get_ready_nodes(plan_state, frozenset({"a"}))
    assert ready == ["b"]


def test_is_all_done_true_when_only_failed_blocked_pending_remain() -> None:
    dag: DAG[str] = DAG()
    dag.add_edge("a", "b")
    dag.add_edge("b", "c")

    plan_state = PlanState(
        plan=[
            _item(item_id="a", status=StepStatus.FAILED),
            _item(item_id="b", status=StepStatus.PENDING, depends_on=["a"]),
            _item(item_id="c", status=StepStatus.PENDING, depends_on=["b"]),
        ]
    )

    scheduler = Scheduler(dag=dag, max_parallel=2)
    assert scheduler.is_all_done(plan_state) is True


def test_is_all_done_false_when_pending_still_runnable() -> None:
    dag: DAG[str] = DAG()
    dag.add_edge("a", "b")

    plan_state = PlanState(
        plan=[
            _item(item_id="a", status=StepStatus.COMPLETED),
            _item(item_id="b", status=StepStatus.PENDING, depends_on=["a"]),
        ]
    )

    scheduler = Scheduler(dag=dag, max_parallel=2)
    assert scheduler.is_all_done(plan_state) is False
