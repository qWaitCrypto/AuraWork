import React, { useMemo, useState } from "react";
import type { ApprovalRecord } from "../lib/types";
import { Badge } from "./Badge";
import { Button } from "./Button";

export type RightTab = "plan" | "files" | "terminal";

type ToolRun = {
  id: string;
  tool: string;
  summary: string;
  startedAt: number;
  endedAt?: number;
  durationMs?: number;
  status: "running" | "succeeded" | "failed" | "cancelled" | "unknown";
  preset?: string;
  subagentRunId?: string;
};

type TerminalLogKind = "llm" | "tool" | "plan" | "approval" | "error";

type TerminalLogItem = {
  id: string;
  ts: number;
  kind: TerminalLogKind;
  level: "info" | "error";
  title: string;
  subtitle?: string;
  status?: "running" | "succeeded" | "failed" | "cancelled" | "unknown";
  durationMs?: number;
  toolRunId?: string;
  expandable?: boolean;
  details?: string;
};

export const RightPanel = React.memo(function RightPanel(props: {
  rightTab: RightTab;
  setRightTab: (t: RightTab) => void;

  // Plan
  latestPlan: any;
  planStats: { pending: number; in_progress: number; completed: number; failed: number; total: number; percent: number };
  planCount: number;
  activePlanIndex: number;
  currentPlan: any[];
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
  events: any[];
  toolRuns: ToolRun[];

  // Session settings
  sessionMeta: any;
  onChangeApprovalMode: (mode: string) => void;
  onToggleStreaming: (enabled: boolean) => void;

  // Formatting helpers
  fmtTime: (ms: number) => string;
}) {
  const {
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
  const [selectedArtifact, setSelectedArtifact] = useState<string | null>(null);

  const terminalLogItems = useMemo<TerminalLogItem[]>(() => {
    const toolById = new Map<string, ToolRun>();
    for (const tr of toolRuns) toolById.set(tr.id, tr);

    const items: TerminalLogItem[] = [];

    const MAX = 300;
    const start = Math.max(0, events.length - 2000);
    for (let i = start; i < events.length; i++) {
      const e = events[i];
      const ts = e.timestamp;

      if (e.kind === "llm_request_started") {
        items.push({ id: e.event_id, ts, kind: "llm", level: "info", title: "LLM request started", status: "running" });
        continue;
      }
      if (e.kind === "llm_response_completed") {
        items.push({ id: e.event_id, ts, kind: "llm", level: "info", title: "LLM response completed", status: "succeeded" });
        continue;
      }
      if (e.kind === "llm_request_failed") {
        const p = e.payload as any;
        const msg = String(p?.error || p?.message || "llm_request_failed");
        items.push({ id: e.event_id, ts, kind: "llm", level: "error", title: msg, status: "failed" });
        continue;
      }

      if (e.kind === "tool_call_start" || e.kind === "tool_call_end") {
        const p = e.payload as any;
        const id = String(p.tool_execution_id || p.tool_call_id || e.event_id);
        const tr = toolById.get(id);
        const title = tr?.summary || String(p.summary || p.tool_name || "Tool");
        const subtitle = tr?.tool || String(p.tool_name || "tool");
        const status = e.kind === "tool_call_start" ? "running" : (tr?.status ?? "unknown");
        const durationMs =
          e.kind === "tool_call_end" ? (tr?.durationMs ?? (typeof p.duration_ms === "number" ? p.duration_ms : undefined)) : undefined;
        items.push({
          id: e.event_id,
          ts,
          kind: "tool",
          level: status === "failed" ? "error" : "info",
          title,
          subtitle,
          status,
          durationMs,
          toolRunId: id,
          expandable: true,
          details: `${subtitle}${tr?.preset ? `\nPreset: ${tr.preset}` : ""}${tr?.subagentRunId ? `\nSubagent: ${tr.subagentRunId}` : ""}`,
        });
        continue;
      }

      if (e.kind === "plan_update") {
        const planLen = Array.isArray((e.payload as any)?.plan) ? ((e.payload as any).plan as any[]).length : undefined;
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

      if (e.kind === "approval_required" || e.kind === "run_paused") {
        items.push({ id: e.event_id, ts, kind: "approval", level: "info", title: "Paused", subtitle: "Approval required" });
        continue;
      }

      if (e.kind === "operation_failed") {
        const p = e.payload as any;
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
  }, [events, terminalFilter, toolRuns]);

  const DagPanel = React.lazy(() => import("./DagPanel"));

  return (
    <aside className="flex w-[380px] flex-shrink-0 flex-col border-l border-surface-200 bg-surface-0">
      {/* Tabs */}
      <div className="flex items-center border-b border-surface-200 bg-surface-50">
        <button
          className={["flex-1 py-3 text-xs font-bold uppercase tracking-wide transition-colors", rightTab === "plan" ? "tab-active" : "tab-inactive"].join(
            " "
          )}
          onClick={() => setRightTab("plan")}
          title="Plan"
        >
          Plan
        </button>
        <button
          className={["flex-1 py-3 text-xs font-bold uppercase tracking-wide transition-colors", rightTab === "files" ? "tab-active" : "tab-inactive"].join(
            " "
          )}
          onClick={() => setRightTab("files")}
          title="Files"
        >
          Files
        </button>
        <button
          className={[
            "flex-1 py-3 text-xs font-bold uppercase tracking-wide transition-colors",
            rightTab === "terminal" ? "tab-active" : "tab-inactive",
          ].join(" ")}
          onClick={() => setRightTab("terminal")}
          title="Terminal"
        >
          Terminal
        </button>
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

            {/* Mermaid Graph */}
            <div className="flex-1 overflow-auto p-4 flex items-center justify-center bg-surface-50">
              <div className="w-full">
                <React.Suspense fallback={<div className="text-sm text-ink-500">Loading plan…</div>}>
                  <DagPanel latestPlan={latestPlan} />
                </React.Suspense>
              </div>
            </div>

            {/* Active Step */}
            <div className="border-t border-surface-200 bg-surface-0 p-4">
              <div className="mb-2 flex items-center gap-2">
                {hasRunningTool ? <span className="h-4 w-4 animate-spin rounded-full border-2 border-accent-400 border-t-transparent" /> : <div className="h-4 w-4" />}
                <span className="text-sm font-semibold text-ink-900">
                  {planStats.total ? (
                    activePlanIndex >= 0 ? (
                      <>
                        Step {activePlanIndex + 1} of {planStats.total} · {String((currentPlan[activePlanIndex] as any)?.step || (currentPlan[activePlanIndex] as any)?.id)}
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
                <Badge tone={statusTone as any}>{statusLabel}</Badge>
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
          <div className="min-h-0 flex-1 overflow-auto p-3">
            <div className="flex items-center justify-between">
              <div className="text-xs font-semibold text-ink-500">Artifacts</div>
              <div className="text-[10px] text-ink-400">Preview</div>
            </div>

            <div className="mt-3 grid min-h-0 grid-cols-2 gap-2">
              {/* List */}
              <div className="min-h-0 overflow-auto rounded-xl border border-surface-200 bg-surface-0">
                <div className="border-b border-surface-200 px-3 py-2 text-[10px] font-bold uppercase tracking-widest text-ink-400">Recent</div>
                <div className="p-2">
                  {recentArtifacts.length ? (
                    <div className="space-y-1">
                      {recentArtifacts.map((loc) => {
                        const selected = selectedArtifact === loc;
                        const cached = artifactTexts[loc] !== undefined;
                        const href = `/api/artifacts/${encodeURIComponent(loc)}`;
                        return (
                          <button
                            key={loc}
                            onClick={() => {
                              setSelectedArtifact(loc);
                              void ensureText(loc);
                            }}
                            className={[
                              "w-full rounded-md border p-2.5 text-left transition-colors",
                              selected ? "border-accent-200 bg-accent-50" : "border-surface-200 bg-surface-0 hover:bg-surface-50",
                            ].join(" ")}
                          >
                            <div className="truncate font-mono text-[11px] text-ink-700">{loc}</div>
                            <div className="mt-2 flex items-center justify-between gap-2">
                              <Badge tone={cached ? "blue" : "gray"}>{cached ? "cached" : "not loaded"}</Badge>
                              <div className="flex items-center gap-2">
                                <a
                                  className="rounded-lg border border-surface-200 bg-surface-0 px-2 py-1 text-[10px] font-semibold text-ink-600 hover:bg-surface-50"
                                  href={href}
                                  target="_blank"
                                  rel="noreferrer"
                                  onClick={(e) => e.stopPropagation()}
                                  title="Download artifact"
                                >
                                  Download
                                </a>
                                <Button
                                  title="Copy locator"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    void navigator.clipboard.writeText(loc);
                                  }}
                                >
                                  Copy
                                </Button>
                              </div>
                            </div>
                          </button>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="p-3 text-sm text-ink-500">No artifacts yet.</div>
                  )}
                </div>
              </div>

              {/* Preview */}
              <div className="min-h-0 overflow-auto rounded-xl border border-surface-200 bg-surface-0">
                <div className="border-b border-surface-200 px-3 py-2 text-[10px] font-bold uppercase tracking-widest text-ink-400">Preview</div>
                <div className="p-3">
                  {selectedArtifact ? (
                    <>
                      <div className="truncate font-mono text-[11px] text-ink-500">{selectedArtifact}</div>
                      <pre className="mt-2 max-h-[520px] overflow-auto whitespace-pre-wrap rounded-lg border border-surface-200 bg-surface-50 p-3 font-mono text-xs text-ink-700">
                        {artifactTexts[selectedArtifact] ?? "Loading…"}
                      </pre>
                    </>
                  ) : (
                    <div className="text-sm text-ink-500">Select an artifact to preview.</div>
                  )}
                </div>
              </div>
            </div>
          </div>
        ) : null}

        {rightTab === "terminal" ? (
          <div className="min-h-0 flex-1 overflow-auto p-3">
            <div className="flex items-center justify-between">
              <div className="text-xs font-semibold text-ink-500">Run log</div>
              <div className="flex flex-wrap items-center gap-1">
                {(["all", "llm", "tools", "plan", "approvals", "errors"] as const).map((k) => (
                  <button
                    key={k}
                    onClick={() => setTerminalFilter(k)}
                    title={`Filter: ${k}`}
                    className={[
                      "rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
                      terminalFilter === k
                        ? "border-accent-200 bg-accent-50 text-accent-700"
                        : "border-surface-200 bg-surface-0 text-ink-500 hover:bg-surface-50",
                    ].join(" ")}
                  >
                    {k}
                  </button>
                ))}
              </div>
            </div>

            <div className="mt-3 space-y-1">
              {terminalLogItems.length ? (
                terminalLogItems.map((it) => {
                  const tone =
                    it.level === "error" || it.status === "failed"
                      ? "red"
                      : it.status === "running"
                        ? "orange"
                        : it.status === "succeeded"
                          ? "blue"
                          : "gray";

                  const anchorId = it.toolRunId ? `toolrun_${it.toolRunId}` : `log_${it.id}`;

                  return (
                    <div
                      id={anchorId}
                      key={it.id}
                      className="rounded-md border border-surface-200 bg-surface-0 p-2.5 transition-colors hover:bg-surface-50"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="text-[10px] font-bold uppercase tracking-wider text-ink-400">{it.kind}</span>
                            <span className="text-[10px] text-ink-400">{fmtTime(it.ts)}</span>
                          </div>
                          <div className="mt-1 truncate text-xs font-medium text-ink-900">{it.title}</div>
                          {it.subtitle ? <div className="mt-0.5 truncate font-mono text-[11px] text-ink-500">{it.subtitle}</div> : null}

                          {it.expandable ? (
                            <details className="mt-2">
                              <summary className="cursor-pointer text-[11px] text-ink-500 hover:text-ink-700">Details</summary>
                              <div className="mt-2 rounded-lg border border-surface-200 bg-surface-0 p-2 font-mono text-[11px] text-ink-700">
                                {it.details || "—"}
                              </div>
                            </details>
                          ) : null}
                        </div>

                        <div className="flex flex-shrink-0 items-center gap-2">
                          {typeof it.durationMs === "number" ? <span className="font-mono text-[11px] text-ink-500">{it.durationMs}ms</span> : null}
                          {it.status ? <Badge tone={tone as any}>{it.status}</Badge> : <Badge tone={tone as any}>{it.level}</Badge>}
                        </div>
                      </div>
                    </div>
                  );
                })
              ) : (
                <div className="text-sm text-ink-500">No logs yet.</div>
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
