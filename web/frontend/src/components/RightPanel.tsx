import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ApprovalRecord, AuraEvent, PlanEnvelope, PlanStep, SessionMeta } from "../lib/types";
import { apiFetchWorkspaceFileText, apiListWorkspaceFiles, type WorkspaceFileEntry } from "../lib/api";
import { httpBase } from "../lib/backendBase";
import { Badge } from "./Badge";
import { Button } from "./Button";
import { useVirtualWindow } from "../hooks/useVirtualWindow";

export type RightTab = "plan" | "files" | "terminal";

type WorkSpecView = {
  goal?: string;
  expectedOutputs: string[];
  workspaceRoots: string[];
  domainAllowlist: string[];
  fileTypeAllowlist: string[];
};

type ToolRun = {
  id: string;
  tool: string;
  summary: string;
  startedAt: number;
  endedAt?: number;
  durationMs?: number;
  status: "running" | "succeeded" | "failed" | "blocked" | "needs_approval" | "cancelled" | "unknown";
  preset?: string;
  subagentRunId?: string;
  workSpec?: WorkSpecView;
};

type TerminalLogKind = "llm" | "tool" | "plan" | "approval" | "error";

type TerminalLogItem = {
  id: string;
  ts: number;
  kind: TerminalLogKind;
  level: "info" | "error";
  title: string;
  subtitle?: string;
  status?: "running" | "succeeded" | "failed" | "blocked" | "needs_approval" | "cancelled" | "unknown";
  durationMs?: number;
  toolRunId?: string;
  expandable?: boolean;
  details?: string;
};


const TERMINAL_ROW_GAP_PX = 12;
const TERMINAL_VIRTUALIZE_THRESHOLD = 120;
const TERMINAL_OVERSCAN_PX = 860;


function cleanText(raw: unknown, maxLen = 220): string | undefined {
  if (typeof raw !== "string") return undefined;
  const text = raw.replace(/\s+/g, " ").trim();
  if (!text) return undefined;
  if (text.length <= maxLen) return text;
  return `${text.slice(0, maxLen - 1)}…`;
}

function cleanStringList(raw: unknown, limit = 4, itemMaxLen = 100): string[] {
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  for (const item of raw) {
    const value = cleanText(item, itemMaxLen);
    if (!value) continue;
    out.push(value);
    if (out.length >= limit) break;
  }
  return out;
}

function parseWorkSpecView(raw: unknown): WorkSpecView | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const ws = raw as Record<string, unknown>;
  const goal = cleanText(ws.goal, 220);

  const expectedOutputs = Array.isArray(ws.expected_outputs)
    ? ws.expected_outputs
      .map((item) => {
        if (!item || typeof item !== "object") return undefined;
        const rec = item as Record<string, unknown>;
        const outputType = cleanText(rec.type, 40);
        const outputPath = cleanText(rec.path, 120);
        if (outputType && outputPath) return `${outputType}: ${outputPath}`;
        return outputPath || outputType || cleanText(rec.format, 60);
      })
      .filter((item): item is string => Boolean(item))
      .slice(0, 6)
    : [];

  const scopeRaw = ws.resource_scope && typeof ws.resource_scope === "object" ? (ws.resource_scope as Record<string, unknown>) : {};
  const workspaceRoots = cleanStringList(scopeRaw.workspace_roots, 4, 120);
  const domainAllowlist = cleanStringList(scopeRaw.domain_allowlist, 4, 100);
  const fileTypeAllowlist = cleanStringList(scopeRaw.file_type_allowlist, 6, 40);

  if (!goal && !expectedOutputs.length && !workspaceRoots.length && !domainAllowlist.length && !fileTypeAllowlist.length) {
    return undefined;
  }

  return {
    goal,
    expectedOutputs,
    workspaceRoots,
    domainAllowlist,
    fileTypeAllowlist,
  };
}

function formatWorkSpecSummary(ws?: WorkSpecView): string | undefined {
  if (!ws) return undefined;
  const parts: string[] = [];
  if (ws.goal) parts.push(`goal: ${ws.goal}`);
  if (ws.expectedOutputs.length) parts.push(`outputs: ${ws.expectedOutputs.length}`);
  if (ws.workspaceRoots.length) parts.push(`roots: ${ws.workspaceRoots.length}`);
  if (ws.domainAllowlist.length) parts.push(`domains: ${ws.domainAllowlist.length}`);
  if (ws.fileTypeAllowlist.length) parts.push(`types: ${ws.fileTypeAllowlist.length}`);
  return parts.length ? parts.join(" · ") : undefined;
}

