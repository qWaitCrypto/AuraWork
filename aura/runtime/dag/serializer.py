from __future__ import annotations

from typing import Any, Iterable

from .graph import DAG
from ..models.taskspec import TaskSpec

SCHEMA_VERSION = "0.1"


class DagSerializationError(ValueError):
    pass


def serialize(dag: DAG[str]) -> dict[str, Any]:
    """
    Serialize a DAG into a JSON-serializable dict.

    Format:
    {
      "schema_version": "0.1",
      "nodes": ["A", "B", ...],
      "edges": [["A","B"], ["B","C"], ...]
    }
    """

    nodes = dag.nodes()
    for n in nodes:
        if not isinstance(n, str):
            raise DagSerializationError("DAG serialization requires string node ids.")

    edges: list[list[str]] = []
    for src in nodes:
        for dst in sorted(dag.successors(src)):
            if not isinstance(dst, str):
                raise DagSerializationError("DAG serialization requires string node ids.")
            edges.append([src, dst])
    edges.sort(key=lambda e: (e[0], e[1]))

    return {
        "schema_version": SCHEMA_VERSION,
        "nodes": list(nodes),
        "edges": edges,
    }


def deserialize(data: dict[str, Any]) -> DAG[str]:
    """
    Deserialize a DAG from a dict produced by `serialize`.
    """

    if not isinstance(data, dict):
        raise DagSerializationError("DAG data must be an object.")

    nodes_raw = data.get("nodes")
    edges_raw = data.get("edges")
    if not isinstance(nodes_raw, list):
        raise DagSerializationError("DAG data is missing 'nodes' (expected list).")
    if not isinstance(edges_raw, list):
        raise DagSerializationError("DAG data is missing 'edges' (expected list).")

    nodes: list[str] = []
    seen: set[str] = set()
    for item in nodes_raw:
        if not isinstance(item, str) or not item:
            raise DagSerializationError("DAG nodes must be non-empty strings.")
        if item in seen:
            raise DagSerializationError(f"Duplicate node id: {item}")
        seen.add(item)
        nodes.append(item)

    g: DAG[str] = DAG()
    for n in nodes:
        g.add_node(n)

    for edge in edges_raw:
        if not isinstance(edge, list) or len(edge) != 2:
            raise DagSerializationError("DAG edges must be 2-item lists.")
        src, dst = edge
        if not isinstance(src, str) or not isinstance(dst, str) or not src or not dst:
            raise DagSerializationError("DAG edges must contain non-empty string node ids.")
        if src not in seen or dst not in seen:
            raise DagSerializationError(f"Edge references unknown node(s): {src!r} -> {dst!r}")
        g.add_edge(src, dst)

    return g


def serialize_with_taskspecs(*, dag: DAG[str], task_specs: dict[str, TaskSpec]) -> dict[str, Any]:
    """
    Serialize a DAG plus its TaskSpec payloads (by node id).

    Adds:
    - task_specs: list[TaskSpec dict] in DAG node order
    """

    base = serialize(dag)

    nodes = base["nodes"]
    assert isinstance(nodes, list)
    specs_out: list[dict[str, Any]] = []
    for node_id in nodes:
        if not isinstance(node_id, str):
            raise DagSerializationError("DAG serialization requires string node ids.")
        spec = task_specs.get(node_id)
        if spec is None:
            raise DagSerializationError(f"Missing TaskSpec for node id: {node_id}")
        specs_out.append(spec.model_dump(mode="json"))

    base["task_specs"] = specs_out
    return base


def deserialize_with_taskspecs(data: dict[str, Any]) -> tuple[DAG[str], dict[str, TaskSpec]]:
    """
    Deserialize a DAG plus TaskSpec payloads from a dict produced by `serialize_with_taskspecs`.
    """

    g = deserialize(data)
    raw = data.get("task_specs")
    if not isinstance(raw, list):
        raise DagSerializationError("DAG data is missing 'task_specs' (expected list).")

    specs: dict[str, TaskSpec] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise DagSerializationError("task_specs items must be objects.")
        spec = TaskSpec.model_validate(item)
        specs[spec.id] = spec

    # Ensure 1:1 mapping with nodes.
    node_ids = set(g.nodes())
    if set(specs) != node_ids:
        missing = sorted(node_ids - set(specs))
        extra = sorted(set(specs) - node_ids)
        raise DagSerializationError(f"TaskSpec ids must match DAG nodes (missing={missing}, extra={extra})")

    return g, specs


def assert_acyclic_after_deserialize(data: dict[str, Any]) -> DAG[str]:
    g = deserialize(data)
    g.assert_acyclic()
    return g

