import React from "react";
import { Bot } from "lucide-react";
import { Badge } from "./Badge";
import { Button } from "./Button";

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

export const Chat = React.memo(function Chat(props: {
  chatItems: ChatItem[];
  artifactTexts: Record<string, string>;
  liveAssistant: string;
  fmtTime: (ms: number) => string;
  onOpenRightTab: (tab: "plan" | "terminal") => void;
  onScrollToToolRun: (toolRunId: string) => void;
}) {
  const { chatItems, artifactTexts, liveAssistant, fmtTime, onOpenRightTab, onScrollToToolRun } = props;

  const [collapsedCards, setCollapsedCards] = React.useState<Record<string, boolean>>({});

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
    <div className="flex-1 overflow-y-auto scroll-smooth p-4 md:p-8 space-y-6" id="chat-stream">
      {chatItems.length ? (
        chatItems.map((it) => {
          if (it.kind === "timeline") {
            const card = it.card;
            const collapsed = isCollapsed(card.id);
            const sum = summarize(card);
            return (
              <div key={card.id} className="mx-auto flex max-w-3xl">
                <div className="w-full rounded-2xl border border-surface-200 bg-surface-0 p-4 shadow-soft">
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
                    <div className="mt-3 flex items-center justify-between rounded-xl border border-surface-200 bg-surface-50 p-3">
                      <div className="min-w-0">
                        <div className="truncate text-xs font-semibold text-ink-900">{sum.text}</div>
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
                          <div key={r.key} className="flex items-start justify-between gap-3 rounded-xl border border-surface-200 bg-surface-50 p-3">
                            <div className="min-w-0">
                              <div className="truncate text-xs font-semibold text-ink-900">{r.title}</div>
                              {r.subtitle ? <div className="mt-0.5 truncate font-mono text-[11px] text-ink-500">{r.subtitle}</div> : null}
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
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            );
            }

            const m = it.msg;
            const content = m.locator ? artifactTexts[m.locator] ?? m.summary ?? "Loading…" : m.text ?? "—";
            const isUser = m.role === "user";
            const isSystem = m.role === "system";
            if (isSystem) {
              return (
                <div key={m.id} className="mx-auto max-w-3xl rounded-xl border border-surface-200 bg-surface-0 p-3 font-mono text-xs text-ink-700 shadow-soft">
                  <div className="text-[10px] text-ink-400">{fmtTime(m.ts)}</div>
                  <div className="mt-1 whitespace-pre-wrap">{content}</div>
                </div>
              );
            }
            return (
              <div key={m.id} className="mx-auto flex max-w-3xl gap-4 group">
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
          <div className="mx-auto max-w-3xl text-sm text-ink-500">No messages yet.</div>
        )}

        {/* AI Typing Indicator */}
        {liveAssistant ? (
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
    </div>
  );
});