function formatWorkSpecDetails(ws?: WorkSpecView): string | undefined {
  if (!ws) return undefined;
  const lines: string[] = [];
  if (ws.goal) lines.push(`goal: ${ws.goal}`);
  if (ws.expectedOutputs.length) lines.push(`expected_outputs:\n- ${ws.expectedOutputs.join("\n- ")}`);
  if (ws.workspaceRoots.length) lines.push(`workspace_roots: ${ws.workspaceRoots.join(", ")}`);
  if (ws.domainAllowlist.length) lines.push(`domain_allowlist: ${ws.domainAllowlist.join(", ")}`);
  if (ws.fileTypeAllowlist.length) lines.push(`file_type_allowlist: ${ws.fileTypeAllowlist.join(", ")}`);
  return lines.length ? lines.join("\n") : undefined;
}

function joinDetails(parts: Array<string | undefined>): string | undefined {
  const rows = parts.map((item) => String(item || "").trim()).filter(Boolean);
  return rows.length ? rows.join("\n") : undefined;
}

function normalizeApproverDecision(raw: unknown): "allow" | "deny" | "escalate" | "unknown" {
  const value = String(raw || "").trim().toLowerCase();
  if (!value) return "unknown";
  if (value === "allow") return "allow";
  if (value === "deny") return "deny";
  if (["require_approval", "needs_approval", "escalate"].includes(value)) return "escalate";
  return "unknown";
}

