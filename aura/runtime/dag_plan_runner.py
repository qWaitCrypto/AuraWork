from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import Any

from .dag.graph import DAG
from .dag.scheduler import Scheduler
from .plan import PlanItem, PlanState, PlanStore, StepStatus


@dataclass
class DAGPlanRunner:
    """
    DAG plan runner with a stateless scheduler.

    Holds:
    - `_dag`: topology cache (rebuild only when plan structure changes)
    - `_dispatched`: in-flight dispatched nodes (runtime-memory only)
    """

    plan_store: PlanStore
    max_parallel: int = 3

    _dag: DAG[str] | None = field(default=None, init=False, repr=False)
    _last_plan_hash: str | None = field(default=None, init=False, repr=False)
    _dispatched: set[str] = field(default_factory=set, init=False, repr=False)

    def get_dispatchable_nodes(self) -> list[PlanItem]:
        plan_state = self.plan_store.get()
        self._maybe_rebuild_dag(plan_state)
        if self._dag is None:
            return []

        scheduler = Scheduler(dag=self._dag, max_parallel=self.max_parallel)
        ready_node_ids = scheduler.get_ready_nodes(plan_state, frozenset(self._dispatched))
        if not ready_node_ids:
            return []

        items_by_id = {item.id: item for item in plan_state.plan}
        out: list[PlanItem] = []
        for node_id in ready_node_ids:
            item = items_by_id.get(node_id)
            if item is None:
                continue
            if item.status is StepStatus.PENDING:
                out.append(item)
        if out:
            self._dispatched.update(item.id for item in out)
        return out

    def mark_completed(self, node_id: str, *, node_result: dict[str, Any] | None = None) -> None:
        self._dispatched.discard(node_id)
        self._update_plan_item(node_id, status=StepStatus.COMPLETED, node_result=node_result)

    def mark_failed(self, node_id: str, error: str, *, node_result: dict[str, Any] | None = None) -> None:
        """Mark a node as failed while preserving error information."""
        self._dispatched.discard(node_id)
        self._update_plan_item(node_id, status=StepStatus.FAILED, error=error, node_result=node_result)

    def release_dispatched(self, node_id: str) -> bool:
        """
        Release a dispatched node without marking it completed/failed.

        Used when a node pauses for approval and should re-enter ready pool.
        """
        if node_id not in self._dispatched:
            return False
        self._dispatched.discard(node_id)
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
        plan_state = self.plan_store.get()
        self._maybe_rebuild_dag(plan_state)
        if self._dag is None:
            return True
        return Scheduler(dag=self._dag).is_all_done(plan_state)

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

    def _update_plan_item(
        self,
        node_id: str,
        *,
        status: StepStatus,
        error: str | None = None,
        node_result: dict[str, Any] | None = None,
    ) -> None:
        plan_state = self.plan_store.get()
        updated_items: list[PlanItem] = []
        found = False
        for item in plan_state.plan:
            if item.id != node_id:
                updated_items.append(item)
                continue

            found = True
            new_meta = dict(item.metadata)
            if node_result is not None:
                new_meta["node_result"] = node_result

            if status is StepStatus.COMPLETED:
                if item.status is StepStatus.COMPLETED and new_meta == item.metadata:
                    updated_items.append(item)
                else:
                    updated_items.append(replace(item, status=StepStatus.COMPLETED, metadata=new_meta))
                continue

            if status is StepStatus.FAILED:
                new_trace = list(item.error_trace)
                if isinstance(error, str) and error:
                    new_trace.append({"error": error})
                updated_items.append(replace(item, status=StepStatus.FAILED, error_trace=new_trace, metadata=new_meta))
                continue

            updated_items.append(replace(item, status=status, metadata=new_meta))

        if not found:
            raise KeyError(f"PlanItem not found: {node_id}")

        self.plan_store.set(updated_items, goal=plan_state.goal, explanation=plan_state.explanation)
        self._last_plan_hash = self._compute_plan_hash(self.plan_store.get())

    def _maybe_rebuild_dag(self, plan_state: PlanState) -> None:
        current_hash = self._compute_plan_hash(plan_state)
        if self._dag is not None and self._last_plan_hash == current_hash:
            return

        self._dag = self._build_dag_from_items(plan_state.plan)
        self._last_plan_hash = current_hash

        # Plan structure changed: keep still-valid in-flight nodes so they are
        # not redispatched during topology updates.
        statuses = {item.id: item.status for item in plan_state.plan}
        valid_statuses = {StepStatus.PENDING, StepStatus.IN_PROGRESS}
        self._dispatched = {
            node_id
            for node_id in self._dispatched
            if statuses.get(node_id) in valid_statuses
        }
