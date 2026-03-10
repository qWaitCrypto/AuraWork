from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import Any

from .dag.graph import DAG
from .dag.scheduler import Scheduler
from .plan import PlanItem, PlanState, PlanStore, StepStatus


@dataclass
class DAGPlanRunner:
    """
    DAG plan runner: manages PlanStore → Scheduler conversion and state synchronization.

    Responsibilities:
    - Build the DAG from PlanStore items and initialize the Scheduler
    - Track completed/running nodes and keep PlanStore statuses in sync
    - Provide an interface to query dispatchable (ready) nodes
    """

    plan_store: PlanStore
    max_parallel: int = 3

    _scheduler: Scheduler[str] | None = field(default=None, init=False, repr=False)
    _last_plan_hash: str | None = field(default=None, init=False, repr=False)

    def refresh_from_store(self) -> None:
        """
        Rebuild the DAG and Scheduler from PlanStore.

        When to call:
        - The first call to get_dispatchable_nodes()
        - After PlanStore is modified externally (e.g., the main agent accepts a proposal and calls update_plan)
        """

        plan_state = self.plan_store.get()
        current_hash = self._compute_plan_hash(plan_state)
        if self._scheduler is None or current_hash != self._last_plan_hash:
            dag = self._build_dag_from_items(plan_state.plan)
            self._scheduler = Scheduler(dag=dag, max_parallel=self.max_parallel)
            self._last_plan_hash = current_hash

        self._sync_scheduler_state(plan_state)

    def get_dispatchable_nodes(self) -> list[PlanItem]:
        """
        Return the list of nodes that are ready to be dispatched.

        Returns:
            list[PlanItem]: nodes that are ready and not already running
        """

        self.refresh_from_store()
        if self._scheduler is None:
            return []

        next_node_ids = self._scheduler.get_next_nodes()
        if not next_node_ids:
            return []

        plan_state = self.plan_store.get()
        items_by_id = {item.id: item for item in plan_state.plan}
        dispatchable: list[PlanItem] = []
        for node_id in next_node_ids:
            item = items_by_id.get(node_id)
            if item is None:
                continue
            if item.status is StepStatus.PENDING:
                dispatchable.append(item)

        return dispatchable

    def mark_completed(self, node_id: str, *, node_result: dict[str, Any] | None = None) -> None:
        """
        Mark a node as completed and update both the Scheduler and PlanStore.
        """

        if self._scheduler is None:
            self.refresh_from_store()
        if self._scheduler is None:
            raise RuntimeError("Scheduler not initialized.")

        self._scheduler.mark_completed(node_id)

        plan_state = self.plan_store.get()
        updated_items: list[PlanItem] = []
        found = False
        for item in plan_state.plan:
            if item.id == node_id:
                found = True
                new_meta = dict(item.metadata)
                if node_result is not None:
                    new_meta["node_result"] = node_result
                if item.status is StepStatus.COMPLETED and new_meta == item.metadata:
                    updated_items.append(item)
                else:
                    updated_items.append(replace(item, status=StepStatus.COMPLETED, metadata=new_meta))
            else:
                updated_items.append(item)
        if not found:
            raise KeyError(f"PlanItem not found: {node_id}")

        self.plan_store.set(updated_items, goal=plan_state.goal, explanation=plan_state.explanation)
        self._last_plan_hash = self._compute_plan_hash(
            PlanState(plan=updated_items, goal=plan_state.goal, explanation=plan_state.explanation, updated_at=plan_state.updated_at)
        )

    def mark_failed(self, node_id: str, error: str, *, node_result: dict[str, Any] | None = None) -> None:
        """Mark a node as failed while preserving error information."""
        if self._scheduler is None:
            self.refresh_from_store()
        if self._scheduler is None:
            raise RuntimeError("Scheduler not initialized.")

        self._scheduler.mark_completed(node_id)  # Remove from scheduler

        plan_state = self.plan_store.get()
        updated_items: list[PlanItem] = []
        found = False
        for item in plan_state.plan:
            if item.id == node_id:
                found = True
                new_trace = list(item.error_trace) + [{"error": error}]
                new_meta = dict(item.metadata)
                if node_result is not None:
                    new_meta["node_result"] = node_result
                updated_items.append(replace(item, status=StepStatus.FAILED, error_trace=new_trace, metadata=new_meta))
            else:
                updated_items.append(item)
        if not found:
            raise KeyError(f"PlanItem not found: {node_id}")

        self.plan_store.set(updated_items, goal=plan_state.goal, explanation=plan_state.explanation)
        self._last_plan_hash = self._compute_plan_hash(
            PlanState(plan=updated_items, goal=plan_state.goal, explanation=plan_state.explanation, updated_at=plan_state.updated_at)
        )

    def release_running(self, node_id: str) -> bool:
        """
        Release a node from scheduler running-state without marking it completed/failed.

        This is used when a dispatched node pauses for approval: the node should be
        redispatchable after approval instead of staying stuck in `_running` forever.
        """
        if self._scheduler is None:
            self.refresh_from_store()
        if self._scheduler is None:
            raise RuntimeError("Scheduler not initialized.")

        if node_id not in self._scheduler._running:
            return False

        self._scheduler._running.discard(node_id)
        if (
            node_id not in self._scheduler._completed
            and self._scheduler._in_degree.get(node_id, 0) == 0
            and node_id not in self._scheduler._ready
        ):
            self._scheduler._ready.appendleft(node_id)
        return True

    def get_goal(self) -> str | None:
        """Get the global goal of the current plan."""
        return self.plan_store.get().goal

    def get_progress_summary(self) -> str:
        """Generate a progress summary (injected into subagent context)."""
        items = self.plan_store.get().plan
        completed = [it for it in items if it.status is StepStatus.COMPLETED]
        failed = [it for it in items if it.status is StepStatus.FAILED]
        lines = [f"✅ Completed {len(completed)}/{len(items)}"]
        if completed:
            lines.append("Recently completed: " + ", ".join(it.id for it in completed[-3:]))
        if failed:
            lines.append(f"❌ Failed {len(failed)}: " + ", ".join(it.id for it in failed))
        return "\n".join(lines)

    def is_all_done(self) -> bool:
        """
        Check whether the DAG is fully completed (completed or failed).
        """

        plan_state = self.plan_store.get()
        return all(item.status in (StepStatus.COMPLETED, StepStatus.FAILED) for item in plan_state.plan)

    def _build_dag_from_items(self, items: list[PlanItem]) -> DAG[str]:
        dag: DAG[str] = DAG()
        for item in items:
            dag.add_node(item.id)
        for item in items:
            for dep_id in item.depends_on:
                dag.add_edge(dep_id, item.id)
        return dag

    def _compute_plan_hash(self, plan_state: PlanState) -> str:
        node_ids = sorted(item.id for item in plan_state.plan)
        edges: list[str] = []
        for item in plan_state.plan:
            for dep in item.depends_on:
                edges.append(f"{dep}->{item.id}")
        edges.sort()
        signature = "|".join(node_ids) + "||" + "|".join(edges)
        return sha256(signature.encode("utf-8")).hexdigest()

    def _sync_scheduler_state(self, plan_state: PlanState) -> None:
        if self._scheduler is None:
            return

        dag = self._scheduler.dag

        # Both COMPLETED and FAILED nodes are "done" for scheduling — neither should
        # re-enter the ready/running queue.  Only COMPLETED nodes unblock successors
        # (a failed node keeps its dependents blocked for the main agent to handle).
        completed = {item.id for item in plan_state.plan if item.status is StepStatus.COMPLETED}
        failed = {item.id for item in plan_state.plan if item.status is StepStatus.FAILED}
        done = completed | failed

        running = {item.id for item in plan_state.plan if item.status is StepStatus.IN_PROGRESS}
        running |= set(self._scheduler._running)
        if done & running:
            running = running - done

        in_degree = {node: 0 for node in dag.nodes()}
        for src in dag.nodes():
            for dst in dag.successors(src):
                if src in completed:
                    continue
                in_degree[dst] = in_degree.get(dst, 0) + 1

        ready = [
            node
            for node in dag.nodes()
            if in_degree.get(node, 0) == 0 and node not in done and node not in running
        ]

        # Sync scheduler internal state with PlanStore statuses.
        # _completed here means "scheduler should not dispatch this node again".
        self._scheduler._completed = set(done)
        self._scheduler._running = set(running)
        self._scheduler._in_degree = in_degree
        self._scheduler._ready = deque(ready)
