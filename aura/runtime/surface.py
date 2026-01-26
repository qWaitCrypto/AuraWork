"""Surface Protocol - multi-surface abstraction (default: unused).

This module defines the interface boundary for future surfaces (Web/Plugin/Cloud).
The current CLI subscribes to `EventBus` directly and does not need to implement this.
"""

from __future__ import annotations

from typing import Any, Protocol

from .engine import RunResult, ToolDecision
from .protocol import Event


class Surface(Protocol):
    """Port-agnostic surface interface."""

    async def on_event(self, event: Event) -> None:
        """Handle events emitted by the runtime (ordered stream)."""

    async def get_input(self, prompt: str = "") -> str:
        """Get user input."""

    async def request_approval(
        self,
        *,
        run: RunResult,
    ) -> list[ToolDecision]:
        """Request approval decisions for a paused run."""

    def supports_streaming(self) -> bool:
        """Whether the surface supports streaming incremental output."""

        return True

