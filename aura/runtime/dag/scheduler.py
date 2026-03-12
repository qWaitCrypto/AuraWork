from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Hashable, TypeVar

from ..plan import PlanState, StepStatus
from .graph import DAG

NodeId = TypeVar("NodeId", bound=Hashable)


@dataclass(slots=True)
class Scheduler(Generic[NodeId]):
    """
    Stateless DAG scheduler.

    Computes ready nodes from current PlanState on each call.
    """

    dag: DAG[NodeId]
    max_parallel: int = 3

    def get_ready_nodes(
        self,
        plan_state: PlanState,
        extra_running: frozenset[str] = frozenset(),
    ) -> list[NodeId]:
        if self.max_parallel < 1:
            raise ValueError("max_parallel must be >= 1.")
        self.dag.assert_acyclic()

        nodes = self.dag.nodes()
        node_set = set(nodes)

        completed = {it.id for it in plan_state.plan if it.status is StepStatus.COMPLETED and it.id in node_set}
        done = {
            it.id
            for it in plan_state.plan
            if it.id in node_set and it.status in (StepStatus.COMPLETED, StepStatus.FAILED)
        }
        ps_running = {it.id for it in plan_state.plan if it.status is StepStatus.IN_PROGRESS and it.id in node_set}
        ext_running = {nid for nid in extra_running if nid in node_set}
        all_running = ps_running | ext_running
        excluded = done | all_running

        in_degree: dict[NodeId, int] = {n: 0 for n in nodes}
        for src in nodes:
            for dst in self.dag.successors(src):
                if src not in completed:
                    in_degree[dst] = int(in_degree.get(dst, 0)) + 1

        ready = [n for n in nodes if int(in_degree.get(n, 0)) == 0 and n not in excluded]
        capacity = self.max_parallel - len(all_running)
        if capacity <= 0:
            return []
        return ready[:capacity]

    def is_all_done(self, plan_state: PlanState) -> bool:
        by_id = {it.id: it for it in plan_state.plan}
        done = {it.id for it in plan_state.plan if it.status in (StepStatus.COMPLETED, StepStatus.FAILED)}
        blocked_by_failure: set[str] = set()

        # Pending nodes downstream of FAILED nodes can never run. Treat them as terminal.
        changed = True
        while changed:
            changed = False
            for item in plan_state.plan:
                if item.id in done or item.id in blocked_by_failure:
                    continue
                if item.status is not StepStatus.PENDING:
                    continue

                deps = [dep for dep in item.depends_on if dep in by_id]
                if any(dep in blocked_by_failure for dep in deps):
                    blocked_by_failure.add(item.id)
                    changed = True
                    continue
                if any(by_id[dep].status is StepStatus.FAILED for dep in deps):
                    blocked_by_failure.add(item.id)
                    changed = True

        return all(it.id in done or it.id in blocked_by_failure for it in plan_state.plan)
