import type { ToolRunStatus } from "../types";

const TOOL_END_STATUS_MAP: Record<string, ToolRunStatus> = {
  ok: "succeeded",
  success: "succeeded",
  succeeded: "succeeded",
  completed: "succeeded",
  done: "succeeded",
  error: "failed",
  failed: "failed",
  cancelled: "cancelled",
  canceled: "cancelled",
  denied: "blocked",
  blocked: "blocked",
  needs_approval: "needs_approval",
  require_approval: "needs_approval",
  requires_approval: "needs_approval",
  pending_approval: "needs_approval",
  running: "running",
};

export function normalizeToolEndStatus(rawStatus: string | null | undefined): ToolRunStatus {
  const normalized = String(rawStatus || "").trim().toLowerCase();
  if (!normalized) return "unknown";
  return TOOL_END_STATUS_MAP[normalized] ?? "unknown";
}
