import React from "react";
import { Bot } from "lucide-react";
import { Badge } from "./Badge";
import { Button } from "./Button";
import { TaskExecutionCard, type ToolLog } from "./TaskExecutionCard";

type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  ts: number;
  locator?: string;
  summary?: string;
  text?: string;
};

type TimelineRow = {
  key: string;
  kind: "llm" | "tool" | "plan" | "approval" | "error";
  title: string;
  subtitle?: string;
  status?: "running" | "succeeded" | "failed" | "cancelled" | "unknown";
  startedAt?: number;
  durationMs?: number;
  toolRunId?: string;
  onOpenTab?: "plan" | "terminal";
};

type TimelineCard = {
  id: string;
  ts: number;
  rows: TimelineRow[];
};

type ChatItem =
  | { kind: "message"; msg: ChatMessage }
  | { kind: "timeline"; card: TimelineCard };

type ToolRun = {
  id: string;
  tool: string;
  summary: string;
  startedAt: number;
  endedAt?: number;
  durationMs?: number;
  status: "running" | "succeeded" | "failed" | "cancelled" | "unknown";
};

export const Chat = React.memo(function Chat(props: {
  chatItems: ChatItem[];
  artifactTexts: Record<string, string>;
  liveAssistant: string;
  liveThinking: string;
  llmRunning: boolean;
  hasRunningTool: boolean;
  toolRuns: ToolRun[];
  activeTaskTitle?: string | null;
  onPickSuggestion?: (text: string) => void;
  fmtTime: (ms: number) => string;
  onOpenRightTab: (tab: "plan" | "terminal") => void;
  onScrollToToolRun: (toolRunId: string) => void;
}) {
  const { chatItems, artifactTexts, liveAssistant, liveThinking, llmRunning, hasRunningTool, toolRuns, activeTaskTitle, onPickSuggestion, fmtTime, onOpenRightTab, onScrollToToolRun } = props;

  const chatStreamRef = React.useRef<HTMLDivElement>(null);

  const [collapsedCards, setCollapsedCards] = React.useState<Record<string, boolean>>({});

  React.useEffect(() => {
    const el = chatStreamRef.current;
    if (!el) return;
    requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight;
    });
  }, [chatItems.length, liveAssistant, liveThinking, hasRunningTool, toolRuns.length]);

  function isCollapsed(cardId: string) {
    return collapsedCards[cardId] ?? true;
  }

  function toggleCollapsed(cardId: string) {
    setCollapsedCards((prev) => ({ ...prev, [cardId]: !(prev[cardId] ?? true) }));
  }

  function summarize(card: TimelineCard) {
    const rows = card.rows;
    const tools = rows.filter((r) => r.kind === "tool").length;
    const hasPlan = rows.some((r) => r.kind === "plan");
    const hasApproval = rows.some((r) => r.kind === "approval");
    const hasError = rows.some((r) => r.kind === "error" || r.status === "failed");
    const durationMs = rows.reduce((acc, r) => acc + (typeof r.durationMs === "number" ? r.durationMs : 0), 0);

    const parts: string[] = [];
    if (tools) parts.push(`Ran ${tools} tools`);
    if (hasPlan) parts.push("Plan updated");
    if (hasApproval) parts.push("Paused");
    if (hasError) parts.push("Errors");

    const dur = durationMs ? `${durationMs}ms` : "—";
    return { text: parts.length ? parts.join(" · ") : "Timeline", dur };
  }

  return (
    <div ref={chatStreamRef} className="flex-1 overflow-y-auto scroll-smooth p-4 md:p-8 space-y-6" id="chat-stream">
      {chatItems.length ? (
        chatItems.map((it) => {
          if (it.kind === "timeline") {
            const card = it.card;
            const collapsed = isCollapsed(card.id);
            const sum = summarize(card);
            const hasRunningLlm = card.rows.some((r) => r.kind === "llm" && r.status === "running");
            return (
              <div key={card.id} className="mx-auto flex max-w-3xl animate-slide-up">
                <div className="w-full rounded-xl border border-surface-100 bg-surface-50/50 px-3 py-2">
                  <button
                    className="flex w-full items-center justify-between text-left"
                    onClick={() => toggleCollapsed(card.id)}
                    title={collapsed ? "Expand" : "Collapse"}
                    type="button"
                  >
                    <div className="text-xs font-semibold text-ink-700">Reasoning timeline</div>
                    <div className="flex items-center gap-3">
                      <div className="text-[10px] text-ink-400">{fmtTime(card.ts)}</div>
                      <div className="text-[10px] font-semibold text-ink-500">{collapsed ? "Show" : "Hide"}</div>
                    </div>
                  </button>

                  {collapsed ? (
                    <div className="mt-2 flex items-center justify-between rounded-lg border border-surface-100 bg-white/60 px-2.5 py-1.5">
                      <div className="min-w-0">
                        <div className="truncate text-xs font-semibold text-ink-900">{sum.text}</div>
                        {hasRunningLlm && (liveThinking || liveAssistant) ? (
                          <div className="mt-1 max-w-[260px] truncate text-[11px] italic text-ink-400">
                            {liveThinking
                              ? `Thinking: ${liveThinking.slice(0, 90)}${liveThinking.length > 90 ? "…" : ""}`
                              : `Response: ${liveAssistant.slice(0, 90)}${liveAssistant.length > 90 ? "…" : ""}`}
                          </div>
                        ) : null}
                      </div>
                      <div className="flex flex-shrink-0 items-center gap-2">
                        <span className="font-mono text-[11px] text-ink-500">{sum.dur}</span>
                        {card.rows.some((r) => r.status === "running") ? <Badge tone="orange">running</Badge> : <Badge tone="gray">done</Badge>}
                      </div>
                    </div>
                  ) : (
                    <div className="mt-3 space-y-2">
                      {card.rows.map((r) => {
                        const tone =
                          r.kind === "error" || r.status === "failed"
                            ? "red"
                            : r.status === "running"
                              ? "orange"
                              : r.status === "succeeded"
                                ? "blue"
                                : "gray";

                        const dur = typeof r.durationMs === "number" ? `${r.durationMs}ms` : undefined;

                        return (
                          <div key={r.key} className="rounded-xl border border-surface-200 bg-surface-50 p-3">
                            <div className="flex items-start justify-between gap-3">
                              <div className="min-w-0 flex-1">
                                <div className="truncate text-xs font-semibold text-ink-900">{r.title}</div>
                                {r.subtitle ? <div className="mt-0.5 truncate font-mono text-[11px] text-ink-500">{r.subtitle}</div> : null}
                                {r.kind === "llm" && r.status === "running" && (liveThinking || liveAssistant) ? (
                                  <div className="mt-2 space-y-1">
                                    {liveThinking ? (
                                      <div className="whitespace-pre-wrap text-[11px] italic text-amber-700/80">
                                        Thinking: {liveThinking.slice(0, 500)}
                                        {liveThinking.length > 500 ? "…" : ""}
                                      </div>
                                    ) : null}
                                    {liveAssistant ? (
                                      <div className="whitespace-pre-wrap text-[11px] text-ink-600">
                                        {liveAssistant.slice(0, 400)}
                                        {liveAssistant.length > 400 ? "…" : ""}
                                      </div>
                                    ) : null}
                                  </div>
                                ) : null}
                              </div>
                              <div className="flex flex-shrink-0 items-center gap-2">
                                {dur ? <span className="font-mono text-[11px] text-ink-500">{dur}</span> : null}
                                {r.status ? <Badge tone={tone as any}>{r.status}</Badge> : <Badge tone={tone as any}>{r.kind}</Badge>}
                                {r.onOpenTab ? (
                                  <Button
                                    title={r.onOpenTab === "terminal" ? "Open in Terminal" : "Open Plan"}
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      onOpenRightTab(r.onOpenTab!);
                                      if (r.onOpenTab === "terminal" && r.toolRunId) onScrollToToolRun(r.toolRunId);
                                    }}
                                  >
                                    Open
                                  </Button>
                                ) : null}
                              </div>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            );
          }

          const m = it.msg;
          const content = (() => {
            if (!m.locator) return m.text ?? "—";
            const v = artifactTexts[m.locator];
            if (typeof v === "string" && v.trim()) return v;
            if (m.summary && m.summary.trim()) return m.summary;
            // 如果有 locator 但内容未加载，说明是后续消息，可能还在处理中
            return null;
          })();
          // 跳过没有内容的消息（避免显示空的 Loading...）
          if (content === null) return null;
          const isUser = m.role === "user";
          const isSystem = m.role === "system";
          if (isSystem) {
            return (
              <div key={m.id} className="mx-auto max-w-3xl animate-slide-up rounded-xl border border-surface-200 bg-surface-0 p-3 font-mono text-xs text-ink-700 shadow-soft">
                <div className="text-[10px] text-ink-400">{fmtTime(m.ts)}</div>
                <div className="mt-1 whitespace-pre-wrap">{content}</div>
              </div>
            );
          }
          return (
            <div key={m.id} className="mx-auto flex max-w-3xl gap-4 group animate-slide-up">
              {isUser ? (
                <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center overflow-hidden rounded-full border border-surface-200 bg-surface-100 text-xs font-semibold text-ink-700 shadow-soft">
                  U
                </div>
              ) : (
                <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-accent-500 to-accent-700 shadow-medium">
                  <Bot className="h-4 w-4 text-white" />
                </div>
              )}
              <div className="min-w-0 flex-1 space-y-1.5">
                <div className="flex items-baseline gap-2">
                  <span className="font-semibold text-ink-900">{isUser ? "You" : "Aura"}</span>
                  <span className="text-xs text-ink-400">{fmtTime(m.ts)}</span>
                </div>
                <div
                  className={[
                    "leading-relaxed text-ink-700",
                    "border border-surface-200 bg-surface-100 p-4 shadow-soft",
                    "rounded-2xl",
                    isUser ? "rounded-tl-sm" : "rounded-tl-2xl",
                  ].join(" ")}
                >
                  <div className="whitespace-pre-wrap">{content}</div>
                </div>
              </div>
            </div>
          );
        })
      ) : (
        <div className="mx-auto w-full max-w-3xl animate-fade-in rounded-2xl border border-surface-200 bg-surface-0 p-6 text-center shadow-soft">
          <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-accent-500 to-accent-700 shadow-medium">
            <Bot className="h-5 w-5 text-white" />
          </div>
          <div className="text-base font-semibold text-ink-900">No messages yet</div>
          <div className="mt-1 text-sm text-ink-500">Try an example prompt:</div>
          <div className="mt-4 grid gap-2 text-left">
            {[
              "Summarize today's news into a DOCX.",
              "Create a task plan for a work request with approvals.",
              "Review this workspace and suggest next steps.",
            ].map((q) => (
              <button
                key={q}
                type="button"
                onClick={() => onPickSuggestion?.(q)}
                className="rounded-xl border border-surface-200 bg-surface-50 px-4 py-3 text-sm text-ink-700 shadow-soft transition-colors hover:bg-surface-100"
              >
                <span className="mr-2 text-accent-600">→</span>
                {q}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Task execution / tool logs */}
      {hasRunningTool ? (
        <div className="mx-auto w-full max-w-3xl animate-fade-in">
          {(() => {
            const logs: ToolLog[] = toolRuns.slice(-10).map((t) => ({
              id: t.id,
              tool: t.tool,
              summary: t.summary,
              status: t.status,
              durationMs: t.durationMs,
              preset: (t as any).preset,
              subagentRunId: (t as any).subagentRunId,
            }));
            const running = toolRuns.filter((t) => t.status === "running");
            const startedAt = running.length ? Math.min(...running.map((t) => t.startedAt)) : null;
            const elapsedMs = typeof startedAt === "number" ? Date.now() - startedAt : undefined;
            const title = activeTaskTitle ? String(activeTaskTitle) : "Task execution";
            return <TaskExecutionCard title={title} status="running" logs={logs} elapsedMs={elapsedMs} />;
          })()}
        </div>
      ) : null}

      {/* AI Typing Indicator */}
        {llmRunning && !liveAssistant && !liveThinking ? (
          <div className="mx-auto flex max-w-3xl gap-4">
            <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-accent-500 to-accent-700 shadow-medium">
              <Bot className="h-4 w-4 text-white" />
            </div>
            <div className="flex items-center gap-1.5 rounded-2xl border border-surface-200 bg-surface-100 px-4 py-3">
              <span className="typing-dot h-2 w-2 rounded-full bg-accent-400" />
              <span className="typing-dot h-2 w-2 rounded-full bg-accent-400" />
              <span className="typing-dot h-2 w-2 rounded-full bg-accent-400" />
            </div>
          </div>
        ) : null}

        {/* Streamed response preview */}
        {llmRunning && (liveAssistant || liveThinking) ? (
          <div className="mx-auto flex max-w-3xl gap-4 animate-fade-in">
            <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-accent-500 to-accent-700 shadow-medium">
              <Bot className="h-4 w-4 text-white" />
            </div>
            <div className="min-w-0 flex-1 space-y-1.5">
              <div className="flex items-baseline gap-2">
                <span className="font-semibold text-ink-900">Aura</span>
                <span className="text-xs text-amber-500 animate-pulse">typing…</span>
              </div>
              <div className="rounded-2xl border border-surface-200 bg-surface-100 p-4 shadow-soft">
                {liveThinking ? (
                  <div className="mb-2 whitespace-pre-wrap text-[11px] italic text-amber-700/80">
                    {liveThinking.slice(0, 600)}
                    {liveThinking.length > 600 ? "…" : ""}
                  </div>
                ) : null}
                {liveAssistant ? <div className="whitespace-pre-wrap text-ink-700">{liveAssistant}</div> : <div className="text-sm text-ink-500">Thinking…</div>}
                <span className="ml-0.5 inline-block h-4 w-1 align-text-bottom bg-accent-500 animate-pulse" />
              </div>
            </div>
          </div>
        ) : null}
    </div>
  );
});
