import React from "react";
import { CheckCircle, Loader2, Terminal, XCircle } from "lucide-react";
import { Badge } from "./Badge";

export type ToolLog = {
  id: string;
  tool: string;
  summary: string;
  status: "running" | "succeeded" | "failed" | "blocked" | "needs_approval" | "cancelled" | "unknown";
  durationMs?: number;
  preset?: string;
  subagentRunId?: string;
};

export function TaskExecutionCard(props: {
  title: string;
  status: "running" | "succeeded" | "failed";
  logs: ToolLog[];
  elapsedMs?: number;
}) {
  const { title, status, logs, elapsedMs } = props;

  const StatusIcon = status === "running" ? Loader2 : status === "succeeded" ? CheckCircle : XCircle;
  const statusColor = status === "running" ? "text-amber-500" : status === "succeeded" ? "text-emerald-500" : "text-rose-500";

  return (
    <div className="overflow-hidden rounded-2xl border border-surface-200 bg-surface-0 shadow-soft">
      <div className="flex items-center justify-between border-b border-surface-100 bg-surface-50 px-4 py-2.5">
        <div className="flex min-w-0 items-center gap-2">
          <Terminal className="h-4 w-4 flex-shrink-0 text-ink-500" />
          <span className="truncate text-sm font-semibold text-ink-900" title={title}>
            {title}
          </span>
        </div>
        <div className="flex flex-shrink-0 items-center gap-2">
          <StatusIcon className={["h-4 w-4", statusColor, status === "running" ? "animate-spin" : ""].join(" ")} />
          <Badge tone={status === "running" ? "orange" : status === "succeeded" ? "blue" : "red"}>{status}</Badge>
        </div>
      </div>

      <div className="max-h-36 space-y-1 overflow-y-auto bg-surface-50/50 p-3 font-mono text-xs text-ink-600">
        {logs.length ? (
          logs.slice(-6).map((log) => {
            const isSubagent = Boolean(log.subagentRunId);
            return (
              <div key={log.id} className={["flex items-center gap-2", isSubagent ? "ml-3 border-l-2 border-accent-200 pl-2" : ""].join(" ")}>
                <span className={isSubagent ? "text-violet-500" : "text-accent-600"}>{isSubagent ? "↳" : "$"}</span>
                <span className="min-w-0 flex-1 truncate" title={`${log.tool}\n${log.summary}${log.subagentRunId ? `\nSubagent: ${log.subagentRunId}` : ""}`}>
                  {isSubagent && <span className="mr-1 text-[10px] text-violet-500">[subagent]</span>}
                  {log.summary}
                </span>
                {log.status === "running" ? (
                  <span className="text-amber-500 animate-pulse">•</span>
                ) : (
                  <span className="flex-shrink-0 text-ink-400">
                    ({log.status}
                    {typeof log.durationMs === "number" ? `, ${log.durationMs}ms` : ""})
                  </span>
                )}
              </div>
            );
          })
        ) : (
          <div className="text-ink-400">Waiting for tool calls…</div>
        )}
      </div>

      {typeof elapsedMs === "number" ? (
        <div className="border-t border-surface-100 px-4 py-1.5 text-[10px] text-ink-400">
          Elapsed: {Math.max(0, Math.round(elapsedMs / 1000))}s
        </div>
      ) : null}
    </div>
  );
}

