import React from "react";
import { ArrowDown, Bot } from "lucide-react";
import { Badge } from "./Badge";
import { Button } from "./Button";
import { TaskExecutionCard } from "./TaskExecutionCard";
import type { ChatItem, ToolRun, ToolLog, TimelineCard } from "../types";
import { useVirtualWindow } from "../hooks/useVirtualWindow";

const CHAT_ROW_GAP_PX = 24;
const CHAT_VIRTUALIZE_THRESHOLD = 90;
const CHAT_OVERSCAN_PX = 960;
const CHAT_AUTO_SCROLL_THRESHOLD_PX = 120;

type RenderableChatRow = {
  key: string;
  item: ChatItem;
  content?: string;
};

function resolveMessageContent(msg: { locator?: string; text?: string; summary?: string }, artifactTexts: Record<string, string>): string | null {
  if (!msg.locator) return msg.text ?? "—";
  const value = artifactTexts[msg.locator];
  if (typeof value === "string" && value.trim()) return value;
  if (typeof msg.text === "string" && msg.text.trim()) return msg.text;
  if (typeof msg.summary === "string" && msg.summary.trim()) return msg.summary;
  return null;
}

type MarkdownBlock =
  | { kind: "text"; content: string }
  | { kind: "code"; content: string; language: string };

function parseMarkdownBlocks(raw: string): MarkdownBlock[] {
  const lines = String(raw || "").replace(/\r\n/g, "\n").split("\n");
  const blocks: MarkdownBlock[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    const fence = line.match(/^```\s*([^`\s]*)\s*$/);
    if (fence) {
      const language = String(fence[1] || "").trim();
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      blocks.push({ kind: "code", content: codeLines.join("\n"), language });
      if (index < lines.length) index += 1;
      continue;
    }

    const textLines: string[] = [];
    while (index < lines.length && !/^```\s*([^`\s]*)\s*$/.test(lines[index])) {
      textLines.push(lines[index]);
      index += 1;
    }

    const content = textLines.join("\n").trim();
    if (content) blocks.push({ kind: "text", content });
  }

  return blocks.length ? blocks : [{ kind: "text", content: String(raw || "") }];
}

