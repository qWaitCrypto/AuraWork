import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowUp,
  Bot,
  Folder,
  History,
  Loader2,
  MessageCircle,
  Paperclip,
  Plus,
  Settings,
} from "lucide-react";
import { apiBootstrap, apiCreateSession, apiFetchArtifact, apiGetApprovals, apiGetSession } from "./lib/api";
import type { ApprovalRecord, AuraEvent, SessionSummary } from "./lib/types";
import { connectSessionWs, wsSend, type ServerMsg } from "./lib/ws";
import { useArtifactStore } from "./store/artifactStore";
import { useEventStore } from "./store/eventStore";
import { useUiStore } from "./store/uiStore";
import { Badge } from "./components/Badge";
import { Button } from "./components/Button";
import { Modal } from "./components/Modal";
import { Chat } from "./components/Chat";
import { RightPanel } from "./components/RightPanel";
import { Sidebar } from "./components/Sidebar";
import { Topbar } from "./components/Topbar";

function fmtTime(ms: number) {
  try {
    return new Date(ms).toLocaleTimeString();
  } catch {
    return "";
  }
}

function fmtElapsed(ms: number) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hh = Math.floor(total / 3600);
  const mm = Math.floor((total % 3600) / 60);
  const ss = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(hh)}:${pad(mm)}:${pad(ss)}`;
}

function basename(p: string) {
  const s = String(p || "");
  const parts = s.split(/[/\\\\]/).filter(Boolean);
  return parts[parts.length - 1] || s || "workspace";
}

type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  ts: number;
  locator?: string;
  summary?: string;
  text?: string;
  requestId?: string | null;
  turnId?: string | null;
};

type TimelineRow = {
  key: string;
  kind: "llm" | "tool" | "plan" | "approval" | "error";
  title: string;
  subtitle?: string;
  status?: "running" | "succeeded" | "failed" | "cancelled" | "unknown";
  startedAt?: number;
  endedAt?: number;
  durationMs?: number;
  toolRunId?: string;
  count?: number;
  onOpenTab?: "plan" | "terminal";
};

type TimelineCard = {
  id: string;
  ts: number;
  requestId?: string | null;
  turnId?: string | null;
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
  preset?: string;
  subagentRunId?: string;
  requestId?: string | null;
  turnId?: string | null;
};


function toolCategory(toolName: string) {
  if (
    [
      "project__read_text",
      "project__read_text_many",
      "project__search_text",
      "project__list_dir",
      "project__glob",
      "project__text_stats",
      "session__search",
      "web__fetch",
      "web__search",
      "mcp__list_servers",
      "mcp__list_tools",
    ].includes(toolName) ||
    toolName.startsWith("skill__")
  )
    return "Explored";
  if (["project__apply_patch", "project__apply_edits", "project__patch"].includes(toolName)) return "Edited";
  if (toolName === "update_plan") return "Planned";
  if (toolName === "update_todo") return "Todo";
  if (toolName.startsWith("spec__")) return "Spec";
  if (toolName === "session__export") return "Ran";
  if (toolName === "shell__run") return "Ran";
  return "Tools";
}

export default function App() {
  const appendMany = useEventStore((s) => s.appendMany);
  const clearEvents = useEventStore((s) => s.clear);
  const events = useEventStore((s) => s.events);

  const ensureText = useArtifactStore((s) => s.ensureText);
  const artifactTexts = useArtifactStore((s) => s.texts);
  const artifactLoading = useArtifactStore((s) => s.loading);

  const bootstrap = useUiStore((s) => s.bootstrap);
  const sessions = useUiStore((s) => s.sessions);
  const currentSessionId = useUiStore((s) => s.currentSessionId);
  const sessionMeta = useUiStore((s) => s.sessionMeta);
  const activeApproval = useUiStore((s) => s.activeApproval);
  const approvals = useUiStore((s) => s.approvals);

  const setBootstrap = useUiStore((s) => s.setBootstrap);
  const setSessions = useUiStore((s) => s.setSessions);
  const setCurrentSession = useUiStore((s) => s.setCurrentSession);
  const setSessionMeta = useUiStore((s) => s.setSessionMeta);
  const setApprovals = useUiStore((s) => s.setApprovals);
  const popApproval = useUiStore((s) => s.popApproval);

  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const [draft, setDraft] = useState("");
  const [diffText, setDiffText] = useState<string | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [rightTab, setRightTab] = useState<"plan" | "files" | "terminal">("plan");
  const [tick, setTick] = useState(0);

  const wsEventQueueRef = useRef<AuraEvent[]>([]);
  const wsFlushScheduledRef = useRef(false);

  function flushWsEvents() {
    wsFlushScheduledRef.current = false;
    const batch = wsEventQueueRef.current;
    if (!batch.length) return;
    wsEventQueueRef.current = [];

    appendMany(batch);

    // Refresh approvals at most once per batch.
    for (const e of batch) {
      if (e.kind === "approval_required" || e.kind === "run_paused") {
        refreshApprovals().catch(() => {});
        break;
      }
    }
  }

  function enqueueWsEvent(e: AuraEvent) {
    wsEventQueueRef.current.push(e);
    if (wsFlushScheduledRef.current) return;
    wsFlushScheduledRef.current = true;
    requestAnimationFrame(flushWsEvents);
  }

  async function refreshBootstrap() {
    const b = await apiBootstrap();
    setBootstrap(b);
    setSessions(b.sessions || []);
    if (!currentSessionId && b.sessions?.length) setCurrentSession(b.sessions[0].session_id);
  }

  async function refreshApprovals() {
    if (!currentSessionId) return;
    const a = await apiGetApprovals(currentSessionId);
    setApprovals(a);
  }

  useEffect(() => {
    refreshBootstrap().catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!currentSessionId) return;
    clearEvents();
    setConnected(false);

    if (wsRef.current) {
      try {
        wsRef.current.close();
      } catch {}
    }

    const ws = connectSessionWs(currentSessionId, (msg: ServerMsg) => {
      if (msg.type === "session_meta") {
        setSessionMeta(msg.meta);
        return;
      }
      if (msg.type === "replay") {
        appendMany(msg.events || []);
        return;
      }
      if (msg.type === "event") {
        const e = msg.event as AuraEvent;
        enqueueWsEvent(e);
        return;
      }
    });

    ws.addEventListener("open", () => setConnected(true));
    ws.addEventListener("close", () => setConnected(false));
    wsRef.current = ws;

    apiGetSession(currentSessionId)
      .then((meta) => setSessionMeta(meta))
      .catch(() => {});
    refreshApprovals().catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentSessionId]);

  const latestPlan = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (e.kind === "plan_update" && Array.isArray((e.payload as any)?.plan)) return e.payload as any;
    }
    const metaPlan = (sessionMeta as any)?.plan;
    return Array.isArray(metaPlan) ? ({ plan: metaPlan } as any) : null;
  }, [events, sessionMeta]);

  const currentPlan = useMemo(() => {
    return Array.isArray((latestPlan as any)?.plan) ? ((latestPlan as any).plan as any[]) : [];
  }, [latestPlan]);

  const planCount = currentPlan.length;

  const planStats = useMemo(() => {
    const counts = { pending: 0, in_progress: 0, completed: 0, failed: 0 };
    for (const it of currentPlan) {
      const st = String(it?.status || "pending");
      if (st === "in_progress") counts.in_progress += 1;
      else if (st === "completed") counts.completed += 1;
      else if (st === "failed") counts.failed += 1;
      else counts.pending += 1;
    }
    const total = currentPlan.length || 0;
    const done = counts.completed + counts.failed;
    const percent = total ? Math.round((done / total) * 100) : 0;
    return { ...counts, total, percent };
  }, [currentPlan]);

  const liveAssistant = useMemo(() => {
    let open = false;
    let buf = "";
    for (const e of events) {
      if (e.kind === "llm_request_started") {
        open = true;
        buf = "";
      }
      if (e.kind === "llm_response_delta" && open) buf += String((e.payload as any).text_delta || "");
      if (e.kind === "llm_response_completed" || e.kind === "llm_request_failed") {
        open = false;
        buf = "";
      }
    }
    return open ? buf : "";
  }, [events]);

  const chatMessages = useMemo<ChatMessage[]>(() => {
    const out: ChatMessage[] = [];
    for (const e of events) {
      if (e.kind === "operation_started" && String((e.payload as any)?.op_kind) === "chat") {
        const ref = (e.payload as any)?.input_ref;
        if (ref?.locator)
          out.push({
            id: e.event_id,
            role: "user",
            ts: e.timestamp,
            locator: String(ref.locator),
            summary: ref.summary,
            requestId: e.request_id ?? null,
            turnId: e.turn_id ?? null,
          });
        continue;
      }
      if (e.kind === "llm_response_completed") {
        const ref = (e.payload as any)?.output_ref;
        if (ref?.locator)
          out.push({
            id: e.event_id,
            role: "assistant",
            ts: e.timestamp,
            locator: String(ref.locator),
            summary: ref.summary,
            requestId: e.request_id ?? null,
            turnId: e.turn_id ?? null,
          });
        continue;
      }
      if (e.kind === "llm_request_failed") {
        const p = e.payload as any;
        const msg = String(p?.error || p?.message || "llm_request_failed");
        out.push({ id: e.event_id, role: "system", ts: e.timestamp, text: msg, requestId: e.request_id ?? null, turnId: e.turn_id ?? null });
        continue;
      }
      if (e.kind === "operation_failed") {
        const p = e.payload as any;
        const msg = String(p?.error || p?.message || "operation_failed");
        out.push({ id: e.event_id, role: "system", ts: e.timestamp, text: msg, requestId: e.request_id ?? null, turnId: e.turn_id ?? null });
        continue;
      }
    }
    return out;
  }, [events]);

  const toolRuns = useMemo<ToolRun[]>(() => {
    const byId = new Map<string, ToolRun>();
    for (const e of events) {
      if (e.kind !== "tool_call_start" && e.kind !== "tool_call_end") continue;
      const p = e.payload as any;
      const id = String(p.tool_execution_id || p.tool_call_id || e.event_id);
      const tool = String(p.tool_name || "tool");
      const summary = String(p.summary || tool);

      if (e.kind === "tool_call_start") {
        byId.set(id, {
          id,
          tool,
          summary,
          startedAt: e.timestamp,
          status: "running",
          preset: p.preset,
          subagentRunId: p.subagent_run_id,
          requestId: e.request_id ?? null,
          turnId: e.turn_id ?? null,
        });
      } else {
        const prev = byId.get(id);
        const statusRaw = String(p.status || "unknown").toLowerCase();
        const status =
          statusRaw === "succeeded"
            ? "succeeded"
            : statusRaw === "failed"
              ? "failed"
              : statusRaw === "cancelled"
                ? "cancelled"
                : "unknown";
        byId.set(id, {
          ...(prev || {
            id,
            tool,
            summary,
            startedAt: e.timestamp,
            status: "unknown",
            requestId: e.request_id ?? null,
            turnId: e.turn_id ?? null,
          }),
          tool,
          summary,
          endedAt: e.timestamp,
          durationMs: typeof p.duration_ms === "number" ? p.duration_ms : prev?.durationMs,
          status,
          preset: p.preset ?? prev?.preset,
          subagentRunId: p.subagent_run_id ?? prev?.subagentRunId,
          requestId: prev?.requestId ?? (e.request_id ?? null),
          turnId: prev?.turnId ?? (e.turn_id ?? null),
        });
      }
    }
    return Array.from(byId.values())
      .sort((a, b) => a.startedAt - b.startedAt)
      .slice(-120);
  }, [events]);


  const chatItems = useMemo<ChatItem[]>(() => {
    const items: ChatItem[] = [];

    // Group events into "runs" primarily by request_id/turn_id.
    const groups = new Map<string, { requestId: string | null; turnId: string | null; events: AuraEvent[]; ts: number }>();
    function groupKey(e: AuraEvent) {
      const rid = e.request_id ?? null;
      const tid = e.turn_id ?? null;
      if (rid) return `r:${rid}`;
      if (tid) return `t:${tid}`;
      return `e:${e.event_id}`;
    }

    for (const e of events) {
      const k = groupKey(e);
      const g = groups.get(k);
      if (g) {
        g.events.push(e);
        if (e.timestamp < g.ts) g.ts = e.timestamp;
      } else {
        groups.set(k, { requestId: e.request_id ?? null, turnId: e.turn_id ?? null, events: [e], ts: e.timestamp });
      }
    }

    // Index tool runs by id for quick lookup.
    const toolById = new Map<string, ToolRun>();
    for (const tr of toolRuns) toolById.set(tr.id, tr);

    // Create timeline cards for groups that contain relevant "reasoning" events.
    const timelineByKey = new Map<string, TimelineCard>();

    for (const [k, g] of groups.entries()) {
      const rows: TimelineRow[] = [];

      // LLM state
      let llmStart: AuraEvent | null = null;
      let llmEnd: AuraEvent | null = null;
      let llmFailed: AuraEvent | null = null;
      for (const e of g.events) {
        if (e.kind === "llm_request_started") llmStart = e;
        if (e.kind === "llm_response_completed") llmEnd = e;
        if (e.kind === "llm_request_failed") llmFailed = e;
      }
      if (llmStart || llmEnd || llmFailed) {
        const startedAt = llmStart?.timestamp ?? llmEnd?.timestamp ?? llmFailed?.timestamp;
        const endedAt = (llmFailed ?? llmEnd)?.timestamp;
        const status = llmFailed ? "failed" : llmEnd ? "succeeded" : "running";
        const durationMs = startedAt && endedAt ? Math.max(0, endedAt - startedAt) : undefined;
        rows.push({
          key: `llm:${k}`,
          kind: "llm",
          title: "Thinking",
          status,
          startedAt: startedAt ?? undefined,
          endedAt: endedAt ?? undefined,
          durationMs,
        });
      }

      // Tools in this group
      const toolIds = new Set<string>();
      for (const e of g.events) {
        if (e.kind !== "tool_call_start" && e.kind !== "tool_call_end") continue;
        const p = e.payload as any;
        const id = String(p.tool_execution_id || p.tool_call_id || e.event_id);
        toolIds.add(id);
      }
      for (const id of toolIds) {
        const tr = toolById.get(id);
        if (!tr) continue;
        rows.push({
          key: `tool:${id}`,
          kind: "tool",
          title: tr.summary,
          subtitle: tr.tool,
          status: tr.status,
          startedAt: tr.startedAt,
          endedAt: tr.endedAt,
          durationMs: tr.durationMs,
          toolRunId: tr.id,
          onOpenTab: "terminal",
        });
      }

      // Plan updates
      for (const e of g.events) {
        if (e.kind !== "plan_update") continue;
        const planLen = Array.isArray((e.payload as any)?.plan) ? ((e.payload as any).plan as any[]).length : undefined;
        rows.push({
          key: `plan:${e.event_id}`,
          kind: "plan",
          title: "Plan updated",
          subtitle: typeof planLen === "number" ? `${planLen} steps` : undefined,
          onOpenTab: "plan",
        });
      }

      // Paused/approval
      for (const e of g.events) {
        if (e.kind === "run_paused" || e.kind === "approval_required") {
          rows.push({ key: `pause:${e.event_id}`, kind: "approval", title: "Paused", subtitle: "Approval required" });
          break;
        }
      }

      // Errors
      for (const e of g.events) {
        if (e.kind !== "operation_failed" && e.kind !== "llm_request_failed") continue;
        const p = e.payload as any;
        const msg = String(p?.error || p?.message || e.kind);
        rows.push({ key: `err:${e.event_id}`, kind: "error", title: msg, status: "failed" });
      }

      if (rows.length) {
        // Order rows deterministically by start time when possible.
        rows.sort((a, b) => (a.startedAt ?? 0) - (b.startedAt ?? 0));
        timelineByKey.set(k, { id: `tl_${k}`, ts: g.ts, requestId: g.requestId, turnId: g.turnId, rows });
      }
    }

    // Merge chat messages and timelines in chronological order.
    const msgs: { ts: number; item: ChatItem }[] = [];
    for (const m of chatMessages) msgs.push({ ts: m.ts, item: { kind: "message", msg: m } });
    for (const tl of timelineByKey.values()) msgs.push({ ts: tl.ts, item: { kind: "timeline", card: tl } });

    msgs.sort((a, b) => a.ts - b.ts);

    // De-duplicate: if timeline is too close and empty, keep as is.
    for (const it of msgs) items.push(it.item);
    return items;
  }, [chatMessages, events, toolRuns]);

  useEffect(() => {
    const need: string[] = [];
    for (const m of chatMessages) {
      if (!m.locator) continue;
      const loc = m.locator;
      if (artifactTexts[loc] !== undefined) continue;
      if (artifactLoading[loc]) continue;
      need.push(loc);
    }
    if (!need.length) return;
    for (const loc of need.slice(0, 10)) void ensureText(loc);
  }, [artifactLoading, artifactTexts, chatMessages, ensureText]);

  async function createSession() {
    const { session_id } = await apiCreateSession();
    await refreshBootstrap();
    setCurrentSession(session_id);
  }

  function sendChat() {
    const text = draft.trim();
    if (!text) return;
    if (!wsSend(wsRef.current, { type: "chat", text })) return;
    setDraft("");
  }

  async function loadDiff(rec: ApprovalRecord) {
    setDiffText(null);
    const diffRef = (rec as any).diff_ref;
    if (!diffRef?.locator) return;
    setDiffLoading(true);
    try {
      setDiffText(await apiFetchArtifact(String(diffRef.locator)));
    } catch {
      setDiffText("(Failed to load diff preview)");
    } finally {
      setDiffLoading(false);
    }
  }

  useEffect(() => {
    if (activeApproval) loadDiff(activeApproval).catch(() => {});
    else setDiffText(null);
  }, [activeApproval]);

  function decideApproval(decision: "approve" | "deny") {
    if (!activeApproval || !currentSessionId) return;
    wsSend(wsRef.current, { type: "approval", approval_id: activeApproval.approval_id, decision });
    popApproval();
    refreshApprovals().catch(() => {});
  }

  const modelProfiles = bootstrap?.model_profiles || [];
  const approvalsCount = approvals.length;
  const workspaceRoot = bootstrap?.project_root || "";
  const workspaceName = basename(workspaceRoot);

  const lastEvent = events.length ? events[events.length - 1] : null;
  const hasRunningTool = toolRuns.some((t) => t.status === "running");
  const isPaused = approvalsCount > 0 || lastEvent?.kind === "run_paused";
  const statusLabel = isPaused ? "Paused" : hasRunningTool ? "Executing" : "Idle";
  const statusTone = isPaused ? "orange" : hasRunningTool ? "orange" : "gray";

  const startTs = events.length ? events[0].timestamp : Date.now();
  const endTs = hasRunningTool ? Date.now() : lastEvent?.timestamp ?? Date.now();
  const elapsedMs = endTs - startTs + tick * 0;

  useEffect(() => {
    if (!hasRunningTool) return;
    const id = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, [hasRunningTool]);

  const activePlanIndex = useMemo(() => {
    for (let i = 0; i < currentPlan.length; i++) {
      const st = String((currentPlan[i] as any)?.status || "");
      if (st === "in_progress") return i;
    }
    return -1;
  }, [currentPlan]);

  const recentArtifacts = useMemo(() => {
    const locs: string[] = [];
    const seen = new Set<string>();
    for (const m of chatMessages) {
      if (!m.locator) continue;
      const loc = m.locator;
      if (seen.has(loc)) continue;
      seen.add(loc);
      locs.push(loc);
    }
    for (const a of approvals) {
      const diff = (a as any)?.diff_ref?.locator;
      if (diff && !seen.has(diff)) {
        seen.add(diff);
        locs.push(String(diff));
      }
    }
    return locs.slice(-30).reverse();
  }, [approvals, chatMessages]);

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  function autosizeTextarea() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }

  return (
    <div className="h-full overflow-hidden bg-surface-50 text-ink-900 selection:bg-accent-100">
      {/* Match prototype: body is full-width flex; no global max-width wrapper */}
      <div className="flex h-full overflow-hidden">
        {/* Left Nav Rail */}
        <nav className="z-20 flex w-16 flex-shrink-0 flex-col items-center gap-5 border-r border-surface-200 bg-surface-0 py-6">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-accent-500 to-accent-700 shadow-medium">
            <Bot className="h-5 w-5 text-white" />
          </div>

          <button
            className="group relative flex h-10 w-10 items-center justify-center rounded-lg border border-accent-100 bg-accent-50 text-accent-600 hover:bg-accent-100"
            title="Chat"
          >
            <MessageCircle className="h-5 w-5" />
          </button>
          <button className="group relative flex h-10 w-10 items-center justify-center rounded-lg text-ink-500 hover:bg-surface-100 hover:text-ink-700" title="Files">
            <Folder className="h-5 w-5" />
          </button>
          <button className="group relative flex h-10 w-10 items-center justify-center rounded-lg text-ink-500 hover:bg-surface-100 hover:text-ink-700" title="History">
            <History className="h-5 w-5" />
          </button>

          <div className="flex-1" />
          <button
            className="group relative flex h-10 w-10 items-center justify-center rounded-lg text-ink-500 hover:bg-surface-100 hover:text-ink-700"
            title="Settings"
          >
            <Settings className="h-5 w-5" />
          </button>
          <div
            className="flex h-10 w-10 items-center justify-center rounded-full border-2 border-surface-200 bg-surface-100 text-xs font-semibold text-ink-700 shadow-soft"
            title="User"
          >
            U
          </div>
        </nav>

        <Sidebar
          workspaceName={workspaceName}
          workspaceRoot={workspaceRoot}
          sessions={sessions}
          currentSessionId={currentSessionId}
          onSelectSession={(id) => setCurrentSession(id)}
          approvalsCount={approvalsCount}
          lastEventKind={lastEvent?.kind ?? null}
          hasRunningTool={hasRunningTool}
          liveAssistant={liveAssistant}
          eventsForCurrentSession={events}
          connected={connected}
          onCreateSession={() => void createSession()}
        />

        {/* Main Chat Area */}
        <main className="relative flex min-w-0 flex-1 flex-col bg-surface-0 textured-bg">
          <Topbar
            currentSessionId={currentSessionId}
            modelProfiles={modelProfiles}
            sessionMeta={sessionMeta}
            onChangeChatProfile={(profileId) => wsSend(wsRef.current, { type: "settings", chat_profile_id: profileId || undefined })}
          />

          <Chat
            chatItems={chatItems as any}
            artifactTexts={artifactTexts}
            liveAssistant={liveAssistant}
            fmtTime={fmtTime}
            onOpenRightTab={(tab) => setRightTab(tab)}
            onScrollToToolRun={(toolRunId) => {
              requestAnimationFrame(() => {
                const el = document.getElementById(`toolrun_${toolRunId}`);
                if (el) el.scrollIntoView({ block: "nearest" });
              });
            }}
          />

          {/* Input Area */}
          <div className="p-4 pb-6 max-w-3xl mx-auto w-full">
              <div className="relative rounded-2xl border border-surface-200 bg-surface-0 shadow-elevated transition-all focus-within:border-accent-300 focus-within:ring-2 focus-within:ring-accent-100">
                <textarea
                  ref={textareaRef}
                  rows={1}
                  placeholder="Message Aura..."
                  className="max-h-48 w-full resize-none bg-transparent px-4 py-4 pr-24 text-sm text-ink-900 outline-none placeholder:text-ink-400"
                  value={draft}
                  onChange={(e) => {
                    setDraft(e.target.value);
                    queueMicrotask(autosizeTextarea);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      sendChat();
                    }
                  }}
                  onInput={() => autosizeTextarea()}
                />
                <div className="absolute bottom-2 right-2 flex gap-1">
                  <button
                    className="rounded-lg p-2 text-ink-400 transition-colors hover:bg-surface-100 hover:text-ink-700"
                    title="Attach file"
                    type="button"
                  >
                    <Paperclip className="h-4 w-4" />
                  </button>
                  <button
                    className="rounded-xl bg-accent-600 p-2.5 text-white shadow-medium transition-all hover:scale-105 hover:bg-accent-700 active:scale-95 disabled:opacity-60"
                    title="Send"
                    type="button"
                    onClick={sendChat}
                    disabled={!connected || !draft.trim()}
                  >
                    <ArrowUp className="h-4 w-4" />
                  </button>
                </div>
              </div>
              <div className="mt-2 text-center text-[10px] text-ink-400">Aura may produce inaccurate information. Please verify important details.</div>
          </div>
        </main>

        <RightPanel
          rightTab={rightTab}
          setRightTab={setRightTab}
          latestPlan={latestPlan}
          planStats={planStats}
          planCount={planCount}
          activePlanIndex={activePlanIndex}
          currentPlan={currentPlan}
          hasRunningTool={hasRunningTool}
          statusLabel={statusLabel}
          statusTone={statusTone as any}
          elapsedText={fmtElapsed(elapsedMs)}
          approvals={approvals}
          approvalsCount={approvalsCount}
          refreshApprovals={() => void refreshApprovals()}
          recentArtifacts={recentArtifacts}
          artifactTexts={artifactTexts}
          ensureText={ensureText}
          events={events}
          toolRuns={toolRuns as any}
          sessionMeta={sessionMeta}
          onChangeApprovalMode={(mode) => wsSend(wsRef.current, { type: "settings", tool_approval_mode: mode })}
          onToggleStreaming={(enabled) => wsSend(wsRef.current, { type: "settings", llm_streaming: enabled })}
          fmtTime={fmtTime}
        />

      </div>

      <Modal
        open={Boolean(activeApproval)}
        title="Approval required"
        onClose={() => {
          popApproval();
        }}
        footer={
          <div className="flex justify-end gap-2">
            <Button onClick={() => decideApproval("deny")}>Deny</Button>
            <Button variant="primary" onClick={() => decideApproval("approve")}>
              Approve
            </Button>
          </div>
        }
      >
        {activeApproval ? (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3 text-sm">
              <div className="rounded-xl border border-surface-200 bg-surface-50 p-3">
                <div className="text-xs text-ink-500">Approval</div>
                <div className="mt-1 font-mono text-xs text-ink-700">{activeApproval.approval_id}</div>
              </div>
              <div className="rounded-xl border border-surface-200 bg-surface-50 p-3">
                <div className="text-xs text-ink-500">Risk</div>
                <div className="mt-1 text-sm font-semibold text-ink-900">{activeApproval.risk_level || "high"}</div>
              </div>
            </div>

            <div className="rounded-xl border border-surface-200 bg-surface-50 p-3">
              <div className="text-xs text-ink-500">Summary</div>
              <div className="mt-1 text-sm text-ink-900">{activeApproval.action_summary}</div>
              {activeApproval.reason ? <div className="mt-2 text-xs text-ink-700">{activeApproval.reason}</div> : null}
            </div>

            <div className="rounded-xl border border-surface-200 bg-surface-50 p-3">
              <div className="flex items-center justify-between">
                <div className="text-xs text-ink-500">Diff preview</div>
                {diffLoading ? <Badge tone="gray">loading</Badge> : null}
              </div>
              <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-surface-200 bg-surface-0 p-3 font-mono text-xs text-ink-700">
                {diffText || (activeApproval.diff_ref ? "Loading…" : "No diff preview.")}
              </pre>
            </div>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
