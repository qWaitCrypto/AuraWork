import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bot,
  History,
  Loader2,
  MessageCircle,
  Monitor,
  Settings,
} from "lucide-react";
import type { PlanEnvelope, PlanStep, SessionSummary, TakeoverContext, WorkspaceRecord } from "./lib/types";
import { wsSend } from "./lib/ws";
import { basename } from "./lib/path";
import { fmtElapsed, fmtTime } from "./lib/timeFormat";
import { useArtifactStore, useArtifactSessionFetched, useArtifactSessionLoading, useArtifactSessionTexts } from "./store/artifactStore";
import { useEventStore } from "./store/eventStore";
import { useUiStore } from "./store/uiStore";
import { Badge } from "./components/Badge";
import { Button } from "./components/Button";
import { Modal } from "./components/Modal";
import { Chat } from "./components/Chat";
import { RightPanel } from "./components/RightPanel";
import { Sidebar } from "./components/Sidebar";
import { Topbar } from "./components/Topbar";
import { ChatInput } from "./components/ChatInput";
import { WorkspacePickerModal } from "./components/WorkspacePickerModal";
import { ApprovalModal } from "./components/ApprovalModal";
import { SettingsModal } from "./components/SettingsModal";
import { useBrowserInput } from "./hooks/useBrowserInput";
import { useSessionWs } from "./hooks/useSessionWs";
import { useBrowserStream } from "./hooks/useBrowserStream";
import { useChatTimeline } from "./hooks/useChatTimeline";
import type { ChatMessage, ToolRun } from "./types";
import { parseWorkSpecView } from "./lib/workSpecView";
import { normalizeToolEndStatus } from "./lib/toolStatus";

type ViewMode = "work" | "stage";
type ThemeMode = "light" | "dark";

function resolveInitialTheme(): ThemeMode {
  try {
    const stored = localStorage.getItem("AURA_WEB_THEME");
    if (stored === "light" || stored === "dark") return stored;
  } catch {
  }

  try {
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
  } catch {
  }

  return "light";
}

function asRecord(raw: unknown): Record<string, unknown> | null {
  return raw && typeof raw === "object" ? (raw as Record<string, unknown>) : null;
}

function parsePlanEnvelope(raw: unknown): PlanEnvelope | null {
  const rec = asRecord(raw);
  if (!rec || !Array.isArray(rec.plan)) return null;
  const plan = rec.plan.filter((item): item is PlanStep => Boolean(item && typeof item === "object"));
  return {
    ...rec,
    plan,
  } as PlanEnvelope;
}

function parseTakeoverContextFromPayload(raw: unknown): TakeoverContext | null {
  const payload = asRecord(raw);
  if (!payload) return null;

  const sub = asRecord(payload.subagent);
  if (sub?.takeover === true) {
    const ctx = asRecord(sub.takeover_context);
    if (ctx) return ctx as TakeoverContext;
  }

  const dag = asRecord(payload.dag);
  if (dag?.takeover === true) {
    const ctx = asRecord(dag.takeover_context);
    if (ctx) return ctx as TakeoverContext;
  }

  return null;
}

function parsePendingQueueFromPayload(raw: unknown): Record<string, unknown>[] {
  const payload = asRecord(raw);
  if (!payload) return [];
  const dag = asRecord(payload.dag);
  if (!dag || !Array.isArray(dag.pending_queue)) return [];
  return dag.pending_queue.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"));
}


