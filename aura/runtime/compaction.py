from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .context_mgmt import (
    approx_tokens_from_text,
    history_budget_for_limit,
    select_recent_messages_to_fit_budget,
    strip_tool_output_for_compaction,
    truncate_text_to_budget,
)
from .llm.types import CanonicalMessage, CanonicalMessageRole, CanonicalRequest, ModelProfile


DEFAULT_HISTORY_BUDGET_RATIO = 0.20
DEFAULT_HISTORY_BUDGET_FALLBACK_TOKENS = 8000
DEFAULT_TOOL_OUTPUT_BUDGET_TOKENS = 400
DEFAULT_AUTO_COMPACT_THRESHOLD_RATIO = 0.8


@dataclass(frozen=True)
class ContextManagementSettings:
    auto_compact_threshold_ratio: float | None
    history_budget_ratio: float
    history_budget_fallback_tokens: int
    tool_output_budget_tokens: int


@dataclass(frozen=True)
class CompactionResult:
    memory_summary: str
    retained_history: list[CanonicalMessage]
    history_budget_tokens: int
    summary_estimated_tokens: int


def load_compact_prompt_text() -> str:
    path = Path(__file__).resolve().parent / "prompts" / "compact_prompt.md"
    return path.read_text(encoding="utf-8", errors="replace")


def settings_for_profile(profile: ModelProfile) -> ContextManagementSettings:
    cm = getattr(profile, "context_management", None)
    auto_ratio = getattr(cm, "auto_compact_threshold_ratio", None)
    history_ratio = getattr(cm, "history_budget_ratio", None)
    fallback_tokens = getattr(cm, "history_budget_fallback_tokens", None)
    tool_budget = getattr(cm, "tool_output_budget_tokens", None)

    if history_ratio is None:
        history_ratio = DEFAULT_HISTORY_BUDGET_RATIO
    if fallback_tokens is None:
        fallback_tokens = DEFAULT_HISTORY_BUDGET_FALLBACK_TOKENS
    if tool_budget is None:
        tool_budget = DEFAULT_TOOL_OUTPUT_BUDGET_TOKENS

    return ContextManagementSettings(
        auto_compact_threshold_ratio=auto_ratio,
        history_budget_ratio=float(history_ratio),
        history_budget_fallback_tokens=int(fallback_tokens),
        tool_output_budget_tokens=int(tool_budget),
    )


def is_auto_compact_enabled(ratio: float | None) -> bool:
    if ratio is None:
        return False
    try:
        val = float(ratio)
    except Exception:
        return False
    return 0.0 < val < 1.0


def should_auto_compact(
    *,
    estimated_input_tokens: int,
    context_limit_tokens: int | None,
    threshold_ratio: float | None,
) -> bool:
    if not is_auto_compact_enabled(threshold_ratio):
        return False
    if not isinstance(context_limit_tokens, int) or context_limit_tokens <= 0:
        return False
    threshold = float(threshold_ratio) * float(context_limit_tokens)
    return float(estimated_input_tokens) > threshold


def build_compaction_request(
    *,
    history: list[CanonicalMessage],
    memory_summary: str | None,
    prompt_text: str,
    tool_output_budget_tokens: int,
) -> CanonicalRequest:
    stripped = [
        strip_tool_output_for_compaction(m, tool_output_budget_tokens=tool_output_budget_tokens) for m in history
    ]

    messages: list[CanonicalMessage] = []
    if isinstance(memory_summary, str) and memory_summary.strip():
        messages.append(
            CanonicalMessage(
                role=CanonicalMessageRole.USER,
                content="Existing durable session summary (from previous compaction):\n\n"
                + memory_summary.strip(),
            )
        )
    messages.extend(stripped)
    messages.append(CanonicalMessage(role=CanonicalMessageRole.USER, content=prompt_text))
    return CanonicalRequest(system=None, messages=messages, tools=[], params={})


def apply_compaction_retention(
    *,
    history: list[CanonicalMessage],
    memory_summary: str,
    context_limit_tokens: int | None,
    history_budget_ratio: float,
    history_budget_fallback_tokens: int,
) -> CompactionResult:
    budget = history_budget_for_limit(
        context_limit_tokens,
        ratio=history_budget_ratio,
        fallback_tokens=history_budget_fallback_tokens,
    )
    if approx_tokens_from_text(memory_summary) > budget.budget_tokens:
        memory_summary = truncate_text_to_budget(memory_summary, budget_tokens=budget.budget_tokens)
    summary_tokens = approx_tokens_from_text(memory_summary)
    remaining = max(0, int(budget.budget_tokens) - int(summary_tokens))
    newest_first = list(reversed(history))
    kept_newest_first = select_recent_messages_to_fit_budget(
        messages_newest_first=newest_first,
        budget_tokens=remaining,
    )
    retained = list(reversed(kept_newest_first))
    return CompactionResult(
        memory_summary=memory_summary,
        retained_history=retained,
        history_budget_tokens=int(budget.budget_tokens),
        summary_estimated_tokens=int(summary_tokens),
    )
