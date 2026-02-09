# Aura Event Schema v0.2 (Runtime Contract)

This file defines the normalized runtime event contract currently enforced by Aura core.

## Scope

This contract focuses on two high-impact event families:

- `llm_response_completed`
- `tool_call_end`

## Global invariants

- `schema_version` defaults to `"0.2"` when missing.
- `sequence` is required for stable replay ordering.
  - In replay/read paths, missing or non-monotonic sequence values are repaired to monotonic values.
- `payload.source` is always present (`"engine" | "subagent" | "unknown"`), defaulting to `"unknown"`.

## `llm_response_completed`

Canonical payload fields:

- `profile_id: string`
- `provider_kind: string`
- `model: string`
- `output_ref: ArtifactRef` (assistant final output artifact)
- `final_text: string` (canonical final assistant output)
- `thinking_ref?: ArtifactRef | null` (optional persisted thinking trace)
- `tool_calls: list`
- `usage?: object | null`
- `context_stats?: object`
- `stop_reason?: string | null`

### Semantics

- `final_text` is the assistant's final user-facing output.
- `thinking_ref` is optional and must remain semantically separate from `final_text`.
- UI consumers should never infer final answer text from `thinking_ref`.

## `tool_call_end`

Canonical payload fields:

- `tool_execution_id: string`
- `tool_name: string`
- `tool_call_id: string`
- `status: ToolEndStatus`
- `duration_ms: number`
- `output_ref?: ArtifactRef | null`
- `tool_message_ref?: ArtifactRef | null`
- `error_code?: string | null`
- `error?: string | null`
- `status_legacy?: string` (optional compatibility alias)

### ToolEndStatus (v0.2 canonical)

- `running`
- `succeeded`
- `failed`
- `blocked`
- `needs_approval`
- `cancelled`
- `unknown`

### Compatibility notes

- Legacy `denied` is mapped to canonical `blocked`.
- Legacy values may be preserved in `status_legacy` for downstream compatibility.
- For terminal non-success statuses (`failed | blocked | denied | cancelled`), `error_code` should be present.

## Provider-equivalence goal

Different external provider protocols (Responses / ChatCompletions / others) are adapted to the same internal objects and event semantics:

- Final answer => `final_text`
- Optional reasoning/thinking => `thinking_ref`
- Errors => normalized error code and status mapping