export default function App() {
  const clearEvents = useEventStore((s) => s.clear);
  const events = useEventStore((s) => s.events);

  const bootstrap = useUiStore((s) => s.bootstrap);
  const sessions = useUiStore((s) => s.sessions);
  const currentSessionId = useUiStore((s) => s.currentSessionId);
  const sessionMeta = useUiStore((s) => s.sessionMeta);
  const activeApproval = useUiStore((s) => s.activeApproval);
  const approvals = useUiStore((s) => s.approvals);

  const setCurrentSession = useUiStore((s) => s.setCurrentSession);
  const setSessionMeta = useUiStore((s) => s.setSessionMeta);
  const setApprovals = useUiStore((s) => s.setApprovals);

  const ensureTextRaw = useArtifactStore((s) => s.ensureText);
  const artifactTexts = useArtifactSessionTexts(currentSessionId);
  const artifactFetched = useArtifactSessionFetched(currentSessionId);
  const artifactLoading = useArtifactSessionLoading(currentSessionId);

  const ensureText = useCallback(async (locator: string) => {
    if (!currentSessionId) return null;
    return ensureTextRaw(currentSessionId, locator);
  }, [currentSessionId, ensureTextRaw]);

  const {
    connected,
    pendingUserMessage,
    wsRef,
    sendChat: sendChatWs,
    sendCompact: sendCompactWs,
    decideApprovalById: decideApprovalByIdWs,
    deleteSession: deleteSessionWs,
    refreshBootstrap,
    refreshApprovals,
  } = useSessionWs();

  const [draft, setDraft] = useState("");
  const [rightTab, setRightTab] = useState<"plan" | "files" | "terminal">("plan");
  const [nowTs, setNowTs] = useState(() => Date.now());
  const [viewMode, setViewMode] = useState<ViewMode>(() => {
    try {
      const v = localStorage.getItem("AURA_WEB_VIEW");
      return v === "stage" ? "stage" : "work";
    } catch {
      return "work";
    }
  });
  const [theme, setTheme] = useState<ThemeMode>(() => resolveInitialTheme());
  const [deleteSessionTarget, setDeleteSessionTarget] = useState<SessionSummary | null>(null);
  const [deleteSessionBusy, setDeleteSessionBusy] = useState(false);
  const [deleteSessionError, setDeleteSessionError] = useState<string | null>(null);

  const {
    browserFrameRef,
    browserFrameTick,
    browserStreamState,
    browserControl,
    browserControlFocused,
    browserImgRef,
    browserStageRef,
    browserMouseMoveRef,
    browserMouseMoveRafRef,
    browserWsRef,
    setBrowserControl,
    setBrowserControlFocused,
  } = useBrowserStream({
    viewMode,
    currentSessionId,
    approvals,
    events,
  });

  const [workspacePickerOpen, setWorkspacePickerOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const workspaces = bootstrap?.workspaces;
  const workspacesList: WorkspaceRecord[] = Array.isArray(workspaces) ? workspaces : [];

  function openWorkspacePicker() {
    setWorkspacePickerOpen(true);
  }

  const latestPlan = useMemo<PlanEnvelope | null>(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const event = events[i];
      if (event.kind !== "plan_update") continue;
      const parsed = parsePlanEnvelope(event.payload);
      if (parsed) return parsed;
    }
    return parsePlanEnvelope({ plan: sessionMeta?.plan ?? [] });
  }, [events, sessionMeta?.plan]);

  const currentPlan = useMemo<PlanStep[]>(() => {
    return latestPlan?.plan ?? [];
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

  const liveLlm = useMemo(() => {
    let open = false;
    let startedAt = 0;
    let assistantBuf = "";
    let thinkingBuf = "";
    for (const e of events) {
      if (e.kind === "llm_request_started") {
        open = true;
        startedAt = e.timestamp;
        assistantBuf = "";
        thinkingBuf = "";
        continue;
      }
      if (!open) continue;
      if (e.kind === "llm_thinking_delta") thinkingBuf += typeof e.payload.thinking_delta === "string" ? e.payload.thinking_delta : "";
      if (e.kind === "llm_response_delta") assistantBuf += typeof e.payload.text_delta === "string" ? e.payload.text_delta : "";
      if (e.kind === "llm_response_completed" || e.kind === "llm_request_failed") {
        open = false;
        startedAt = 0;
        assistantBuf = "";
        thinkingBuf = "";
      }
    }
    return {
      llmRunning: open,
      startedAt,
      assistantText: open ? assistantBuf : "",
      thinkingText: open ? thinkingBuf : "",
    };
  }, [events]);

  const liveAssistant = liveLlm.assistantText;
  const liveThinking = liveLlm.thinkingText;
  const llmRunning = liveLlm.llmRunning;
  const llmStartedAt = liveLlm.startedAt;

  const chatMessages = useMemo<ChatMessage[]>(() => {
    const out: ChatMessage[] = [];
    const llmTextLenByStep = new Map<string, number>();
    for (const e of events) {
      if (e.kind === "llm_response_delta") {
        const stepId = typeof e.step_id === "string" ? e.step_id : null;
        if (!stepId) continue;
        const delta = typeof e.payload.text_delta === "string" ? e.payload.text_delta : "";
        if (!delta) continue;
        llmTextLenByStep.set(stepId, (llmTextLenByStep.get(stepId) || 0) + delta.length);
        continue;
      }
      if (e.kind === "operation_started" && String(e.payload.op_kind || "") === "chat") {
        const ref = asRecord(e.payload.input_ref);
        if (typeof ref?.locator === "string")
          out.push({
            id: e.event_id,
            role: "user",
            ts: e.timestamp,
            locator: ref.locator,
            summary: typeof ref.summary === "string" ? ref.summary : undefined,
            requestId: e.request_id ?? null,
            turnId: e.turn_id ?? null,
          });
        continue;
      }
      if (e.kind === "llm_response_completed") {
        const ref = asRecord(e.payload.output_ref);
        const toolCalls = Array.isArray(e.payload.tool_calls) ? e.payload.tool_calls : [];
        const summary = typeof ref?.summary === "string" ? ref.summary : "";
        const finalText = typeof e.payload.final_text === "string" ? e.payload.final_text : "";
        const finalTextLen = finalText.trim().length;
        const stepId = typeof e.step_id === "string" ? e.step_id : null;
        const textLen = stepId ? (llmTextLenByStep.get(stepId) || 0) : 0;

        // Hide tool-planning turns that produced no user-facing text.
        // This avoids empty assistant bubbles for tool-call-only responses (common with OpenAI Responses).
        const hasToolCalls = toolCalls.length > 0;
        const hasUserText = textLen > 0 || finalTextLen > 0 || Boolean(summary.trim());
        if (hasToolCalls && !hasUserText) {
          continue;
        }

        const msg: ChatMessage = {
          id: e.event_id,
          role: "assistant",
          ts: e.timestamp,
          requestId: e.request_id ?? null,
          turnId: e.turn_id ?? null,
        };
        if (typeof ref?.locator === "string") msg.locator = ref.locator;
        if (summary) msg.summary = summary;
        if (finalTextLen > 0) msg.text = finalText;
        if (msg.locator || msg.text) out.push(msg);
        continue;
      }
      if (e.kind === "llm_request_failed") {
        const msg = String(e.payload.error || e.payload.message || "llm_request_failed");
        out.push({ id: e.event_id, role: "system", ts: e.timestamp, text: msg, requestId: e.request_id ?? null, turnId: e.turn_id ?? null });
        continue;
      }
      if (e.kind === "operation_failed") {
        const msg = String(e.payload.error || e.payload.message || "operation_failed");
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
      const payload = e.payload;
      const id = String(payload.tool_execution_id || payload.tool_call_id || e.event_id);
      const tool = String(payload.tool_name || "tool");
      const summary = String(payload.summary || tool);
      const workSpec = parseWorkSpecView(payload.work_spec);
      const preset = typeof payload.preset === "string" ? payload.preset : undefined;
      const subagentRunId = typeof payload.subagent_run_id === "string" ? payload.subagent_run_id : undefined;
      const browserAgentSession = typeof payload.browser_agent_session === "string" ? payload.browser_agent_session : undefined;

      if (e.kind === "tool_call_start") {
        byId.set(id, {
          id,
          tool,
          summary,
          startedAt: e.timestamp,
          status: "running",
          preset,
          subagentRunId,
          browserAgentSession,
          requestId: e.request_id ?? null,
          turnId: e.turn_id ?? null,
          workSpec,
        });
      } else {
        const prev = byId.get(id);
        const status = normalizeToolEndStatus(
          typeof payload.status === "string"
            ? payload.status
            : typeof payload.status_legacy === "string"
              ? payload.status_legacy
              : "unknown",
        );
        byId.set(id, {
          ...(prev || {
            id,
            tool,
            summary,
            startedAt: e.timestamp,
            status: "unknown",
            requestId: e.request_id ?? null,
            turnId: e.turn_id ?? null,
            workSpec,
          }),
          tool,
          summary,
          endedAt: e.timestamp,
          durationMs: typeof payload.duration_ms === "number" ? payload.duration_ms : prev?.durationMs,
          status,
          preset: preset ?? prev?.preset,
          subagentRunId: subagentRunId ?? prev?.subagentRunId,
          browserAgentSession: browserAgentSession ?? prev?.browserAgentSession,
          requestId: prev?.requestId ?? (e.request_id ?? null),
          turnId: prev?.turnId ?? (e.turn_id ?? null),
          workSpec: workSpec ?? prev?.workSpec,
        });
      }
    }
    return Array.from(byId.values())
      .sort((a, b) => a.startedAt - b.startedAt)
      .slice(-120);
  }, [events]);


  const chatItems = useChatTimeline({
    chatMessages,
    events,
    pendingUserMessage,
    toolRuns,
  });

  useEffect(() => {
    const need: string[] = [];
    for (const m of chatMessages) {
      if (!m.locator) continue;
      const loc = m.locator;
      if (artifactFetched[loc]) continue;
      if (artifactLoading[loc]) continue;
      need.push(loc);
    }
    for (const e of events) {
      if (e.kind !== "llm_response_completed") continue;
      const thinkingRef = asRecord(e.payload.thinking_ref);
      const loc = typeof thinkingRef?.locator === "string" ? thinkingRef.locator.trim() : "";
      if (!loc) continue;
      if (artifactFetched[loc]) continue;
      if (artifactLoading[loc]) continue;
      need.push(loc);
    }
    if (!need.length) return;
    for (const loc of need.slice(0, 10)) void ensureText(loc);
  }, [artifactFetched, artifactLoading, chatMessages, events, ensureText]);

  async function createSession() {
    // Open workspace picker: select existing workspace or register a new directory.
    openWorkspacePicker();
  }

  async function deleteSession(sessionId: string): Promise<boolean> {
    const sid = String(sessionId || "").trim();
    if (!sid) return false;
    try {
      await deleteSessionWs(sid);
      return true;
    } catch (error: unknown) {
      console.warn("[ui] delete session failed", error);
      return false;
    }
  }

  function requestDeleteSession(session: SessionSummary) {
    setDeleteSessionTarget(session);
    setDeleteSessionError(null);
  }

  function closeDeleteSessionModal() {
    if (deleteSessionBusy) return;
    setDeleteSessionTarget(null);
    setDeleteSessionError(null);
  }

  async function confirmDeleteSession() {
    if (!deleteSessionTarget || deleteSessionBusy) return;
    setDeleteSessionBusy(true);
    setDeleteSessionError(null);
    const ok = await deleteSession(deleteSessionTarget.session_id);
    setDeleteSessionBusy(false);
    if (ok) {
      setDeleteSessionTarget(null);
      return;
    }
    setDeleteSessionError("Failed to delete session. Please try again.");
  }

  function sendChat() {
    const text = draft.trim();
    if (!text) return;
    if (text === "/compact") {
      const ok = sendCompactWs();
      if (ok) setDraft("");
      return;
    }
    const ok = sendChatWs(text);
    if (ok) setDraft("");
  }


  function decideApprovalById(approvalId: string, decision: "approve" | "deny", note?: string) {
    decideApprovalByIdWs(approvalId, decision, note);
  }

  function decideApproval(decision: "approve" | "deny") {
    if (!activeApproval) return;
    decideApprovalByIdWs(activeApproval.approval_id, decision);
  }

  const modelProfiles = bootstrap?.model_profiles || [];
  const approvalsCount = approvals.length;

  const takeoverApproval = useMemo(() => {
    for (const approval of approvals) {
      if (parseTakeoverContextFromPayload(approval.resume_payload)) return approval;
    }
    return null;
  }, [approvals]);

  const takeoverContext = useMemo(() => {
    if (!takeoverApproval) return null;
    return parseTakeoverContextFromPayload(takeoverApproval.resume_payload);
  }, [takeoverApproval]);

  const takeoverQueue = useMemo(() => {
    if (!takeoverApproval) return [] as Record<string, unknown>[];
    return parsePendingQueueFromPayload(takeoverApproval.resume_payload);
  }, [takeoverApproval]);

  const takeoverStreamAgentSession = useMemo(() => {
    if (!takeoverContext) return null;
    const raw = typeof takeoverContext.browser_agent_session === "string"
      ? takeoverContext.browser_agent_session
      : typeof takeoverContext.agent_session === "string"
        ? takeoverContext.agent_session
        : "";
    const value = raw.trim();
    return value || null;
  }, [takeoverContext]);

  const activeApprovalIsTakeover = useMemo(() => {
    if (!activeApproval) return false;
    return Boolean(parseTakeoverContextFromPayload(activeApproval.resume_payload));
  }, [activeApproval]);

  const activeTakeoverContext = useMemo(() => {
    if (!activeApproval) return null;
    return parseTakeoverContextFromPayload(activeApproval.resume_payload);
  }, [activeApproval]);

  const currentSession = useMemo(() => {
    if (!currentSessionId) return null;
    return (sessions || []).find((s) => s.session_id === currentSessionId) ?? null;
  }, [currentSessionId, sessions]);

  const workspaceRoot = currentSession?.project_root || bootstrap?.workspaces?.[0]?.project_root || "";
  const workspaceName = basename(workspaceRoot);

  const lastEvent = events.length ? events[events.length - 1] : null;
  const hasRunningTool = toolRuns.some((t) => t.status === "running");
  const hasRunningBrowser = toolRuns.some((t) => t.tool === "browser__run" && t.status === "running");
  const lastBrowserRun = useMemo(() => {
    for (let i = toolRuns.length - 1; i >= 0; i--) {
      const t = toolRuns[i];
      if (t.tool === "browser__run") return t;
    }
    return null;
  }, [toolRuns]);
  const isPaused = approvalsCount > 0 || lastEvent?.kind === "run_paused";
  const statusLabel = isPaused ? "Paused" : hasRunningTool ? "Executing" : "Idle";
  const statusTone = isPaused ? "orange" : hasRunningTool ? "orange" : "gray";

  const startTs = events.length ? events[0].timestamp : nowTs;
  const endTs = hasRunningTool ? nowTs : lastEvent?.timestamp ?? nowTs;
  const elapsedMs = endTs - startTs;

  useEffect(() => {
    if (!hasRunningTool) return;
    setNowTs(Date.now());
    const id = window.setInterval(() => setNowTs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [hasRunningTool]);

  useEffect(() => {
    try {
      localStorage.setItem("AURA_WEB_VIEW", viewMode);
    } catch { }
  }, [viewMode]);

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = theme;
    try {
      localStorage.setItem("AURA_WEB_THEME", theme);
    } catch { }
  }, [theme]);

  useEffect(() => {
    if (!takeoverApproval) return;
    if (viewMode !== "stage") setViewMode("stage");
  }, [takeoverApproval?.approval_id, viewMode]);

  const activePlanIndex = useMemo(() => {
    for (let i = 0; i < currentPlan.length; i++) {
      const st = String(currentPlan[i]?.status || "");
      if (st === "in_progress") return i;
    }
    return -1;
  }, [currentPlan]);

  const activeTaskTitle = useMemo(() => {
    if (activePlanIndex < 0) return null;
    const it = currentPlan[activePlanIndex];
    if (!it) return null;
    return String(it.step || it.id || "").trim() || null;
  }, [activePlanIndex, currentPlan]);

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
      const diff = a.diff_ref?.locator;
      if (diff && !seen.has(diff)) {
        seen.add(diff);
        locs.push(String(diff));
      }
    }
    return locs.slice(-30).reverse();
  }, [approvals, chatMessages]);


  function openRightTabInWorkView(tab: "plan" | "terminal") {
    setRightTab(tab);
    setViewMode("work");
  }

  function openToolRunInWorkView(toolRunId: string) {
    setRightTab("terminal");
    setViewMode("work");
    // Wait for React to render the RightPanel again.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const el = document.getElementById(`toolrun_${toolRunId}`);
        if (el) el.scrollIntoView({ block: "nearest" });
      });
    });
  }

  const navButtonBase =
    "group relative flex h-10 w-10 items-center justify-center rounded-lg transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/30";
  const navButtonActive = "border border-accent-100 bg-accent-50 text-accent-600 hover:bg-accent-100";
  const navButtonInactive = "text-ink-500 hover:bg-surface-100 hover:text-ink-700";

  const browserFrameSrc = useMemo(
    () => (browserFrameRef.current.data ? `data:image/jpeg;base64,${browserFrameRef.current.data}` : null),
    [browserFrameRef, browserFrameTick],
  );
  const browserFrameMeta = useMemo(() => browserFrameRef.current.metadata, [browserFrameRef, browserFrameTick]);

  const browserInput = useBrowserInput({
    browserControl,
    browserControlFocused,
    setBrowserControl,
    setBrowserControlFocused,
    browserWsRef,
    browserImgRef,
    browserStageRef,
    browserFrameRef,
    browserMouseMoveRef,
    browserMouseMoveRafRef,
  });

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
            className={[navButtonBase, viewMode === "work" ? navButtonActive : navButtonInactive].join(" ")}
            onClick={() => setViewMode("work")}
            aria-label="Work view"
            type="button"
          >
            <MessageCircle className="h-5 w-5" />
            <div className="pointer-events-none absolute left-14 top-1/2 -translate-y-1/2 whitespace-nowrap rounded-lg bg-ink-900 px-2.5 py-1.5 text-xs font-medium text-white opacity-0 shadow-elevated transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
              Work
            </div>
          </button>
          <button
            className={[navButtonBase, viewMode === "stage" ? navButtonActive : navButtonInactive].join(" ")}
            onClick={() => setViewMode("stage")}
            aria-label="Stage view"
            type="button"
          >
            <Monitor className="h-5 w-5" />
            <div className="pointer-events-none absolute left-14 top-1/2 -translate-y-1/2 whitespace-nowrap rounded-lg bg-ink-900 px-2.5 py-1.5 text-xs font-medium text-white opacity-0 shadow-elevated transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
              Stage
            </div>
          </button>
          <button
            className={[navButtonBase, navButtonInactive, "opacity-40 cursor-not-allowed"].join(" ")}
            aria-label="History"
            title="Coming soon"
            type="button"
            disabled
          >
            <History className="h-5 w-5" />
            <div className="pointer-events-none absolute left-14 top-1/2 -translate-y-1/2 whitespace-nowrap rounded-lg bg-ink-900 px-2.5 py-1.5 text-xs font-medium text-white opacity-0 shadow-elevated transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
              History
            </div>
          </button>

          <div className="flex-1" />
          <button
            className="group relative flex h-10 w-10 items-center justify-center rounded-lg text-ink-500 transition-colors hover:bg-surface-100 hover:text-ink-700"
            aria-label="Settings"
            title="Settings"
            type="button"
            onClick={() => setSettingsOpen(true)}
          >
            <Settings className="h-5 w-5" />
            <div className="pointer-events-none absolute left-14 top-1/2 -translate-y-1/2 whitespace-nowrap rounded-lg bg-ink-900 px-2.5 py-1.5 text-xs font-medium text-white opacity-0 shadow-elevated transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
              Settings
            </div>
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
          onSelectSession={(id) => {
            clearEvents();
            setSessionMeta(null);
            setApprovals([]);
            setCurrentSession(id);
          }}
          onRequestDeleteSession={(session) => requestDeleteSession(session)}
          onOpenWorkspacePicker={() => openWorkspacePicker()}
          approvalsCount={approvalsCount}
          lastEventKind={lastEvent?.kind ?? null}
          hasRunningTool={hasRunningTool}
          liveAssistant={liveAssistant}
          eventsForCurrentSession={events}
          connected={connected}
          onCreateSession={() => void createSession()}
        />

        {viewMode === "work" ? (
          <>
            {/* Main Chat Area */}
            <main className="relative flex min-w-0 flex-1 flex-col bg-surface-0 textured-bg">
              <Topbar
                currentSessionId={currentSessionId}
                modelProfiles={modelProfiles}
                sessionMeta={sessionMeta}
                onChangeChatProfile={(profileId) => wsSend(wsRef.current, { type: "settings", chat_profile_id: profileId || undefined })}
                theme={theme}
                onToggleTheme={() => setTheme((prev) => (prev === "dark" ? "light" : "dark"))}
              />

              <Chat
                chatItems={chatItems}
                artifactTexts={artifactTexts}
                liveAssistant={liveAssistant}
                liveThinking={liveThinking}
                llmRunning={llmRunning}
                llmStartedAt={llmStartedAt}
                hasRunningTool={hasRunningTool}
                toolRuns={toolRuns}
                activeTaskTitle={activeTaskTitle}
                onPickSuggestion={(text) => setDraft(text)}
                onOpenRightTab={(tab) => openRightTabInWorkView(tab)}
                onScrollToToolRun={(toolRunId) => openToolRunInWorkView(toolRunId)}
              />

              {/* Input Area */}
              <div className="max-w-3xl mx-auto w-full">
                <ChatInput draft={draft} onDraftChange={setDraft} onSend={sendChat} disabled={!connected} />
              </div>
            </main>

            <RightPanel
              currentSessionId={currentSessionId}
              rightTab={rightTab}
              setRightTab={setRightTab}
              latestPlan={latestPlan}
              planStats={planStats}
              planCount={planCount}
              activePlanIndex={activePlanIndex}
              currentPlan={currentPlan}
              hasRunningTool={hasRunningTool}
              statusLabel={statusLabel}
              statusTone={statusTone}
              elapsedText={fmtElapsed(elapsedMs)}
              approvals={approvals}
              approvalsCount={approvalsCount}
              refreshApprovals={() => void refreshApprovals()}
              recentArtifacts={recentArtifacts}
              artifactTexts={artifactTexts}
              ensureText={ensureText}
              events={events}
              toolRuns={toolRuns}
              sessionMeta={sessionMeta}
              onChangeApprovalMode={(mode) => wsSend(wsRef.current, { type: "settings", tool_approval_mode: mode })}
              onToggleStreaming={(enabled) => wsSend(wsRef.current, { type: "settings", llm_streaming: enabled })}
            />
          </>
        ) : (
          // Stage View: large canvas/browser area + chat dock
          <main className="relative flex min-w-0 flex-1 flex-col bg-surface-0">
            <Topbar
              currentSessionId={currentSessionId}
              modelProfiles={modelProfiles}
              sessionMeta={sessionMeta}
              onChangeChatProfile={(profileId) => wsSend(wsRef.current, { type: "settings", chat_profile_id: profileId || undefined })}
                theme={theme}
                onToggleTheme={() => setTheme((prev) => (prev === "dark" ? "light" : "dark"))}
            />

            <div className="flex min-h-0 flex-1 overflow-hidden">
              <section className="flex min-w-0 flex-1 flex-col bg-surface-0">
                <div className="flex items-center justify-between border-b border-surface-200 bg-surface-0/80 px-4 py-3 backdrop-blur-sm">
                  <div className="min-w-0">
                    <div className="text-sm font-semibold text-ink-900">Stage</div>
                    <div className="mt-0.5 truncate font-mono text-[10px] text-ink-500">
                      {activeTaskTitle ? `Step: ${activeTaskTitle}` : "No active step"}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge tone={statusTone}>{statusLabel}</Badge>
                    {planStats.total ? <Badge tone="gray">{planStats.percent}%</Badge> : null}
                    {hasRunningBrowser ? <Badge tone="orange">browser</Badge> : null}
                    <Button onClick={() => setViewMode("work")} title="Back to Work view">
                      Work
                    </Button>
                  </div>
                </div>

                {takeoverApproval ? (
                  <div className="border-b border-amber-200 bg-amber-50 px-4 py-2">
                    <div className="flex items-center justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <div className="text-xs font-semibold text-amber-800">Human takeover required</div>
                          {takeoverQueue.length > 0 ? (
                            <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800">queue +{takeoverQueue.length}</span>
                          ) : null}
                        </div>
                        <div className="truncate text-[11px] text-amber-700">
                          {(takeoverApproval.action_summary || "Complete CAPTCHA/login in the browser view, then resume.") as string}
                        </div>
                        {typeof takeoverContext?.current_url === "string" && takeoverContext.current_url ? (
                          <div className="truncate font-mono text-[10px] text-amber-700/80">{String(takeoverContext.current_url)}</div>
                        ) : null}
                        {typeof takeoverStreamAgentSession === "string" && takeoverStreamAgentSession ? (
                          <div className="truncate font-mono text-[10px] text-amber-700/70">session: {takeoverStreamAgentSession}</div>
                        ) : null}
                      </div>
                      <div className="flex items-center gap-2">
                        <Button
                          variant="primary"
                          onClick={() => {
                            decideApprovalById(takeoverApproval.approval_id, "approve", "user_takeover_completed");
                          }}
                        >
                          Resume
                        </Button>
                        <Button
                          onClick={() => {
                            decideApprovalById(takeoverApproval.approval_id, "deny", "user_takeover_cancelled");
                          }}
                        >
                          Cancel
                        </Button>
                      </div>
                    </div>
                  </div>
                ) : null}

                {/* Browser viewport */}
                <div className="flex min-h-0 flex-1 flex-col bg-surface-50">
                  {/* Compact top bar */}
                  <div className="flex items-center justify-between border-b border-surface-200 bg-surface-0 px-4 py-2">
                    <div className="flex items-center gap-3">
                      {/* Connection indicator */}
                      <div className="relative">
                        <span className={`block h-2 w-2 rounded-full ${browserStreamState.wsOpen ? "bg-emerald-500" : "bg-surface-300"}`} />
                        {browserStreamState.wsOpen && (
                          <span className="absolute inset-0 animate-ping rounded-full bg-emerald-500 opacity-40" />
                        )}
                      </div>
                      {/* Resolution and timestamp */}
                      <span className="font-mono text-xs text-ink-600">
                        {browserFrameMeta?.deviceWidth && browserFrameMeta?.deviceHeight
                          ? `${browserFrameMeta.deviceWidth}×${browserFrameMeta.deviceHeight}`
                          : "Live"}
                      </span>
                      {browserStreamState.lastFrameAt && (
                        <span className="text-[10px] text-ink-400">{fmtTime(browserStreamState.lastFrameAt)}</span>
                      )}
                      {browserStreamState.agentSession ? (
                        <span className="max-w-[220px] truncate font-mono text-[10px] text-ink-400">{browserStreamState.agentSession}</span>
                      ) : null}
                    </div>
                    <div className="flex items-center gap-2">
                      {/* Observe/Control switch */}
                      <div className="flex rounded-full bg-surface-100 p-0.5">
                        <button
                          className={`rounded-full px-3 py-1 text-[11px] font-medium transition-all duration-200 ${!browserControl
                            ? "bg-white text-ink-900 shadow-sm"
                            : "text-ink-500 hover:text-ink-700"
                            }`}
                          onClick={() => { setBrowserControl(false); setBrowserControlFocused(false); }}
                          disabled={!browserFrameSrc}
                        >
                          Observe
                        </button>
                        <button
                          className={`rounded-full px-3 py-1 text-[11px] font-medium transition-all duration-200 ${browserControl
                            ? "bg-amber-500 text-white shadow-sm"
                            : "text-ink-500 hover:text-ink-700"
                            }`}
                          onClick={() => { setBrowserControl(true); queueMicrotask(() => browserInput.focusBrowserControl()); }}
                          disabled={!browserFrameSrc}
                        >
                          Control
                        </button>
                      </div>
                      {/* Icon controls */}
                      <button
                        className="flex h-7 w-7 items-center justify-center rounded-lg text-ink-400 transition-colors hover:bg-surface-100 hover:text-ink-700"
                        onClick={() => openRightTabInWorkView("plan")}
                        title="View Plan"
                      >
                        <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" /></svg>
                      </button>
                      <button
                        className="flex h-7 w-7 items-center justify-center rounded-lg text-ink-400 transition-colors hover:bg-surface-100 hover:text-ink-700"
                        onClick={() => openRightTabInWorkView("terminal")}
                        title="View Log"
                      >
                        <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" /></svg>
                      </button>
                    </div>
                  </div>

                  {/* Browser frame */}
                  <div className="relative flex min-h-0 flex-1 items-center justify-center p-6">
                    {browserFrameSrc ? (
                      <>
                        {/* Frame container */}
                        <div
                          ref={browserStageRef}
                          tabIndex={browserControl ? 0 : -1}
                          className={`relative flex items-center justify-center outline-none touch-none ${browserControl ? "cursor-crosshair" : "cursor-default"}`}
                          onPointerMove={browserInput.onPointerMove}
                          onPointerDown={browserInput.onPointerDown}
                          onPointerUp={browserInput.onPointerUp}
                          onPointerCancel={() => { }}
                          onWheel={browserInput.onWheel}
                          onKeyDown={browserInput.onKeyDown}
                          onKeyUp={browserInput.onKeyUp}
                          onBlur={() => setBrowserControlFocused(false)}
                          onFocus={() => setBrowserControlFocused(true)}
                        >
                          <img
                            ref={browserImgRef}
                            src={browserFrameSrc}
                            alt="Browser preview"
                            className="max-h-full max-w-full select-none rounded-xl border border-surface-200 shadow-elevated"
                            draggable={false}
                          />
                        </div>
                        {/* Control mode hint */}
                        {browserControl && (
                          <div className="absolute bottom-8 right-8 rounded-full bg-ink-900/80 px-4 py-2 text-xs font-medium text-white backdrop-blur-sm">
                            {browserControlFocused ? "🎮 Active · Esc to exit" : "Click viewport to control"}
                          </div>
                        )}
                      </>
                    ) : (
                      /* Waiting state */
                      <div className="flex flex-col items-center gap-4">
                        <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-surface-100">
                          <svg className="h-8 w-8 text-ink-300" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z" />
                          </svg>
                        </div>
                        <div className="text-center">
                          <div className="text-sm font-medium text-ink-600">Browser not active</div>
                          <div className="mt-1 text-xs text-ink-400">Run a browser task to see live preview</div>
                        </div>
                      </div>
                    )}
                  </div>

                  {/* Bottom activity status */}
                  {lastBrowserRun && (
                    <div className="flex items-center gap-3 border-t border-surface-200 bg-surface-0 px-4 py-2">
                      <span className={`h-1.5 w-1.5 rounded-full ${lastBrowserRun.status === "running" ? "animate-pulse bg-amber-500" : "bg-emerald-500"}`} />
                      <code className="flex-1 truncate font-mono text-[11px] text-ink-500">
                        {lastBrowserRun.summary}
                      </code>
                      <span className={`text-[10px] font-medium ${lastBrowserRun.status === "running" ? "text-amber-600" : "text-emerald-600"}`}>
                        {lastBrowserRun.status}
                      </span>
                      {typeof lastBrowserRun.durationMs === "number" && (
                        <span className="text-[10px] text-ink-400">{lastBrowserRun.durationMs}ms</span>
                      )}
                    </div>
                  )}
                </div>
              </section>

              <section className="flex w-[420px] flex-shrink-0 flex-col border-l border-surface-200 bg-surface-0 textured-bg">
                <Chat
                  chatItems={chatItems}
                  artifactTexts={artifactTexts}
                  liveAssistant={liveAssistant}
                  liveThinking={liveThinking}
                  llmRunning={llmRunning}
                  llmStartedAt={llmStartedAt}
                  hasRunningTool={hasRunningTool}
                  toolRuns={toolRuns}
                  activeTaskTitle={activeTaskTitle}
                  onPickSuggestion={(text) => setDraft(text)}
                  onOpenRightTab={(tab) => openRightTabInWorkView(tab)}
                  onScrollToToolRun={(toolRunId) => openToolRunInWorkView(toolRunId)}
                />

                <ChatInput draft={draft} onDraftChange={setDraft} onSend={sendChat} disabled={!connected} />
              </section>
            </div>
          </main>
        )
        }

      </div>

      <WorkspacePickerModal
        open={workspacePickerOpen}
        workspaces={workspacesList}
        onClose={() => setWorkspacePickerOpen(false)}
        onSessionCreated={(sid) => { setCurrentSession(sid); setWorkspacePickerOpen(false); }}
        refreshBootstrap={refreshBootstrap}
      />

      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />

      <Modal
        open={Boolean(deleteSessionTarget)}
        title="Delete session"
        onClose={closeDeleteSessionModal}
        dismissible={!deleteSessionBusy}
        footer={(
          <div className="flex items-center justify-end gap-2">
            <Button type="button" variant="ghost" onClick={closeDeleteSessionModal} disabled={deleteSessionBusy}>
              Cancel
            </Button>
            <Button
              type="button"
              className="border border-rose-200 bg-rose-600 text-white hover:bg-rose-700 disabled:opacity-60"
              onClick={() => void confirmDeleteSession()}
              disabled={deleteSessionBusy}
            >
              {deleteSessionBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
              Delete
            </Button>
          </div>
        )}
      >
        <div className="space-y-3 text-sm text-ink-700">
          <p>Delete this session permanently? This action cannot be undone.</p>
          <div className="rounded-xl border border-surface-200 bg-surface-50 p-3">
            <div className="font-mono text-xs text-ink-800">{deleteSessionTarget?.session_id || ""}</div>
            {deleteSessionTarget?.project_root ? (
              <div className="mt-1 truncate font-mono text-[11px] text-ink-500" title={deleteSessionTarget.project_root}>
                {deleteSessionTarget.project_root}
              </div>
            ) : null}
          </div>
          {deleteSessionError ? (
            <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">{deleteSessionError}</div>
          ) : null}
        </div>
      </Modal>

      <ApprovalModal
        approval={activeApprovalIsTakeover ? null : activeApproval}
        currentSessionId={currentSessionId}
        onDecide={decideApproval}
      />
    </div>
  );
}
