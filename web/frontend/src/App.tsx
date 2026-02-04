import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowUp,
  Bot,
  History,
  Loader2,
  MessageCircle,
  Monitor,
  Paperclip,
  Plus,
  Settings,
} from "lucide-react";
import { apiBootstrap, apiCreateSession, apiDeleteSession, apiFetchArtifact, apiGetApprovals, apiGetSession, apiRegisterWorkspace } from "./lib/api";
import { isDesktop, waitForBackendGlobals } from "./lib/backendBase";
import type { ApprovalRecord, AuraEvent, SessionSummary, WorkspaceRecord } from "./lib/types";
import { connectBrowserStreamWs, connectSessionWs, wsSend, type BrowserStreamMsg, type ServerMsg } from "./lib/ws";
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

type ViewMode = "work" | "stage";


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

  const ensureTextRaw = useArtifactStore((s) => s.ensureText);
  const primeArtifactText = useArtifactStore((s) => s.primeText);
  const artifactTextsRaw = useArtifactStore((s) => s.texts);
  const artifactFetchedRaw = useArtifactStore((s) => s.fetched);
  const artifactLoadingRaw = useArtifactStore((s) => s.loading);

  const artifactTexts = useMemo<Record<string, string>>(() => {
    if (!currentSessionId) return {};
    const prefix = `${currentSessionId}::`;
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(artifactTextsRaw)) {
      if (!k.startsWith(prefix)) continue;
      out[k.slice(prefix.length)] = v;
    }
    return out;
  }, [artifactTextsRaw, currentSessionId]);

  const artifactFetched = useMemo<Record<string, boolean>>(() => {
    if (!currentSessionId) return {};
    const prefix = `${currentSessionId}::`;
    const out: Record<string, boolean> = {};
    for (const [k, v] of Object.entries(artifactFetchedRaw)) {
      if (!k.startsWith(prefix)) continue;
      out[k.slice(prefix.length)] = Boolean(v);
    }
    return out;
  }, [artifactFetchedRaw, currentSessionId]);

  const artifactLoading = useMemo<Record<string, boolean>>(() => {
    if (!currentSessionId) return {};
    const prefix = `${currentSessionId}::`;
    const out: Record<string, boolean> = {};
    for (const [k, v] of Object.entries(artifactLoadingRaw)) {
      if (!k.startsWith(prefix)) continue;
      out[k.slice(prefix.length)] = v;
    }
    return out;
  }, [artifactLoadingRaw, currentSessionId]);

  const ensureText = async (locator: string) => {
    if (!currentSessionId) return null;
    return ensureTextRaw(currentSessionId, locator);
  };



  const wsRef = useRef<WebSocket | null>(null);
  const browserWsRef = useRef<WebSocket | null>(null);
  const currentSessionIdRef = useRef<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [draft, setDraft] = useState("");
  const [pendingUserMessage, setPendingUserMessage] = useState<{ id: string; text: string; ts: number } | null>(null);
  const [diffText, setDiffText] = useState<string | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [rightTab, setRightTab] = useState<"plan" | "files" | "terminal">("plan");
  const [tick, setTick] = useState(0);
  const [viewMode, setViewMode] = useState<ViewMode>(() => {
    try {
      const v = localStorage.getItem("AURA_WEB_VIEW");
      return v === "stage" ? "stage" : "work";
    } catch {
      return "work";
    }
  });
  const browserFrameRef = useRef<{ data: string; metadata: any; ts: number }>({ data: "", metadata: null, ts: 0 });
  const browserFrameRafRef = useRef<number | null>(null);
  const [browserFrameTick, setBrowserFrameTick] = useState(0);
  const [browserStreamState, setBrowserStreamState] = useState<{
    wsOpen: boolean;
    upstreamPort?: number;
    agentSession?: string;
    lastError?: string;
    lastStatusAt?: number;
    lastFrameAt?: number;
  }>({ wsOpen: false });
  const [browserControl, setBrowserControl] = useState(false);
  const [browserControlFocused, setBrowserControlFocused] = useState(false);
  const browserImgRef = useRef<HTMLImageElement | null>(null);
  const browserStageRef = useRef<HTMLDivElement | null>(null);
  const browserMouseMoveRef = useRef<{ x: number; y: number; modifiers: number } | null>(null);
  const browserMouseMoveRafRef = useRef<number | null>(null);

  const [workspacePickerOpen, setWorkspacePickerOpen] = useState(false);
  const [workspacePathDraft, setWorkspacePathDraft] = useState("");
  const [workspaceBusy, setWorkspaceBusy] = useState(false);
  const [workspaceErr, setWorkspaceErr] = useState<string | null>(null);

  const workspaces = (bootstrap as any)?.workspaces as WorkspaceRecord[] | undefined;
  const workspacesList: WorkspaceRecord[] = Array.isArray(workspaces) ? workspaces : [];

  function openWorkspacePicker() {
    setWorkspaceErr(null);
    setWorkspacePickerOpen(true);
  }

  async function createSessionInWorkspace(workspaceId: string) {
    const wid = String(workspaceId || "").trim();
    if (!wid) return;
    setWorkspaceBusy(true);
    setWorkspaceErr(null);
    try {
      const { session_id } = await apiCreateSession(wid);
      await refreshBootstrap();
      setCurrentSession(session_id);
      setWorkspacePickerOpen(false);
    } catch (e: any) {
      setWorkspaceErr(String(e?.message || e || "create_session_failed"));
    } finally {
      setWorkspaceBusy(false);
    }
  }

  async function registerAndCreateSession(projectRoot: string) {
    const pr = String(projectRoot || "").trim();
    if (!pr) return;
    setWorkspaceBusy(true);
    setWorkspaceErr(null);
    try {
      const ws = (await apiRegisterWorkspace(pr)) as WorkspaceRecord;
      setWorkspacePathDraft("");
      await createSessionInWorkspace(ws.workspace_id);
    } catch (e: any) {
      setWorkspaceErr(String(e?.message || e || "register_workspace_failed"));
      setWorkspaceBusy(false);
    }
  }

  useEffect(() => {
    if (!workspacePickerOpen) return;
    refreshBootstrap().catch(() => { });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspacePickerOpen]);

  function workspaceLabel(w: WorkspaceRecord) {
    const root = String(w.project_root || "");
    return basename(root);
  }

  function workspaceSubtitle(w: WorkspaceRecord) {
    return String(w.project_root || "");
  }

  function fmtTs(ts: number | null | undefined) {
    if (typeof ts !== "number") return "";
    try {
      return new Date(ts).toLocaleString();
    } catch {
      return "";
    }
  }

  const wsEventQueueRef = useRef<AuraEvent[]>([]);
  const wsFlushScheduledRef = useRef(false);
  const liveStreamRef = useRef<{ open: boolean; text: string; thinking: string; startedAt: number }>({
    open: false,
    text: "",
    thinking: "",
    startedAt: 0,
  });

  function uiDebugEnabled() {
    try {
      return localStorage.getItem("AURA_WEB_DEBUG") === "1";
    } catch {
      return false;
    }
  }

  useEffect(() => {
    currentSessionIdRef.current = currentSessionId;
  }, [currentSessionId]);

  function bumpBrowserFrame() {
    if (browserFrameRafRef.current != null) return;
    browserFrameRafRef.current = requestAnimationFrame(() => {
      browserFrameRafRef.current = null;
      setBrowserFrameTick((t) => t + 1);
    });
  }

  function flushWsEvents() {
    wsFlushScheduledRef.current = false;
    const batch = wsEventQueueRef.current;
    if (!batch.length) return;
    wsEventQueueRef.current = [];

    // Maintain a simple streaming buffer so we can "pin" the streamed output to the
    // server-provided artifact locator when the terminal event arrives.
    const stream = liveStreamRef.current;
    const debug = uiDebugEnabled();
    for (const e of batch) {
      if (e.kind === "llm_request_started") {
        stream.open = true;
        stream.startedAt = e.timestamp;
        stream.text = "";
        stream.thinking = "";
        if (debug) console.debug("[ui] llm_request_started", { event_id: e.event_id, request_id: e.request_id, turn_id: e.turn_id });
        continue;
      }

      if (e.kind === "llm_response_delta") {
        if (!stream.open) {
          stream.open = true;
          stream.startedAt = e.timestamp;
          stream.text = "";
          stream.thinking = "";
          if (debug) console.warn("[ui] llm_response_delta without llm_request_started (recovering)", { event_id: e.event_id });
        }
        stream.text += String((e.payload as any).text_delta || "");
        continue;
      }
      if (e.kind === "llm_thinking_delta") {
        if (!stream.open) {
          stream.open = true;
          stream.startedAt = e.timestamp;
          stream.text = "";
          stream.thinking = "";
          if (debug) console.warn("[ui] llm_thinking_delta without llm_request_started (recovering)", { event_id: e.event_id });
        }
        stream.thinking += String((e.payload as any).thinking_delta || "");
        continue;
      }

      if (e.kind === "llm_response_completed") {
        stream.open = false;
        const p = e.payload as any;
        const outRef = p?.output_ref;
        const locator = outRef?.locator;
        const toolCallsLen = Array.isArray(p?.tool_calls) ? p.tool_calls.length : 0;
        const text = stream.text;
        if (debug) {
          console.debug("[ui] llm_response_completed", {
            event_id: e.event_id,
            provider_kind: p?.provider_kind,
            model: p?.model,
            locator,
            text_len: text.length,
            thinking_len: stream.thinking.length,
            tool_calls_len: toolCallsLen,
          });
        }
        const sid = currentSessionIdRef.current;
        if (sid && typeof locator === "string" && locator.trim() && text) {
          primeArtifactText(sid, locator, text);
        } else if (debug) {
          const level = toolCallsLen > 0 && !text ? "debug" : "warn";
          (console as any)[level]("[ui] did not prime artifact text", { sid, locator, text_len: text.length, tool_calls_len: toolCallsLen });
        }
        continue;
      }

      if (e.kind === "llm_request_failed") {
        stream.open = false;
        if (debug) console.warn("[ui] llm_request_failed", { event_id: e.event_id, payload: e.payload });
        continue;
      }
    }

    appendMany(batch);

    // Clear optimistic chat message when the server acknowledges a chat op.
    for (const e of batch) {
      if (e.kind === "operation_started" && String((e.payload as any)?.op_kind || "") === "chat") {
        setPendingUserMessage(null);
        break;
      }
    }

    // Refresh approvals at most once per batch.
    for (const e of batch) {
      if (e.kind === "approval_required" || e.kind === "run_paused") {
        refreshApprovals().catch(() => { });
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
    return b;
  }

  async function refreshApprovals() {
    if (!currentSessionId) return;
    const a = await apiGetApprovals(currentSessionId);
    setApprovals(a);
  }

  useEffect(() => {
    waitForBackendGlobals()
      .catch(() => { })
      .finally(() => {
        refreshBootstrap().catch(() => { });
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!currentSessionId) {
      // Ensure we close any previous session sockets when the active session becomes null
      // (e.g. deleting the last session).
      try {
        wsRef.current?.close();
      } catch { }
      wsRef.current = null;
      try {
        browserWsRef.current?.close();
      } catch { }
      browserWsRef.current = null;
      setConnected(false);
      setPendingUserMessage(null);
      clearEvents();
      setSessionMeta(null);
      setApprovals([]);
      liveStreamRef.current.open = false;
      liveStreamRef.current.text = "";
      liveStreamRef.current.thinking = "";
      liveStreamRef.current.startedAt = 0;
      browserFrameRef.current = { data: "", metadata: null, ts: 0 };
      setBrowserFrameTick(0);
      setBrowserStreamState({ wsOpen: false });
      setBrowserControl(false);
      setBrowserControlFocused(false);
      return;
    }
    clearEvents();
    setSessionMeta(null);
    setApprovals([]);
    setConnected(false);
    setPendingUserMessage(null);
    liveStreamRef.current.open = false;
    liveStreamRef.current.text = "";
    liveStreamRef.current.thinking = "";
    liveStreamRef.current.startedAt = 0;
    browserFrameRef.current = { data: "", metadata: null, ts: 0 };
    setBrowserFrameTick(0);
    setBrowserStreamState({ wsOpen: false });

    if (wsRef.current) {
      try {
        wsRef.current.close();
      } catch { }
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
      .catch(() => { });
    refreshApprovals().catch(() => { });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentSessionId]);

  useEffect(() => {
    // Browser stream is only used in Stage view.
    if (viewMode !== "stage") {
      if (browserWsRef.current) {
        try {
          browserWsRef.current.close();
        } catch { }
        browserWsRef.current = null;
      }
      setBrowserControl(false);
      return;
    }
    if (!currentSessionId) return;

    // Reset UI state (keep last frame so switching back is instant).
    setBrowserStreamState((s) => ({ ...s, wsOpen: false, lastError: undefined }));

    const ws = connectBrowserStreamWs(currentSessionId, (msg: BrowserStreamMsg) => {
      if (msg.type === "frame" && typeof (msg as any).data === "string") {
        browserFrameRef.current = { data: (msg as any).data, metadata: (msg as any).metadata ?? null, ts: Date.now() };
        setBrowserStreamState((s) => ({ ...s, lastFrameAt: Date.now(), lastError: undefined }));
        bumpBrowserFrame();
        return;
      }
      if (msg.type === "status") {
        setBrowserStreamState((s) => ({
          ...s,
          upstreamPort: typeof (msg as any).port === "number" ? (msg as any).port : s.upstreamPort,
          agentSession: typeof (msg as any).agent_session === "string" ? (msg as any).agent_session : s.agentSession,
          lastStatusAt: Date.now(),
          lastError: undefined,
        }));
        return;
      }
      if (msg.type === "error") {
        const m = String((msg as any).message || "browser_stream_error");
        setBrowserStreamState((s) => ({ ...s, lastError: m }));
        return;
      }
    });

    browserWsRef.current = ws;
    ws.addEventListener("open", () => setBrowserStreamState((s) => ({ ...s, wsOpen: true, lastError: undefined })));
    ws.addEventListener("close", () => setBrowserStreamState((s) => ({ ...s, wsOpen: false })));

    return () => {
      try {
        ws.close();
      } catch { }
      if (browserWsRef.current === ws) browserWsRef.current = null;
    };
  }, [viewMode, currentSessionId]);

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
      if (e.kind === "llm_thinking_delta") thinkingBuf += String((e.payload as any).thinking_delta || "");
      if (e.kind === "llm_response_delta") assistantBuf += String((e.payload as any).text_delta || "");
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

  const chatMessages = useMemo<ChatMessage[]>(() => {
    const out: ChatMessage[] = [];
    const llmTextLenByStep = new Map<string, number>();
    for (const e of events) {
      if (e.kind === "llm_response_delta") {
        const stepId = typeof e.step_id === "string" ? e.step_id : null;
        if (!stepId) continue;
        const delta = String((e.payload as any)?.text_delta || "");
        if (!delta) continue;
        llmTextLenByStep.set(stepId, (llmTextLenByStep.get(stepId) || 0) + delta.length);
        continue;
      }
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
        const payload = e.payload as any;
        const ref = payload?.output_ref;
        const toolCalls = Array.isArray(payload?.tool_calls) ? payload.tool_calls : [];
        const summary = typeof ref?.summary === "string" ? ref.summary : "";
        const stepId = typeof e.step_id === "string" ? e.step_id : null;
        const textLen = stepId ? (llmTextLenByStep.get(stepId) || 0) : 0;

        // Hide tool-planning turns that produced no user-facing text.
        // This avoids empty assistant bubbles for tool-call-only responses (common with OpenAI Responses).
        const hasToolCalls = toolCalls.length > 0;
        const hasUserText = textLen > 0 || Boolean(summary.trim());
        if (hasToolCalls && !hasUserText) {
          continue;
        }

        if (ref?.locator) {
          out.push({
            id: e.event_id,
            role: "assistant",
            ts: e.timestamp,
            locator: String(ref.locator),
            summary: summary || undefined,
            requestId: e.request_id ?? null,
            turnId: e.turn_id ?? null,
          });
        }
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
    if (pendingUserMessage) {
      msgs.push({
        ts: pendingUserMessage.ts,
        item: {
          kind: "message",
          msg: {
            id: pendingUserMessage.id,
            role: "user",
            ts: pendingUserMessage.ts,
            text: pendingUserMessage.text,
          },
        },
      });
    }

    msgs.sort((a, b) => a.ts - b.ts);

    // De-duplicate: if timeline is too close and empty, keep as is.
    for (const it of msgs) items.push(it.item);

    return items;
  }, [chatMessages, events, pendingUserMessage, toolRuns]);

  useEffect(() => {
    const need: string[] = [];
    for (const m of chatMessages) {
      if (!m.locator) continue;
      const loc = m.locator;
      if (artifactFetched[loc]) continue;
      if (artifactLoading[loc]) continue;
      need.push(loc);
    }
    if (!need.length) return;
    for (const loc of need.slice(0, 10)) void ensureText(loc);
  }, [artifactFetched, artifactLoading, chatMessages, ensureText]);

  async function createSession() {
    // Open workspace picker: select existing workspace or register a new directory.
    openWorkspacePicker();
  }

  async function deleteSession(sessionId: string) {
    const sid = String(sessionId || "").trim();
    if (!sid) return;
    try {
      await apiDeleteSession(sid);
      const b = await refreshBootstrap();
      if (currentSessionIdRef.current === sid) {
        const next = (b.sessions || []).find((s) => s.session_id !== sid)?.session_id || null;
        setCurrentSession(next);
      }
    } catch (e: any) {
      // Surface the failure to the console for now (UI toast system is not in place yet).
      console.warn("[ui] delete session failed", e);
    }
  }

  function sendChat() {
    const text = draft.trim();
    if (!text) return;
    setPendingUserMessage({ id: `pending_${Date.now()}`, text, ts: Date.now() });
    if (!wsSend(wsRef.current, { type: "chat", text })) {
      setPendingUserMessage(null);
      return;
    }
    setDraft("");
  }

  async function loadDiff(rec: ApprovalRecord) {
    setDiffText(null);
    const diffRef = (rec as any).diff_ref;
    if (!diffRef?.locator) return;
    setDiffLoading(true);
    try {
      if (!currentSessionId) throw new Error("no session");
      setDiffText(await apiFetchArtifact(currentSessionId, String(diffRef.locator)));
    } catch {
      setDiffText("(Failed to load diff preview)");
    } finally {
      setDiffLoading(false);
    }
  }

  useEffect(() => {
    if (activeApproval) loadDiff(activeApproval).catch(() => { });
    else setDiffText(null);
  }, [activeApproval]);

  function decideApproval(decision: "approve" | "deny") {
    if (!activeApproval || !currentSessionId) return;
    wsSend(wsRef.current, { type: "approval", approval_id: activeApproval.approval_id, decision });
    popApproval();
    refreshApprovals().catch(() => { });
  }

  const modelProfiles = bootstrap?.model_profiles || [];
  const approvalsCount = approvals.length;

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

  const startTs = events.length ? events[0].timestamp : Date.now();
  const endTs = hasRunningTool ? Date.now() : lastEvent?.timestamp ?? Date.now();
  const elapsedMs = endTs - startTs + tick * 0;

  useEffect(() => {
    if (!hasRunningTool) return;
    const id = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, [hasRunningTool]);

  useEffect(() => {
    try {
      localStorage.setItem("AURA_WEB_VIEW", viewMode);
    } catch { }
  }, [viewMode]);

  const activePlanIndex = useMemo(() => {
    for (let i = 0; i < currentPlan.length; i++) {
      const st = String((currentPlan[i] as any)?.status || "");
      if (st === "in_progress") return i;
    }
    return -1;
  }, [currentPlan]);

  const activeTaskTitle = useMemo(() => {
    if (activePlanIndex < 0) return null;
    const it: any = currentPlan[activePlanIndex];
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

  const browserFrameSrc = browserFrameRef.current.data ? `data:image/jpeg;base64,${browserFrameRef.current.data}` : null;
  const browserFrameMeta = browserFrameRef.current.metadata;
  const browserFrameTs = browserFrameRef.current.ts;
  const _browserFrameTick = browserFrameTick;

  function browserModifiers(e: { altKey?: boolean; ctrlKey?: boolean; metaKey?: boolean; shiftKey?: boolean }): number {
    // Chrome DevTools Protocol modifiers: Alt=1, Ctrl=2, Meta=4, Shift=8
    return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
  }

  function browserDevicePointFromClient(clientX: number, clientY: number): { x: number; y: number } | null {
    const img = browserImgRef.current;
    if (!img) return null;

    const meta = browserFrameRef.current.metadata;
    const deviceWidth = Number(meta?.deviceWidth || img.naturalWidth || 0);
    const deviceHeight = Number(meta?.deviceHeight || img.naturalHeight || 0);
    if (!deviceWidth || !deviceHeight) return null;

    const rect = img.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    const relX = (clientX - rect.left) / rect.width;
    const relY = (clientY - rect.top) / rect.height;
    if (!Number.isFinite(relX) || !Number.isFinite(relY)) return null;
    if (relX < 0 || relY < 0 || relX > 1 || relY > 1) return null;

    const x = Math.round(relX * deviceWidth);
    const y = Math.round(relY * deviceHeight);
    if (x < 0 || y < 0 || x > deviceWidth || y > deviceHeight) return null;
    return { x, y };
  }

  function sendBrowserMouseEvent(payload: any) {
    const ws = browserWsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try {
      ws.send(JSON.stringify(payload));
    } catch { }
  }

  function sendBrowserKeyboardEvent(payload: any) {
    const ws = browserWsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try {
      ws.send(JSON.stringify(payload));
    } catch { }
  }

  function focusBrowserControl() {
    const el = browserStageRef.current;
    if (!el) return;
    try {
      el.focus();
      setBrowserControlFocused(true);
    } catch { }
  }

  function onBrowserMouseMove(e: React.MouseEvent) {
    if (!browserControl) return;
    const p = browserDevicePointFromClient(e.clientX, e.clientY);
    if (!p) return;
    browserMouseMoveRef.current = { x: p.x, y: p.y, modifiers: browserModifiers(e) };
    if (browserMouseMoveRafRef.current != null) return;
    browserMouseMoveRafRef.current = requestAnimationFrame(() => {
      browserMouseMoveRafRef.current = null;
      const cur = browserMouseMoveRef.current;
      if (!cur) return;
      sendBrowserMouseEvent({ type: "input_mouse", eventType: "mouseMoved", x: cur.x, y: cur.y, modifiers: cur.modifiers });
    });
  }

  function sendBrowserClick(e: React.MouseEvent) {
    if (!browserControl) return;
    focusBrowserControl();
    const p = browserDevicePointFromClient(e.clientX, e.clientY);
    if (!p) return;
    const modifiers = browserModifiers(e);
    sendBrowserMouseEvent({ type: "input_mouse", eventType: "mouseMoved", x: p.x, y: p.y, modifiers });
    sendBrowserMouseEvent({ type: "input_mouse", eventType: "mousePressed", x: p.x, y: p.y, button: "left", clickCount: 1, modifiers });
    sendBrowserMouseEvent({ type: "input_mouse", eventType: "mouseReleased", x: p.x, y: p.y, button: "left", clickCount: 1, modifiers });
  }

  function sendBrowserWheel(e: React.WheelEvent) {
    if (!browserControl) return;
    focusBrowserControl();
    const p = browserDevicePointFromClient(e.clientX, e.clientY);
    if (!p) return;
    e.preventDefault();
    const modifiers = browserModifiers(e);
    const deltaX = Number.isFinite(e.deltaX) ? e.deltaX : 0;
    const deltaY = Number.isFinite(e.deltaY) ? e.deltaY : 0;
    sendBrowserMouseEvent({ type: "input_mouse", eventType: "mouseWheel", x: p.x, y: p.y, deltaX, deltaY, modifiers, button: "none", clickCount: 0 });
  }

  function onBrowserKeyDown(e: React.KeyboardEvent) {
    if (!browserControl) return;
    if (!browserControlFocused) return;
    // Allow a quick escape hatch back to chat without sending the key to the browser.
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      setBrowserControl(false);
      setBrowserControlFocused(false);
      return;
    }

    e.preventDefault();
    e.stopPropagation();

    const modifiers = browserModifiers(e);
    const key = String(e.key || "");
    const code = String((e as any).code || "");

    sendBrowserKeyboardEvent({ type: "input_keyboard", eventType: "keyDown", key, code, modifiers });
    // Best-effort: emit a char event for printable text.
    if (key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      sendBrowserKeyboardEvent({ type: "input_keyboard", eventType: "char", text: key, key, code, modifiers });
    }
  }

  function onBrowserKeyUp(e: React.KeyboardEvent) {
    if (!browserControl) return;
    if (!browserControlFocused) return;
    e.preventDefault();
    e.stopPropagation();
    const modifiers = browserModifiers(e);
    const key = String(e.key || "");
    const code = String((e as any).code || "");
    sendBrowserKeyboardEvent({ type: "input_keyboard", eventType: "keyUp", key, code, modifiers });
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
            className={[navButtonBase, navButtonInactive].join(" ")}
            aria-label="History"
            type="button"
          >
            <History className="h-5 w-5" />
            <div className="pointer-events-none absolute left-14 top-1/2 -translate-y-1/2 whitespace-nowrap rounded-lg bg-ink-900 px-2.5 py-1.5 text-xs font-medium text-white opacity-0 shadow-elevated transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
              History
            </div>
          </button>

          <div className="flex-1" />
          <button
            className="group relative flex h-10 w-10 items-center justify-center rounded-lg text-ink-500 transition-colors hover:bg-surface-100 hover:text-ink-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/30"
            aria-label="Settings"
            type="button"
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
          onDeleteSession={(id) => void deleteSession(id)}
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
              />

              <Chat
                chatItems={chatItems as any}
                artifactTexts={artifactTexts}
                liveAssistant={liveAssistant}
                liveThinking={liveThinking}
                llmRunning={llmRunning}
                hasRunningTool={hasRunningTool}
                toolRuns={toolRuns as any}
                activeTaskTitle={activeTaskTitle}
                onPickSuggestion={(text) => setDraft(text)}
                fmtTime={fmtTime}
                onOpenRightTab={(tab) => openRightTabInWorkView(tab)}
                onScrollToToolRun={(toolRunId) => openToolRunInWorkView(toolRunId)}
              />

              {/* Input Area */}
              <div className="p-4 pb-6 max-w-3xl mx-auto w-full">
                <div className="relative rounded-2xl border border-surface-200 bg-surface-0 shadow-elevated transition-all focus-within:border-accent-300 focus-within:ring-2 focus-within:ring-accent-100">
                  <textarea
                    ref={textareaRef}
                    rows={1}
                    placeholder="Message Aura..."
                    className="max-h-48 w-full resize-none bg-transparent px-4 py-4 pr-24 text-sm text-ink-900 outline-none placeholder:text-ink-400 scrollbar-hide"
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
          </>
        ) : (
          // Stage View: large canvas/browser area + chat dock
          <main className="relative flex min-w-0 flex-1 flex-col bg-surface-0">
            <Topbar
              currentSessionId={currentSessionId}
              modelProfiles={modelProfiles}
              sessionMeta={sessionMeta}
              onChangeChatProfile={(profileId) => wsSend(wsRef.current, { type: "settings", chat_profile_id: profileId || undefined })}
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
                    <Badge tone={statusTone as any}>{statusLabel}</Badge>
                    {planStats.total ? <Badge tone="gray">{planStats.percent}%</Badge> : null}
                    {hasRunningBrowser ? <Badge tone="orange">browser</Badge> : null}
                    <Button onClick={() => setViewMode("work")} title="Back to Work view">
                      Work
                    </Button>
                  </div>
                </div>

                {/* Browser 视图 - 白色主题 */}
                <div className="flex min-h-0 flex-1 flex-col bg-surface-50">
                  {/* 紧凑顶部栏 */}
                  <div className="flex items-center justify-between border-b border-surface-200 bg-surface-0 px-4 py-2">
                    <div className="flex items-center gap-3">
                      {/* 连接状态指示灯 */}
                      <div className="relative">
                        <span className={`block h-2 w-2 rounded-full ${browserStreamState.wsOpen ? "bg-emerald-500" : "bg-surface-300"}`} />
                        {browserStreamState.wsOpen && (
                          <span className="absolute inset-0 animate-ping rounded-full bg-emerald-500 opacity-40" />
                        )}
                      </div>
                      {/* 分辨率和时间戳 */}
                      <span className="font-mono text-xs text-ink-600">
                        {browserFrameMeta?.deviceWidth && browserFrameMeta?.deviceHeight
                          ? `${browserFrameMeta.deviceWidth}×${browserFrameMeta.deviceHeight}`
                          : "Live"}
                      </span>
                      {browserStreamState.lastFrameAt && (
                        <span className="text-[10px] text-ink-400">{fmtTime(browserStreamState.lastFrameAt)}</span>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      {/* Observe/Control 切换按钮 - 胶囊样式 */}
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
                          onClick={() => { setBrowserControl(true); queueMicrotask(() => focusBrowserControl()); }}
                          disabled={!browserFrameSrc}
                        >
                          Control
                        </button>
                      </div>
                      {/* 图标按钮 */}
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

                  {/* 浏览器画面 */}
                  <div className="relative flex min-h-0 flex-1 items-center justify-center p-6">
                    {browserFrameSrc ? (
                      <>
                        {/* 画面容器 */}
                        <div
                          ref={browserStageRef}
                          tabIndex={browserControl ? 0 : -1}
                          className={`relative flex items-center justify-center outline-none ${browserControl ? "cursor-crosshair" : "cursor-default"}`}
                          onMouseMove={onBrowserMouseMove}
                          onWheel={sendBrowserWheel}
                          onKeyDown={onBrowserKeyDown}
                          onKeyUp={onBrowserKeyUp}
                          onBlur={() => setBrowserControlFocused(false)}
                          onFocus={() => setBrowserControlFocused(true)}
                        >
                          <img
                            ref={browserImgRef}
                            src={browserFrameSrc}
                            alt="Browser preview"
                            className="max-h-full max-w-full select-none rounded-xl border border-surface-200 shadow-elevated"
                            draggable={false}
                            onClick={sendBrowserClick}
                          />
                        </div>
                        {/* Control 模式提示 */}
                        {browserControl && (
                          <div className="absolute bottom-8 right-8 rounded-full bg-ink-900/80 px-4 py-2 text-xs font-medium text-white backdrop-blur-sm">
                            {browserControlFocused ? "🎮 Active · Esc to exit" : "Click viewport to control"}
                          </div>
                        )}
                      </>
                    ) : (
                      /* 等待状态 */
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

                  {/* 底部活动状态 */}
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
                  chatItems={chatItems as any}
                  artifactTexts={artifactTexts}
                  liveAssistant={liveAssistant}
                  liveThinking={liveThinking}
                  llmRunning={llmRunning}
                  hasRunningTool={hasRunningTool}
                  toolRuns={toolRuns as any}
                  activeTaskTitle={activeTaskTitle}
                  onPickSuggestion={(text) => setDraft(text)}
                  fmtTime={fmtTime}
                  onOpenRightTab={(tab) => openRightTabInWorkView(tab)}
                  onScrollToToolRun={(toolRunId) => openToolRunInWorkView(toolRunId)}
                />

                <div className="p-4 pb-6 w-full">
                  <div className="relative rounded-2xl border border-surface-200 bg-surface-0 shadow-elevated transition-all focus-within:border-accent-300 focus-within:ring-2 focus-within:ring-accent-100">
                    <textarea
                      ref={textareaRef}
                      rows={1}
                      placeholder="Message Aura..."
                      className="max-h-48 w-full resize-none bg-transparent px-4 py-4 pr-24 text-sm text-ink-900 outline-none placeholder:text-ink-400 scrollbar-hide"
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
              </section>
            </div>
          </main>
        )
        }

      </div >

      <Modal
        open={workspacePickerOpen}
        title="选择工作目录 (Workspace)"
        onClose={() => {
          if (workspaceBusy) return;
          setWorkspacePickerOpen(false);
        }}
        footer={
          <div className="flex items-center justify-between gap-2">
            <div className="text-xs text-rose-600">{workspaceErr || ""}</div>
            <div className="flex justify-end gap-2">
              <Button
                onClick={() => {
                  if (workspaceBusy) return;
                  setWorkspacePickerOpen(false);
                }}
              >
                关闭
              </Button>
              <Button
                variant="primary"
                onClick={() => {
                  void registerAndCreateSession(workspacePathDraft);
                }}
                disabled={workspaceBusy || !workspacePathDraft.trim()}
              >
                {workspaceBusy ? "处理中…" : "新建并进入"}
              </Button>
            </div>
          </div>
        }
      >
        <div className="space-y-4">
          <div className="rounded-xl border border-surface-200 bg-surface-50 p-3">
            <div className="text-xs font-semibold text-ink-700">已注册的工作目录</div>
            <div className="mt-2 space-y-2">
              {workspacesList.length ? (
                workspacesList.map((w) => (
                  <button
                    key={w.workspace_id}
                    className="flex w-full items-start justify-between gap-3 rounded-lg border border-surface-200 bg-surface-0 p-3 text-left hover:bg-surface-50 disabled:opacity-60"
                    onClick={() => void createSessionInWorkspace(w.workspace_id)}
                    disabled={workspaceBusy}
                    title={workspaceSubtitle(w)}
                    type="button"
                  >
                    <div className="min-w-0">
                      <div className="truncate text-sm font-semibold text-ink-900">{workspaceLabel(w)}</div>
                      <div className="truncate font-mono text-[10px] text-ink-500">{workspaceSubtitle(w)}</div>
                      {w.last_used_at ? <div className="mt-1 text-[10px] text-ink-400">最近使用：{fmtTs(w.last_used_at)}</div> : null}
                    </div>
                    <div className="font-mono text-[10px] text-ink-400">{w.workspace_id}</div>
                  </button>
                ))
              ) : (
                <div className="text-sm text-ink-500">暂无已注册 workspace。</div>
              )}
            </div>
          </div>

          <div className="rounded-xl border border-surface-200 bg-surface-50 p-3">
            <div className="text-xs font-semibold text-ink-700">注册新目录并进入</div>
            <div className="mt-2 flex items-center gap-2">
              <input
                className="w-full rounded-lg border border-surface-200 bg-surface-0 px-3 py-2 text-sm font-mono"
                placeholder="例如：D:/Work/MyProject 或 /mnt/d/Work/MyProject"
                value={workspacePathDraft}
                onChange={(e) => setWorkspacePathDraft(e.target.value)}
                disabled={workspaceBusy}
              />
              {isDesktop() ? (
                <Button
                  onClick={async () => {
                    if (workspaceBusy) return;
                    try {
                      const { open } = await import("@tauri-apps/plugin-dialog");
                      const selected = await open({ directory: true, title: "Select Workspace" });
                      if (typeof selected === "string" && selected.trim()) {
                        void registerAndCreateSession(selected);
                      }
                    } catch (e: any) {
                      setWorkspaceErr(String(e?.message || e || "desktop_directory_picker_failed"));
                    }
                  }}
                  disabled={workspaceBusy}
                >
                  Browse…
                </Button>
              ) : null}
            </div>
            <div className="mt-2 text-[11px] text-ink-500">可选择任意本地目录（不存在会自动创建并初始化 .aura/）。</div>
          </div>
        </div>
      </Modal>

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
    </div >
  );
}
