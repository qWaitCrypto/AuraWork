import React from "react";
import { ChevronDown, Plus, Trash2 } from "lucide-react";
import type { SessionSummary } from "../lib/types";
import { Badge } from "./Badge";

export const Sidebar = React.memo(function Sidebar(props: {
  workspaceName: string;
  workspaceRoot: string;
  sessions: SessionSummary[];
  currentSessionId: string | null;
  onSelectSession: (id: string) => void;
  onRequestDeleteSession: (session: SessionSummary) => void;
  onOpenWorkspacePicker: () => void;

  // Status / derived signals (App computes these for current session)
  approvalsCount: number;
  lastEventKind: string | null;
  hasRunningTool: boolean;
  liveAssistant: string;
  eventsForCurrentSession: { kind: string }[];

  connected: boolean;
  onCreateSession: () => void;
}) {
  const {
    workspaceName,
    workspaceRoot,
    sessions,
    currentSessionId,
    onSelectSession,
    onRequestDeleteSession,
    onOpenWorkspacePicker,
    approvalsCount,
    lastEventKind,
    hasRunningTool,
    liveAssistant,
    eventsForCurrentSession,
    connected,
    onCreateSession,
  } = props;

  function shortSessionId(sessionId: string) {
    const raw = String(sessionId || "").trim();
    const compact = raw.startsWith("sess_") ? raw.slice(5) : raw;
    return compact.slice(0, 8) || raw;
  }

  function formatSessionStamp(ts: number | null | undefined) {
    if (typeof ts !== "number") return null;
    try {
      return new Date(ts).toLocaleString([], {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return null;
    }
  }

  function sessionLabel(s: SessionSummary) {
    const stamp = formatSessionStamp(s.created_at ?? s.updated_at ?? null);
    if (stamp) return `Session ${stamp}`;
    return `Session ${shortSessionId(s.session_id)}`;
  }

  return (
    <aside className="flex w-64 flex-shrink-0 flex-col border-r border-surface-200 bg-surface-100">
      {/* Workspace Select */}
      <div className="border-b border-surface-200 p-4">
        <button
          className="group flex w-full items-center justify-between rounded-lg border border-transparent px-3 py-2.5 shadow-soft transition-colors hover:border-surface-200 hover:bg-surface-0 hover:shadow-medium"
          onClick={onOpenWorkspacePicker}
          title="Select / New Workspace"
          type="button"
        >
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-accent-500 to-accent-600 text-xs font-bold text-white shadow-soft">
              {workspaceName.slice(0, 1).toUpperCase()}
            </div>
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold text-ink-900">{workspaceName}</div>
              <div className="truncate font-mono text-[10px] text-ink-500 group-hover:text-ink-700">{workspaceRoot || "—"}</div>
            </div>
          </div>
          <ChevronDown className="h-4 w-4 text-ink-400" />
        </button>
      </div>

      {/* Session List */}
      <div className="flex-1 overflow-auto p-2">
        <div className="px-3 py-2 text-[10px] font-bold uppercase tracking-widest text-ink-400">Sessions</div>
        {sessions.map((s) => {
          const active = s.session_id === currentSessionId;
          const containerClass = active
            ? "border-accent-200 bg-surface-0 shadow-soft ring-1 ring-accent-100"
            : "border-transparent hover:border-surface-200 hover:bg-surface-0 hover:shadow-soft cursor-pointer";
          return (
            <div
              key={s.session_id}
              className={["group mb-1 flex w-full items-stretch gap-1 rounded-lg border transition-all", containerClass].join(" ")}
              title={s.session_id}
            >
              <button
                onClick={() => onSelectSession(s.session_id)}
                className="flex min-w-0 flex-1 items-start gap-3 p-3 text-left"
                type="button"
              >
                {(() => {
                  const now = Date.now();
                  const updated = typeof s.updated_at === "number" ? s.updated_at : null;
                  const recentlyActive = updated ? now - updated < 2 * 60 * 1000 : false;

                  const currentDerived = active
                    ? (() => {
                      if (approvalsCount > 0 || lastEventKind === "run_paused") return "Paused";
                      if (hasRunningTool || liveAssistant) return "Active";
                      const lastFail = (() => {
                        for (let i = eventsForCurrentSession.length - 1; i >= 0; i--) {
                          const e = eventsForCurrentSession[i];
                          if (e.kind === "operation_failed" || e.kind === "llm_request_failed") return true;
                          if (e.kind === "operation_completed" || e.kind === "llm_response_completed") return false;
                        }
                        return false;
                      })();
                      return lastFail ? "Failed" : "Completed";
                    })()
                    : recentlyActive
                      ? "Active"
                      : "Idle";

                  const dotClass =
                    currentDerived === "Failed"
                      ? "bg-red-500"
                      : currentDerived === "Paused"
                        ? "bg-amber-500"
                        : currentDerived === "Active"
                          ? "bg-accent-500"
                          : "bg-surface-200";

                  const subtitle =
                    currentDerived === "Paused"
                      ? "Approval required"
                      : currentDerived === "Failed"
                        ? "Failed"
                        : currentDerived === "Active"
                          ? "Working…"
                          : "Completed";

                  return (
                    <>
                      <div
                        className={[
                          "mt-1.5 h-2 w-2 flex-shrink-0 rounded-full",
                          dotClass,
                          currentDerived === "Active" ? "animate-pulse-subtle" : "",
                        ].join(" ")}
                        aria-label={currentDerived}
                        title={currentDerived}
                      />
                      <div className="min-w-0 flex-1">
                        <div className={active ? "truncate text-sm font-medium text-ink-900" : "truncate text-sm text-ink-700 group-hover:text-ink-900 transition-colors"}>
                          {sessionLabel(s)}
                        </div>
                        <div className={active ? "mt-1 truncate text-xs text-ink-500" : "mt-1 truncate text-xs text-ink-400"}>
                          {active ? subtitle : updated ? `Completed · ${new Date(updated).toLocaleString()} · ${shortSessionId(s.session_id)}` : subtitle}
                        </div>
                        {s.project_root ? (
                          <div className={active ? "mt-1 truncate font-mono text-[10px] text-ink-400" : "mt-1 truncate font-mono text-[10px] text-ink-400"} title={s.project_root}>
                            {s.project_root}
                          </div>
                        ) : null}
                      </div>
                    </>
                  );
                })()}
              </button>

              <button
                type="button"
                className={[
                  "flex w-10 flex-shrink-0 items-center justify-center rounded-md text-ink-400 transition-colors",
                  "opacity-0 group-hover:opacity-100 hover:bg-rose-50 hover:text-rose-600",
                ].join(" ")}
                title="Delete session"
                onClick={() => onRequestDeleteSession(s)}
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
          );
        })}
      </div>

      <div className="border-t border-surface-200 bg-surface-0/50 p-4">
        <button
          className="group flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-cta-500 to-cta-600 py-3 text-sm font-semibold text-white shadow-lg transition-all hover:shadow-xl hover:from-cta-600 hover:to-cta-600 active:scale-[0.98]"
          style={{ boxShadow: '0 4px 14px rgba(249, 115, 22, 0.35)' }}
          onClick={onCreateSession}
          title="Create new session"
        >
          <Plus className="h-4 w-4 transition-transform duration-200 group-hover:rotate-90" />
          New Session
        </button>
        <div className="mt-3 flex items-center justify-between">
          <div title={connected ? "WebSocket connected" : "WebSocket disconnected"}>
            <Badge tone={connected ? "blue" : "gray"}>{connected ? "Connected" : "Disconnected"}</Badge>
          </div>
          {approvalsCount ? (
            <div title="Pending approvals">
              <Badge tone="orange">{approvalsCount} approvals</Badge>
            </div>
          ) : null}
        </div>
      </div>
    </aside>
  );
});
