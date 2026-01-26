from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..llm.types import ToolSpec


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]

    def execute(self, *, args: dict[str, Any], project_root) -> Any: ...


@dataclass(slots=True)
class ToolRegistry:
    _tools: dict[str, Tool]

    def __init__(self) -> None:
        self._tools = {}

    def register(self, tool: Tool) -> None:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name:
            raise ValueError("Tool.name must be a non-empty string.")
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_specs(self) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        for name in sorted(self._tools):
            tool = self._tools[name]
            out.append(
                ToolSpec(
                    name=tool.name,
                    description=tool.description,
                    input_schema=dict(tool.input_schema),
                )
            )
        return out