function renderInlineMarkdown(text: string, keyPrefix: string): React.ReactNode[] {
  return text.split(/(`[^`\n]+`)/g).filter(Boolean).map((part, index) => {
    if (part.startsWith("`") && part.endsWith("`") && part.length >= 2) {
      return (
        <code key={`${keyPrefix}-code-${index}`} className="rounded bg-surface-100 px-1.5 py-0.5 font-mono text-[0.92em] text-ink-700">
          {part.slice(1, -1)}
        </code>
      );
    }
    return <React.Fragment key={`${keyPrefix}-text-${index}`}>{part}</React.Fragment>;
  });
}

function renderMarkdownTextBlock(content: string, keyPrefix: string): React.ReactNode[] {
  const paragraphs = content.split(/\n{2,}/).map((item) => item.trim()).filter(Boolean);

  return paragraphs.map((paragraph, index) => {
    const key = `${keyPrefix}-${index}`;
    const lines = paragraph.split("\n").map((line) => line.trim()).filter(Boolean);

    if (lines.length === 1) {
      const heading = lines[0].match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        const level = heading[1].length;
        const text = heading[2];
        if (level === 1) {
          return <h1 key={key} className="text-base font-semibold text-ink-900">{renderInlineMarkdown(text, key)}</h1>;
        }
        if (level === 2) {
          return <h2 key={key} className="text-sm font-semibold text-ink-900">{renderInlineMarkdown(text, key)}</h2>;
        }
        return <h3 key={key} className="text-sm font-medium text-ink-800">{renderInlineMarkdown(text, key)}</h3>;
      }
    }

    if (lines.length > 0 && lines.every((line) => /^[-*]\s+/.test(line))) {
      return (
        <ul key={key} className="list-disc space-y-1 pl-5 text-sm leading-relaxed">
          {lines.map((line, lineIndex) => (
            <li key={`${key}-li-${lineIndex}`}>
              {renderInlineMarkdown(line.replace(/^[-*]\s+/, ""), `${key}-li-${lineIndex}`)}
            </li>
          ))}
        </ul>
      );
    }

    if (lines.length > 0 && lines.every((line) => /^\d+\.\s+/.test(line))) {
      return (
        <ol key={key} className="list-decimal space-y-1 pl-5 text-sm leading-relaxed">
          {lines.map((line, lineIndex) => (
            <li key={`${key}-oli-${lineIndex}`}>
              {renderInlineMarkdown(line.replace(/^\d+\.\s+/, ""), `${key}-oli-${lineIndex}`)}
            </li>
          ))}
        </ol>
      );
    }

    return (
      <p key={key} className="whitespace-pre-wrap text-sm leading-relaxed text-ink-700">
        {renderInlineMarkdown(paragraph, key)}
      </p>
    );
  });
}

function MarkdownText(props: { text: string; className?: string }) {
  const blocks = React.useMemo(() => parseMarkdownBlocks(props.text), [props.text]);

  return (
    <div className={props.className}>
      {blocks.map((block, index) => {
        if (block.kind === "code") {
          return (
            <pre key={`md-code-${index}`} className="overflow-x-auto rounded-xl border border-surface-200 bg-surface-100 px-3 py-2 font-mono text-xs text-ink-700">
              {block.language ? <div className="mb-1 text-[10px] uppercase tracking-wide text-ink-400">{block.language}</div> : null}
              <code>{block.content}</code>
            </pre>
          );
        }
        return (
          <div key={`md-text-${index}`} className="space-y-2">
            {renderMarkdownTextBlock(block.content, `md-text-${index}`)}
          </div>
        );
      })}
    </div>
  );
}

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

  const isNearBottomRef = React.useRef(true);
  const [isNearBottom, setIsNearBottom] = React.useState(true);

  const updateBottomState = React.useCallback(() => {
    const el = chatStreamRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.clientHeight - el.scrollTop;
    const nearBottom = distance <= CHAT_AUTO_SCROLL_THRESHOLD_PX;
    isNearBottomRef.current = nearBottom;
    setIsNearBottom((prev) => (prev === nearBottom ? prev : nearBottom));
  }, []);

  const scrollToLatest = React.useCallback((behavior: ScrollBehavior = "smooth") => {
    const el = chatStreamRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
    isNearBottomRef.current = true;
    setIsNearBottom(true);
  }, []);

  React.useEffect(() => {
    const el = chatStreamRef.current;
    if (!el) return;

    const onScroll = () => {
      updateBottomState();
    };

    onScroll();
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
    };
  }, [updateBottomState]);

  const renderableRows = React.useMemo<RenderableChatRow[]>(() => {
    const rows: RenderableChatRow[] = [];
    for (const item of chatItems) {
      if (item.kind === "timeline") {
        rows.push({ key: `timeline:${item.card.id}`, item });
        continue;
      }
      const content = resolveMessageContent(item.msg, artifactTexts);
      if (content === null) continue;
      rows.push({ key: `message:${item.msg.id}`, item, content });
    }
    return rows;
  }, [artifactTexts, chatItems]);

  const estimateRowSize = React.useCallback((row: RenderableChatRow) => {
    if (row.item.kind === "timeline") {
      const collapsed = collapsedCards[row.item.card.id] ?? true;
      if (collapsed) return 96;
      const count = row.item.card.rows.length;
      return Math.min(760, 140 + count * 86);
    }

    const msg = row.item.msg;
    const contentLen = row.content ? row.content.length : 0;
    if (msg.role === "system") return Math.min(260, 76 + Math.ceil(contentLen / 64) * 18);
    return Math.min(680, 112 + Math.ceil(contentLen / 58) * 20);
  }, [collapsedCards]);

  const virtualWindow = useVirtualWindow({
    containerRef: chatStreamRef,
    items: renderableRows,
    estimateSize: estimateRowSize,
    threshold: CHAT_VIRTUALIZE_THRESHOLD,
    overscanPx: CHAT_OVERSCAN_PX,
    gapPx: CHAT_ROW_GAP_PX,
  });

  const visibleRows = virtualWindow.enabled
    ? renderableRows.slice(virtualWindow.start, virtualWindow.end)
    : renderableRows;

  const topPadding = virtualWindow.enabled
    ? Math.max(0, virtualWindow.topPadding - (virtualWindow.start > 0 ? CHAT_ROW_GAP_PX : 0))
    : 0;
  const bottomPadding = virtualWindow.enabled
    ? Math.max(0, virtualWindow.bottomPadding - (virtualWindow.end < virtualWindow.total ? CHAT_ROW_GAP_PX : 0))
    : 0;

  React.useEffect(() => {
    if (!isNearBottomRef.current) return;

    const raf = requestAnimationFrame(() => {
      const el = chatStreamRef.current;
      if (!el) return;
      el.scrollTop = el.scrollHeight;
      isNearBottomRef.current = true;
      setIsNearBottom(true);
    });

    return () => cancelAnimationFrame(raf);
  }, [renderableRows.length, liveAssistant, liveThinking, hasRunningTool, toolRuns.length]);

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
    if (hasApproval) parts.push("Approval flow");
    if (hasError) parts.push("Errors");

    const dur = durationMs ? `${durationMs}ms` : "—";
    return { text: parts.length ? parts.join(" · ") : "Timeline", dur };
  }

  return (
    <div className="relative flex-1 min-h-0">
      <div ref={chatStreamRef} className="h-full overflow-y-auto scroll-smooth p-4 md:p-8 space-y-6" id="chat-stream">
      {renderableRows.length ? (
        <>
          {virtualWindow.enabled && topPadding > 0 ? <div aria-hidden style={{ height: topPadding }} /> : null}
          {visibleRows.map((row) => {
            const it = row.item;
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
                            : r.status === "blocked" || r.status === "needs_approval"
                              ? "orange"
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
                                {r.details ? (
                                  <details className="mt-1 rounded-lg border border-surface-200 bg-white/70 px-2 py-1.5">
                                    <summary className="cursor-pointer text-[10px] font-medium text-ink-500">Details</summary>
                                    <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap font-mono text-[10px] text-ink-600">{r.details}</pre>
                                  </details>
                                ) : null}
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
                                {r.kind === "llm" && r.status !== "running" && r.thinkingLocator ? (() => {
                                  const t = artifactTexts[r.thinkingLocator];
                                  if (!(typeof t === "string" && t.trim())) return null;
                                  return (
                                    <details className="mt-2 rounded-lg border border-surface-200 bg-white/70 px-2.5 py-2">
                                      <summary className="cursor-pointer text-[11px] font-medium text-ink-600">Thinking</summary>
                                      <div className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-ink-600">
                                        {t}
                                      </div>
                                    </details>
                                  );
                                })() : null}
                              </div>
                              <div className="flex flex-shrink-0 items-center gap-2">
                                {dur ? <span className="font-mono text-[11px] text-ink-500">{dur}</span> : null}
                                {r.status ? <Badge tone={tone}>{r.status}</Badge> : <Badge tone={tone}>{r.kind}</Badge>}
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
          const content = row.content ?? resolveMessageContent(m, artifactTexts);
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
                    "leading-relaxed text-ink-700 p-4 shadow-soft",
                    isUser
                      ? "rounded-2xl rounded-tr-sm bg-accent-50 border border-accent-100"
                      : "rounded-2xl rounded-tl-sm bg-surface-0 border-l-2 border-accent-400 border-t border-r border-b border-t-surface-200 border-r-surface-200 border-b-surface-200",
                  ].join(" ")}
                >
                  {isUser ? <div className="whitespace-pre-wrap">{content}</div> : <MarkdownText text={content} className="space-y-2" />}
                </div>
              </div>
            </div>
          );
          })}
          {virtualWindow.enabled && bottomPadding > 0 ? <div aria-hidden style={{ height: bottomPadding }} /> : null}
        </>
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
                className="rounded-xl border border-surface-200 bg-surface-50 px-4 py-3 text-sm text-ink-700 shadow-soft transition-all duration-200 hover:bg-surface-100 hover:border-accent-200 hover:shadow-md cursor-pointer"
              >
                <span className="mr-2 text-cta-500">→</span>
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
              preset: t.preset,
              subagentRunId: t.subagentRunId,
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
              {liveAssistant ? <MarkdownText text={liveAssistant} className="space-y-2" /> : <div className="text-sm text-ink-500">Thinking…</div>}
              <span className="ml-0.5 inline-block h-4 w-1 align-text-bottom bg-accent-500 animate-pulse" />
            </div>
          </div>
        </div>
      ) : null}
      </div>

      {!isNearBottom && renderableRows.length ? (
        <div className="pointer-events-none absolute bottom-4 right-6 z-20">
          <Button
            type="button"
            variant="primary"
            className="pointer-events-auto rounded-full px-3 py-1.5 text-xs shadow-elevated"
            onClick={() => scrollToLatest()}
          >
            <ArrowDown className="h-3.5 w-3.5" />
            Jump to latest
          </Button>
        </div>
      ) : null}
    </div>
  );
});
