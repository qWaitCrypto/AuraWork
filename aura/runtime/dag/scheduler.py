from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Generic, Hashable, TypeVar

from .graph import DAG, DagError

NodeId = TypeVar("NodeId", bound=Hashable)


class SchedulerError(RuntimeError):
    pass


@dataclass(slots=True)
class Scheduler(Generic[NodeId]):
    """
    In-degree scheduler core (design doc §6.1).

    Maintains:
    - in-degree map (mutable copy)
    - ready queue (nodes with in-degree == 0 and not running/completed)
    - running set (dispatched but not completed)
    - completed set
    """

    dag: DAG[NodeId]
    max_parallel: int = 3

    _order_index: dict[NodeId, int] = field(init=False)
    _in_degree: dict[NodeId, int] = field(init=False)
    _ready: Deque[NodeId] = field(init=False)
    _running: set[NodeId] = field(default_factory=set, init=False)
    _completed: set[NodeId] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        if self.max_parallel < 1:
            raise ValueError("max_parallel must be >= 1.")

        self.dag.assert_acyclic()

        nodes = self.dag.nodes()
        self._order_index = {n: i for i, n in enumerate(nodes)}
        self._in_degree = {n: int(self.dag.get_in_degree(n)) for n in nodes}
        self._ready = deque([n for n in nodes if self._in_degree.get(n, 0) == 0])

    def get_next_nodes(self) -> list[NodeId]:
        """
        Pop up to (max_parallel - running) nodes from the ready queue and mark them running.
        """

        capacity = self.max_parallel - len(self._running)
        if capacity <= 0:
            return []

        out: list[NodeId] = []
        while capacity > 0 and self._ready:
            node = self._ready.popleft()
            if node in self._completed or node in self._running:
                continue
            out.append(node)
            self._running.add(node)
            capacity -= 1
        return out

    def mark_completed(self, node: NodeId) -> None:
        """
        Mark a running node as completed and update successors in-degrees.
        """

        if node not in self._in_degree:
            raise KeyError(f"Unknown node: {node!r}")
        if node in self._completed:
            raise SchedulerError(f"Node already completed: {node!r}")
        if node not in self._running:
            raise SchedulerError(f"Node is not running: {node!r}")

        self._running.remove(node)
        self._completed.add(node)

        # Update in-degree for successors; enqueue those that become ready.
        successors = list(self.dag.successors(node))
        successors.sort(key=lambda n: self._order_index.get(n, 1_000_000_000))
        for succ in successors:
            if succ in self._completed:
                continue
            cur = int(self._in_degree.get(succ, 0))
            nxt = cur - 1
            if nxt < 0:
                raise DagError(f"Negative in-degree for node {succ!r}; graph invariants violated.")
            self._in_degree[succ] = nxt
            if nxt == 0 and succ not in self._running:
                self._ready.append(succ)

    def is_all_done(self) -> bool:
        return len(self._completed) == len(self._in_degree)

    def running_nodes(self) -> set[NodeId]:
        return set(self._running)

    def completed_nodes(self) -> set[NodeId]:
        return set(self._completed)

