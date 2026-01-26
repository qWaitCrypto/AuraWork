from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, Hashable, Iterable, TypeVar

NodeId = TypeVar("NodeId", bound=Hashable)


class DagError(RuntimeError):
    pass


@dataclass(slots=True)
class DAG(Generic[NodeId]):
    """
    Minimal directed graph helper used by the scheduler (design doc §6).

    - Nodes are identified by a hashable key (typically str).
    - Edges are stored as adjacency sets (deduplicated).
    - In-degree is maintained incrementally for O(1) queries.
    """

    _succ: dict[NodeId, set[NodeId]] = field(default_factory=dict)
    _in_degree: dict[NodeId, int] = field(default_factory=dict)
    _insertion_order: list[NodeId] = field(default_factory=list)

    def nodes(self) -> list[NodeId]:
        return list(self._insertion_order)

    def has_node(self, node: NodeId) -> bool:
        return node in self._in_degree

    def add_node(self, node: NodeId) -> None:
        if self.has_node(node):
            return
        self._succ[node] = set()
        self._in_degree[node] = 0
        self._insertion_order.append(node)

    def add_edge(self, src: NodeId, dst: NodeId) -> None:
        """
        Add a directed edge src -> dst.

        Nodes are auto-created if missing. Duplicate edges are ignored.
        """

        self.add_node(src)
        self.add_node(dst)
        if dst in self._succ[src]:
            return
        self._succ[src].add(dst)
        self._in_degree[dst] = int(self._in_degree.get(dst, 0)) + 1

    def successors(self, node: NodeId) -> set[NodeId]:
        if not self.has_node(node):
            raise KeyError(f"Unknown node: {node!r}")
        return set(self._succ.get(node, set()))

    def get_in_degree(self, node: NodeId) -> int:
        if not self.has_node(node):
            raise KeyError(f"Unknown node: {node!r}")
        return int(self._in_degree[node])

    def get_ready_nodes(self) -> list[NodeId]:
        """
        Return nodes with in-degree == 0.

        Order is stable (insertion order) to make scheduling deterministic.
        """

        out: list[NodeId] = []
        for n in self._insertion_order:
            if self._in_degree.get(n, 0) == 0:
                out.append(n)
        return out

    def has_cycle(self) -> bool:
        """
        Return True if the graph contains a directed cycle.

        Uses Kahn's algorithm with an in-degree copy to avoid mutating the graph.
        """

        # Fast path: empty graph.
        if not self._insertion_order:
            return False

        indeg: dict[NodeId, int] = {n: int(self._in_degree.get(n, 0)) for n in self._insertion_order}
        queue: list[NodeId] = [n for n in self._insertion_order if indeg.get(n, 0) == 0]
        visited = 0

        while queue:
            node = queue.pop()
            visited += 1
            for succ in self._succ.get(node, set()):
                if succ not in indeg:
                    # Defensive: should not happen because add_edge auto-adds nodes.
                    continue
                indeg[succ] -= 1
                if indeg[succ] == 0:
                    queue.append(succ)

        return visited != len(indeg)

    def assert_acyclic(self) -> None:
        if self.has_cycle():
            raise DagError("Graph contains a cycle.")

    @classmethod
    def from_edges(cls, edges: Iterable[tuple[NodeId, NodeId]]) -> "DAG[NodeId]":
        g: DAG[NodeId] = cls()
        for src, dst in edges:
            g.add_edge(src, dst)
        return g