function summarizeApproverTrace(raw: unknown): string | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const trace = raw as Record<string, unknown>;
  const decision = cleanText(trace.decision ?? trace.final_decision, 40);
  const parsed = cleanText(trace.parsed_decision ?? trace.parsed, 40);
  const reason = cleanText(trace.reason, 260);
  const error = cleanText(trace.error, 260);
  const skipped = trace.skipped === true;
  return joinDetails([
    decision ? `decision: ${decision}` : undefined,
    parsed ? `parsed: ${parsed}` : undefined,
    skipped ? "skipped: true" : undefined,
    error ? `error: ${error}` : undefined,
    reason ? `reason: ${reason}` : undefined,
  ]);
}
export const RightPanel = React.memo(function RightPanel(props: {
  currentSessionId: string | null;
  rightTab: RightTab;
  setRightTab: (t: RightTab) => void;

  // Plan
  latestPlan: PlanEnvelope | null;
  planStats: { pending: number; in_progress: number; completed: number; failed: number; total: number; percent: number };
  planCount: number;
  activePlanIndex: number;
  currentPlan: PlanStep[];
  hasRunningTool: boolean;
  statusLabel: string;
  statusTone: "orange" | "gray";
  elapsedText: string;

  // Approvals
  approvals: ApprovalRecord[];
  approvalsCount: number;
  refreshApprovals: () => void;

  // Artifacts
  recentArtifacts: string[];
  artifactTexts: Record<string, string>;
  ensureText: (loc: string) => Promise<string | null>;

  // Terminal
  events: AuraEvent[];
  toolRuns: ToolRun[];

  // Session settings
  sessionMeta: SessionMeta | null;
  onChangeApprovalMode: (mode: string) => void;
  onToggleStreaming: (enabled: boolean) => void;

  // Formatting helpers
  fmtTime: (ms: number) => string;
}) {
  const {
    currentSessionId,
    rightTab,
    setRightTab,
    latestPlan,
    planStats,
    planCount,
    activePlanIndex,
    currentPlan,
    hasRunningTool,
    statusLabel,
    statusTone,
    elapsedText,
    approvals,
    approvalsCount,
    refreshApprovals,
    recentArtifacts,
    artifactTexts,
    ensureText,
    events,
    toolRuns,
    sessionMeta,
    onChangeApprovalMode,
    onToggleStreaming,
    fmtTime,
  } = props;

  const [terminalFilter, setTerminalFilter] = useState<"all" | "llm" | "tools" | "plan" | "approvals" | "errors">("all");
  const terminalListRef = useRef<HTMLDivElement>(null);
  const [workspaceDir, setWorkspaceDir] = useState<string>("");
  const [workspaceEntries, setWorkspaceEntries] = useState<WorkspaceFileEntry[]>([]);
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const [workspaceErr, setWorkspaceErr] = useState<string | null>(null);
  const [selectedWorkspaceFile, setSelectedWorkspaceFile] = useState<string | null>(null);
  const [workspacePreview, setWorkspacePreview] = useState<{ text: string; truncated: boolean; bytes: number } | null>(null);
  const [workspacePreviewLoading, setWorkspacePreviewLoading] = useState(false);

  function encodePath(p: string) {
    return String(p || "")
      .split("/")
      .map((seg) => encodeURIComponent(seg))
      .join("/");
  }

  useEffect(() => {
    setWorkspaceDir("");
    setWorkspaceEntries([]);
    setWorkspaceErr(null);
    setSelectedWorkspaceFile(null);
    setWorkspacePreview(null);
    setWorkspacePreviewLoading(false);
  }, [currentSessionId]);

  async function refreshWorkspaceFiles() {
    if (!currentSessionId) return;
    setWorkspaceLoading(true);
    setWorkspaceErr(null);
    try {
      const data = await apiListWorkspaceFiles(currentSessionId, workspaceDir, { limit: 800 });
      setWorkspaceEntries(Array.isArray(data.entries) ? data.entries : []);
    } catch (error: unknown) {
      setWorkspaceEntries([]);
      setWorkspaceErr(String((error as { message?: unknown } | null)?.message || error || "workspace_files_failed"));
    } finally {
      setWorkspaceLoading(false);
    }
  }

  useEffect(() => {
    if (rightTab !== "files") return;
    if (!currentSessionId) return;
    void refreshWorkspaceFiles();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rightTab, currentSessionId, workspaceDir]);

  const eventsKey = `${events.length}_${events[events.length - 1]?.event_id || ""}`;

  const terminalLogItems = useMemo<TerminalLogItem[]>(() => {
    const toolById = new Map<string, ToolRun>();
    for (const tr of toolRuns) toolById.set(tr.id, tr);

    const items: TerminalLogItem[] = [];

    const MAX = 300;
    const start = Math.max(0, events.length - 2000);
    for (let i = start; i < events.length; i++) {
      const e = events[i];
      const ts = e.timestamp;
      const p = e.payload as Record<string, unknown>;

      if (e.kind === "llm_request_started") {
        items.push({ id: e.event_id, ts, kind: "llm", level: "info", title: "LLM request started", status: "running" });
        continue;
      }
      if (e.kind === "llm_response_completed") {
        items.push({ id: e.event_id, ts, kind: "llm", level: "info", title: "LLM response completed", status: "succeeded" });
        continue;
      }
      if (e.kind === "llm_request_failed") {
        const msg = String(p?.error || p?.message || "llm_request_failed");
        items.push({ id: e.event_id, ts, kind: "llm", level: "error", title: msg, status: "failed" });
        continue;
      }

      if (e.kind === "tool_call_start" || e.kind === "tool_call_end") {
        const id = String(p.tool_execution_id || p.tool_call_id || e.event_id);
        const tr = toolById.get(id);
        const title = tr?.summary || String(p.summary || p.tool_name || "Tool");
        const baseSubtitle = tr?.tool || String(p.tool_name || "tool");
        const status = e.kind === "tool_call_start" ? "running" : (tr?.status ?? "unknown");
        const durationMs =
          e.kind === "tool_call_end" ? (tr?.durationMs ?? (typeof p.duration_ms === "number" ? p.duration_ms : undefined)) : undefined;
        const workSpec = tr?.workSpec ?? parseWorkSpecView(p.work_spec);
        const wsSummary = formatWorkSpecSummary(workSpec);
        const wsDetails = formatWorkSpecDetails(workSpec);
        items.push({
          id: e.event_id,
          ts,
          kind: "tool",
          level: status === "failed" ? "error" : "info",
          title,
          subtitle: wsSummary ? `${baseSubtitle} · ${wsSummary}` : baseSubtitle,
          status,
          durationMs,
          toolRunId: id,
          expandable: true,
          details: joinDetails([
            baseSubtitle,
            tr?.preset ? `preset: ${tr.preset}` : undefined,
            tr?.subagentRunId ? `subagent: ${tr.subagentRunId}` : undefined,
            wsDetails,
          ]),
        });
        continue;
      }

      if (e.kind === "plan_update") {
        const planLen = Array.isArray(p.plan) ? p.plan.length : undefined;
        items.push({
          id: e.event_id,
          ts,
          kind: "plan",
          level: "info",
          title: "Plan updated",
          subtitle: typeof planLen === "number" ? `${planLen} steps` : undefined,
        });
        continue;
      }

      if (e.kind === "subagent_approver_started") {
        const inspection = p.inspection && typeof p.inspection === "object" ? (p.inspection as Record<string, unknown>) : {};
        const actionSummary = cleanText(inspection?.action_summary, 180) || cleanText(p?.tool_name, 80) || "Approver started";
        const risk = cleanText(inspection?.risk_level, 40);
        const reason = cleanText(inspection?.reason, 260);
        items.push({
          id: e.event_id,
          ts,
          kind: "approval",
          level: "info",
          title: `Approver started · ${actionSummary}`,
          subtitle: risk ? `risk: ${risk}` : undefined,
          status: "running",
          expandable: true,
          details: joinDetails([
            reason ? `reason: ${reason}` : undefined,
            formatWorkSpecDetails(parseWorkSpecView(p?.work_spec)),
          ]),
        });
        continue;
      }

      if (e.kind === "subagent_approver_completed") {
        const after = p.inspection_after && typeof p.inspection_after === "object" ? (p.inspection_after as Record<string, unknown>) : {};
        const decision = normalizeApproverDecision(after?.decision);
        const status = decision === "allow" ? "succeeded" : decision === "deny" ? "blocked" : decision === "escalate" ? "needs_approval" : "unknown";
        const actionSummary = cleanText(after?.action_summary, 180) || cleanText(p?.tool_name, 80) || "Approver completed";
        const reason = cleanText(after?.reason, 260);
        items.push({
          id: e.event_id,
          ts,
          kind: "approval",
          level: decision === "deny" ? "error" : "info",
          title: `Approver decision · ${decision}`,
          subtitle: actionSummary,
          status,
          expandable: true,
          details: joinDetails([
            reason ? `reason: ${reason}` : undefined,
            summarizeApproverTrace(p?.approver_trace),
          ]),
        });
        continue;
      }

      if (e.kind === "approval_required") {
        const actionSummary = cleanText(p?.action_summary, 180) || "Approval required";
        const risk = cleanText(p?.risk_level, 40);
        const reason = cleanText(p?.reason, 260);
        const approvalId = cleanText(p?.approval_id, 120);
        items.push({
          id: e.event_id,
          ts,
          kind: "approval",
          level: "info",
          title: actionSummary,
          subtitle: risk ? `risk: ${risk}` : "Approval required",
          status: "needs_approval",
          expandable: true,
          details: joinDetails([
            approvalId ? `approval_id: ${approvalId}` : undefined,
            reason ? `reason: ${reason}` : undefined,
          ]),
        });
        continue;
      }

      if (e.kind === "run_paused") {
        const pendingCount = Array.isArray(p?.pending_tools) ? p.pending_tools.length : undefined;
        items.push({
          id: e.event_id,
          ts,
          kind: "approval",
          level: "info",
          title: "Run paused",
          subtitle: typeof pendingCount === "number" ? `pending tools: ${pendingCount}` : "Awaiting approval",
          status: "needs_approval",
          expandable: true,
          details: cleanText(p?.approval_id, 120) ? `approval_id: ${String(p.approval_id)}` : undefined,
        });
        continue;
      }

      if (e.kind === "approval_granted") {
        const approvalId = cleanText(p?.approval_id, 120);
        items.push({
          id: e.event_id,
          ts,
          kind: "approval",
          level: "info",
          title: "Approval granted",
          subtitle: approvalId,
          status: "succeeded",
          expandable: true,
          details: cleanText(p?.decision, 40) ? `decision: ${String(p.decision)}` : undefined,
        });
        continue;
      }

      if (e.kind === "approval_denied") {
        const approvalId = cleanText(p?.approval_id, 120);
        items.push({
          id: e.event_id,
          ts,
          kind: "approval",
          level: "error",
          title: "Approval denied",
          subtitle: approvalId,
          status: "blocked",
          expandable: true,
          details: cleanText(p?.decision, 40) ? `decision: ${String(p.decision)}` : undefined,
        });
        continue;
      }

      if (e.kind === "run_resumed") {
        const pendingCount = Number.isFinite(Number(p?.pending_tools_count)) ? Number(p.pending_tools_count) : undefined;
        items.push({
          id: e.event_id,
          ts,
          kind: "approval",
          level: "info",
          title: "Run resumed",
          subtitle: typeof pendingCount === "number" ? `pending tools: ${pendingCount}` : undefined,
          status: "running",
          expandable: true,
          details: cleanText(p?.approval_id, 120) ? `approval_id: ${String(p.approval_id)}` : undefined,
        });
        continue;
      }

      if (e.kind === "operation_failed") {
        const msg = String(p?.error || p?.message || "operation_failed");
        items.push({ id: e.event_id, ts, kind: "error", level: "error", title: msg, status: "failed" });
        continue;
      }
    }

    items.sort((a, b) => a.ts - b.ts);
    const filtered = items.filter((it) => {
      if (terminalFilter === "all") return true;
      if (terminalFilter === "llm") return it.kind === "llm";
      if (terminalFilter === "tools") return it.kind === "tool";
      if (terminalFilter === "plan") return it.kind === "plan";
      if (terminalFilter === "approvals") return it.kind === "approval";
      if (terminalFilter === "errors") return it.level === "error" || it.kind === "error";
      return true;
    });

    return filtered.slice(-MAX);
  }, [eventsKey, terminalFilter, toolRuns.length]);

  const estimateTerminalRowSize = useCallback((item: TerminalLogItem) => {
    let height = 76;
    if (item.subtitle) height += 16;
    if (typeof item.durationMs === "number") height += 2;
    if (item.expandable) height += 22;
    if (item.status === "needs_approval" || item.status === "blocked") height += 4;
    if (item.details && item.details.length > 180) height += 12;
    return Math.min(200, height);
  }, []);

  const terminalVirtualWindow = useVirtualWindow({
    containerRef: terminalListRef,
    items: terminalLogItems,
    estimateSize: estimateTerminalRowSize,
    threshold: TERMINAL_VIRTUALIZE_THRESHOLD,
    overscanPx: TERMINAL_OVERSCAN_PX,
    gapPx: TERMINAL_ROW_GAP_PX,
    enabled: rightTab === "terminal",
  });

  const visibleTerminalLogItems = terminalVirtualWindow.enabled
    ? terminalLogItems.slice(terminalVirtualWindow.start, terminalVirtualWindow.end)
    : terminalLogItems;

  const terminalTopPadding = terminalVirtualWindow.enabled
    ? Math.max(0, terminalVirtualWindow.topPadding - (terminalVirtualWindow.start > 0 ? TERMINAL_ROW_GAP_PX : 0))
    : 0;
  const terminalBottomPadding = terminalVirtualWindow.enabled
    ? Math.max(0, terminalVirtualWindow.bottomPadding - (terminalVirtualWindow.end < terminalVirtualWindow.total ? TERMINAL_ROW_GAP_PX : 0))
    : 0;

  const DagPanel = React.lazy(() => import("./DagPanel"));

  return (
    <aside className="flex w-[380px] flex-shrink-0 flex-col border-l border-surface-200 bg-surface-0">
      {/* Tabs - Pill Style */}
      <div className="flex items-center gap-2 border-b border-surface-200 bg-surface-50 px-4 py-3">
        <div className="flex flex-1 gap-1 rounded-lg bg-surface-100 p-1">
          {(["plan", "files", "terminal"] as const).map((tab) => (
            <button
              key={tab}
              className={[
                "flex-1 rounded-md py-1.5 text-xs font-medium transition-all duration-150",
                rightTab === tab
                  ? "bg-white text-ink-900 shadow-sm"
                  : "text-ink-500 hover:text-ink-700",
              ].join(" ")}
              onClick={() => setRightTab(tab)}
              title={tab.charAt(0).toUpperCase() + tab.slice(1)}
            >
              {tab.charAt(0).toUpperCase() + tab.slice(1)}
            </button>
          ))}
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col">
        {rightTab === "plan" ? (
          <>
            {/* Stats */}
            <div className="grid grid-cols-2 gap-px bg-surface-200">
              <div className="bg-surface-0 p-3">
                <div className="text-[10px] font-bold uppercase tracking-wider text-ink-400">Status</div>
                <div className="mt-1 flex items-center gap-1.5 text-sm font-semibold text-ink-700">
                  {statusLabel === "Executing" ? <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-500" /> : null}
                  {statusLabel}
                </div>
              </div>
              <div className="bg-surface-0 p-3">
                <div className="text-[10px] font-bold uppercase tracking-wider text-ink-400">Elapsed</div>
                <div className="mt-1 font-mono text-sm text-ink-700">{elapsedText}</div>
              </div>
            </div>

            {/* Progress */}
            <div className="border-b border-surface-200 bg-surface-0 px-3 py-2">
              <div className="flex items-center justify-between text-[10px] font-semibold text-ink-400">
                <span>Progress</span>
                <span>{planStats.percent}%</span>
              </div>
              <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-surface-200">
                <div
                  className="h-full bg-gradient-to-r from-accent-400 to-accent-600 transition-all duration-500"
                  style={{ width: `${planStats.percent}%` }}
                />
              </div>
            </div>

            {/* Plan (vertical timeline) */}
            <div className="flex-1 overflow-y-auto bg-surface-50/50 p-3">
              <React.Suspense fallback={<div className="text-sm text-ink-500">Loading plan…</div>}>
                <DagPanel latestPlan={latestPlan} />
              </React.Suspense>
            </div>

            {/* Active Step */}
            <div className="border-t border-surface-200 bg-surface-0 p-4">
              <div className="mb-2 flex items-center gap-2">
                {hasRunningTool ? <span className="h-4 w-4 animate-spin rounded-full border-2 border-accent-400 border-t-transparent" /> : <div className="h-4 w-4" />}
                <span className="text-sm font-semibold text-ink-900">
                  {planStats.total ? (
                    activePlanIndex >= 0 ? (
                      <>
                        Step {activePlanIndex + 1} of {planStats.total} · {String(currentPlan[activePlanIndex]?.step || currentPlan[activePlanIndex]?.id || "")}
                      </>
                    ) : (
                      <>
                        {planStats.completed} of {planStats.total} completed
                      </>
                    )
                  ) : (
                    "No plan yet"
                  )}
                </span>
              </div>

              <div className="rounded-lg border border-surface-200 bg-surface-50 p-3 font-mono text-xs text-ink-500">
                {toolRuns.slice(-3).length ? (
                  toolRuns
                    .slice(-3)
                    .map((t) => `> ${t.summary}  (${t.status}${typeof t.durationMs === "number" ? `, ${t.durationMs}ms` : ""})`)
                    .join("\n")
                ) : (
                  <span>
                    <span className="text-accent-600">{">"}</span> Waiting for events…
                  </span>
                )}
              </div>

              <div className="mt-3 flex items-center justify-between">
                <Badge tone={statusTone}>{statusLabel}</Badge>
                {planCount ? <Badge tone="gray">{planCount} steps</Badge> : null}
              </div>

              {approvalsCount ? (
                <div className="mt-4 rounded-xl border border-surface-200 bg-surface-0 shadow-soft">
                  <div className="border-b border-surface-200 px-3 py-2 text-sm font-semibold">Approvals</div>
                  <div className="p-3">
                    <div className="space-y-1">
                      {approvals.slice(0, 3).map((a) => (
                        <div key={a.approval_id} className="rounded-lg border border-surface-200 bg-surface-50 p-3">
                          <div className="font-mono text-xs text-ink-700">{a.approval_id}</div>
                          <div className="mt-1 text-xs text-ink-700">{a.action_summary}</div>
                          <div className="mt-2 flex items-center gap-2">
                            <Badge tone="orange">{a.risk_level || "high"}</Badge>
                            <Badge tone="gray">{a.status}</Badge>
                          </div>
                        </div>
                      ))}
                    </div>
                    <div className="mt-3">
                      <Button onClick={refreshApprovals}>Refresh approvals</Button>
                    </div>
                  </div>
                </div>
              ) : null}
            </div>
          </>
        ) : null}

        {rightTab === "files" ? (
          <div className="flex min-h-0 flex-1 flex-col">
            {/* Header */}
            <div className="flex items-center justify-between border-b border-surface-200 bg-surface-0 px-4 py-3">
              <div className="min-w-0">
                <div className="text-sm font-semibold text-ink-700">Workspace files</div>
                <div className="mt-0.5 truncate font-mono text-[10px] text-ink-400">{workspaceDir ? `/${workspaceDir}` : "/"}</div>
              </div>
              <div className="flex items-center gap-2">
                <Badge tone="gray">{workspaceEntries.length}</Badge>
                <Button onClick={() => void refreshWorkspaceFiles()} disabled={workspaceLoading || !currentSessionId}>
                  Refresh
                </Button>
              </div>
            </div>

            {/* Workspace files list */}
            <div className="flex-1 overflow-y-auto bg-surface-50/50 p-3">
              {workspaceErr ? (
                <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-xs text-rose-700">{workspaceErr}</div>
              ) : null}

              {workspaceLoading && !workspaceEntries.length ? (
                <div className="flex h-24 items-center justify-center rounded-xl border border-dashed border-surface-200 text-sm text-ink-400">
                  Loading…
                </div>
              ) : null}

              {!workspaceLoading && !workspaceEntries.length && !workspaceErr ? (
                <div className="flex h-24 items-center justify-center rounded-xl border border-dashed border-surface-200 text-sm text-ink-400">
                  No files
                </div>
              ) : null}

              {workspaceEntries.length ? (
                <div className="space-y-2">
                  {workspaceDir ? (
                    <button
                      type="button"
                      className="w-full rounded-lg border border-surface-200 bg-surface-0 px-3 py-2 text-left text-xs text-ink-700 shadow-soft hover:bg-surface-50"
                      onClick={() => {
                        const parts = workspaceDir.split("/").filter(Boolean);
                        parts.pop();
                        setSelectedWorkspaceFile(null);
                        setWorkspacePreview(null);
                        setWorkspaceDir(parts.join("/"));
                      }}
                    >
                      ← Up
                    </button>
                  ) : null}

                  {workspaceEntries.map((ent) => {
                    const selected = selectedWorkspaceFile === ent.path;
                    const href = currentSessionId
                      ? `${httpBase()}/api/sessions/${encodeURIComponent(currentSessionId)}/workspace/file/${encodePath(ent.path)}`
                      : "#";
                    const sizeLabel = ent.is_dir ? "dir" : typeof ent.size_bytes === "number" ? `${ent.size_bytes} bytes` : "file";
                    return (
                      <div
                        key={ent.path}
                        className={[
                          "rounded-lg border p-3 transition-all cursor-pointer",
                          selected
                            ? "border-accent-300 bg-accent-50 shadow-soft ring-1 ring-accent-200"
                            : "border-surface-200 bg-surface-0 hover:border-surface-300 hover:shadow-soft",
                        ].join(" ")}
                        onClick={async () => {
                          if (ent.is_dir) {
                            setSelectedWorkspaceFile(null);
                            setWorkspacePreview(null);
                            setWorkspaceDir(ent.path);
                            return;
                          }

                          const next = selected ? null : ent.path;
                          setSelectedWorkspaceFile(next);
                          setWorkspacePreview(null);
                          if (!next || !currentSessionId) return;
                          setWorkspacePreviewLoading(true);
                          try {
                            const data = await apiFetchWorkspaceFileText(currentSessionId, next, { maxBytes: 220_000 });
                            setWorkspacePreview({ text: data.text, truncated: data.truncated, bytes: data.bytes });
                          } catch (error: unknown) {
                            setWorkspacePreview({ text: String((error as { message?: unknown } | null)?.message || error || "preview_failed"), truncated: false, bytes: 0 });
                          } finally {
                            setWorkspacePreviewLoading(false);
                          }
                        }}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <div className="min-w-0 flex-1">
                            <div className="truncate text-xs font-medium text-ink-800">{ent.name}</div>
                            <div className="mt-0.5 truncate font-mono text-[10px] text-ink-400">
                              {ent.path} · {sizeLabel} · {fmtTime(ent.modified_at)}
                            </div>
                          </div>
                          <div className="flex items-center gap-1.5">
                            {ent.is_dir ? (
                              <Badge tone="gray">dir</Badge>
                            ) : (
                              <a
                                className="rounded-md border border-surface-200 bg-surface-0 px-2 py-1 text-[10px] font-medium text-ink-600 transition-colors hover:bg-surface-100"
                                href={href}
                                target="_blank"
                                rel="noreferrer"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  if (!currentSessionId) e.preventDefault();
                                }}
                                title="Download"
                              >
                                ↓
                              </a>
                            )}
                          </div>
                        </div>

                        {selected && !ent.is_dir ? (
                          <div className="mt-3 animate-fade-in">
                            <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-lg border border-surface-200 bg-surface-0 p-3 font-mono text-[11px] text-ink-700">
                              {workspacePreviewLoading ? "Loading…" : workspacePreview?.text ?? "—"}
                            </pre>
                            {workspacePreview?.truncated ? <div className="mt-1 text-[10px] text-ink-400">Preview truncated.</div> : null}
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
                </div>
              ) : null}
            </div>
          </div>
        ) : null}

        {rightTab === "terminal" ? (
          <div className="flex min-h-0 flex-1 flex-col">
            {/* Header with filters */}
            <div className="flex items-center justify-between border-b border-surface-200 bg-surface-0 px-4 py-3">
              <div className="text-sm font-semibold text-ink-700">Run Log</div>
              <div className="flex items-center gap-1">
                {(["all", "tools", "llm", "errors"] as const).map((k) => (
                  <button
                    key={k}
                    onClick={() => setTerminalFilter(k)}
                    className={[
                      "rounded-md px-2 py-1 text-[10px] font-medium transition-colors",
                      terminalFilter === k
                        ? "bg-accent-100 text-accent-700"
                        : "text-ink-500 hover:bg-surface-100",
                    ].join(" ")}
                  >
                    {k}
                  </button>
                ))}
              </div>
            </div>

            {/* Log List */}
            <div ref={terminalListRef} className="flex-1 overflow-y-auto bg-surface-50/50 p-3">
              {terminalLogItems.length ? (
                <div className="space-y-3">
                  {terminalVirtualWindow.enabled && terminalTopPadding > 0 ? <div aria-hidden style={{ height: terminalTopPadding }} /> : null}
                  {visibleTerminalLogItems.map((it) => {
                    const isError = it.level === "error" || it.status === "failed";
                    const isRunning = it.status === "running";
                    const isSuccess = it.status === "succeeded";
                    const isBlocked = it.status === "blocked" || it.status === "needs_approval";

                    const borderColor = isError
                      ? "border-l-rose-400"
                      : isRunning || isBlocked
                        ? "border-l-amber-400"
                        : isSuccess
                          ? "border-l-emerald-400"
                          : "border-l-surface-300";

                    const tone = isError ? "red" : isRunning || isBlocked ? "orange" : isSuccess ? "blue" : "gray";
                    const anchorId = it.toolRunId ? `toolrun_${it.toolRunId}` : `log_${it.id}`;

                    return (
                      <div
                        id={anchorId}
                        key={it.id}
                        className={[
                          "rounded-lg border border-surface-200 border-l-[3px] bg-surface-0 p-3 shadow-soft transition-all hover:shadow-medium",
                          borderColor,
                        ].join(" ")}
                      >
                        {/* Top row: kind + time + status */}
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-2">
                            <span className="rounded bg-surface-100 px-1.5 py-0.5 text-[9px] font-bold uppercase text-ink-500">
                              {it.kind}
                            </span>
                            <span className="text-[10px] text-ink-400">{fmtTime(it.ts)}</span>
                          </div>
                          <div className="flex items-center gap-2">
                            {typeof it.durationMs === "number" && (
                              <span className="font-mono text-[10px] text-ink-400">{it.durationMs}ms</span>
                            )}
                            <Badge tone={tone}>{it.status || it.level}</Badge>
                          </div>
                        </div>

                        {/* Title */}
                        <div className="mt-2 text-xs font-medium text-ink-800 leading-relaxed">{it.title}</div>

                        {/* Subtitle */}
                        {it.subtitle && (
                          <div className="mt-1 truncate font-mono text-[10px] text-ink-500">{it.subtitle}</div>
                        )}

                        {/* Expandable details */}
                        {it.expandable && (
                          <details className="mt-2 group">
                            <summary className="cursor-pointer text-[10px] text-ink-400 hover:text-ink-600">▸ Details</summary>
                            <pre className="mt-2 overflow-auto whitespace-pre-wrap rounded-md border border-surface-200 bg-surface-50 p-2 font-mono text-[10px] text-ink-600 animate-fade-in">
                              {it.details || "—"}
                            </pre>
                          </details>
                        )}
                      </div>
                    );
                  })}
                  {terminalVirtualWindow.enabled && terminalBottomPadding > 0 ? <div aria-hidden style={{ height: terminalBottomPadding }} /> : null}
                </div>
              ) : (
                <div className="flex h-32 items-center justify-center rounded-xl border border-dashed border-surface-200 text-sm text-ink-400">
                  No logs yet
                </div>
              )}
            </div>

            <div className="mt-4 rounded-xl border border-surface-200 bg-surface-0 p-3">
              <div className="text-xs font-semibold text-ink-500">Session settings</div>
              <div className="mt-3 space-y-1.5">
                <div className="flex items-center justify-between gap-2">
                  <div className="text-xs text-ink-500">Approvals</div>
                  <select
                    className="w-40 rounded-lg border border-surface-200 bg-surface-0 px-2 py-1 text-xs"
                    value={sessionMeta?.tool_approval_mode || "standard"}
                    onChange={(e) => onChangeApprovalMode(e.target.value)}
                  >
                    <option value="standard">standard</option>
                    <option value="strict">strict</option>
                    <option value="trusted">trusted</option>
                  </select>
                </div>
                <div className="flex items-center justify-between">
                  <label className="text-xs text-ink-500">Streaming</label>
                  <input
                    type="checkbox"
                    checked={Boolean(sessionMeta?.llm_streaming ?? true)}
                    onChange={(e) => onToggleStreaming(e.target.checked)}
                  />
                </div>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </aside>
  );
});
