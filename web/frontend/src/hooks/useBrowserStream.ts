import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import { connectBrowserStreamWs, type BrowserFrameMetadata, type BrowserStreamMsg } from "../lib/ws";
import type { ApprovalRecord, ApprovalResumePayload, AuraEvent, JsonObject, TakeoverContext } from "../lib/types";

const BROWSER_WS_HEARTBEAT_MS = 15000;
const BROWSER_WS_PONG_TIMEOUT_MS = 10000;
const BROWSER_WS_RECONNECT_BASE_MS = 700;
const BROWSER_WS_RECONNECT_MAX_MS = 8000;
const BROWSER_WS_RECONNECT_JITTER_MS = 400;
const BROWSER_WS_STABLE_OPEN_MS = 1500;

export interface BrowserStreamState {
  wsOpen: boolean;
  upstreamPort?: number;
  agentSession?: string;
  lastError?: string;
  lastStatusAt?: number;
  lastFrameAt?: number;
}

export interface BrowserFrameData {
  data: string;
  metadata: BrowserFrameMetadata | null;
  ts: number;
}

function toolExecutionIdFromPayload(payload: JsonObject): string {
  if (typeof payload.tool_execution_id === "string") return payload.tool_execution_id.trim();
  if (typeof payload.tool_call_id === "string") return payload.tool_call_id.trim();
  return "";
}

function browserAgentSessionFromPayload(payload: JsonObject): string {
  if (typeof payload.browser_agent_session !== "string") return "";
  return payload.browser_agent_session.trim();
}

function isBrowserRunPayload(payload: JsonObject): boolean {
  return typeof payload.tool_name === "string" && payload.tool_name === "browser__run";
}

function resolveTakeoverContext(payload: ApprovalResumePayload | null): TakeoverContext | null {
  if (!payload) return null;

  const sub = payload.subagent;
  if (sub?.takeover === true && sub.takeover_context && typeof sub.takeover_context === "object") {
    return sub.takeover_context;
  }

  const dag = payload.dag;
  if (dag?.takeover === true && dag.takeover_context && typeof dag.takeover_context === "object") {
    return dag.takeover_context;
  }

  return null;
}

function resolveAgentSessionFromContext(context: TakeoverContext | null): string {
  if (!context) return "";

  const raw = typeof context.browser_agent_session === "string"
    ? context.browser_agent_session
    : typeof context.agent_session === "string"
      ? context.agent_session
      : "";
  return raw.trim();
}

function resolveTargetAgentSession(approvals: ApprovalRecord[], events: AuraEvent[]): string {
  for (const approval of approvals) {
    const context = resolveTakeoverContext(approval.resume_payload);
    const value = resolveAgentSessionFromContext(context);
    if (value) return value;
  }

  let latestBrowserSession = "";
  const endedBrowserToolIds = new Set<string>();

  for (let index = events.length - 1; index >= 0; index--) {
    const event = events[index];
    const kind = String(event.kind || "");
    if (kind !== "tool_call_start" && kind !== "tool_call_end") continue;

    const payload = event.payload;
    if (!isBrowserRunPayload(payload)) continue;

    const toolExecId = toolExecutionIdFromPayload(payload);
    const current = browserAgentSessionFromPayload(payload);

    if (current && !latestBrowserSession) latestBrowserSession = current;

    if (kind === "tool_call_end") {
      if (toolExecId) endedBrowserToolIds.add(toolExecId);
      continue;
    }

    if (!current) continue;
    if (toolExecId && endedBrowserToolIds.has(toolExecId)) continue;
    return current;
  }

  return latestBrowserSession;
}

export function useBrowserStream(opts: {
  viewMode: "work" | "stage";
  currentSessionId: string | null;
  approvals: ApprovalRecord[];
  events: AuraEvent[];
}) {
  const { viewMode, currentSessionId, approvals, events } = opts;

  const browserWsRef = useRef<WebSocket | null>(null);
  const browserFrameRef = useRef<BrowserFrameData>({ data: "", metadata: null, ts: 0 });
  const browserFrameRafRef = useRef<number | null>(null);
  const browserStreamTargetRef = useRef<string>("");

  const [browserFrameTick, setBrowserFrameTick] = useState(0);
  const [browserStreamState, setBrowserStreamState] = useState<BrowserStreamState>({ wsOpen: false });
  const [browserControl, setBrowserControl] = useState(false);
  const [browserControlFocused, setBrowserControlFocused] = useState(false);

  const browserImgRef = useRef<HTMLImageElement | null>(null);
  const browserStageRef = useRef<HTMLDivElement | null>(null);
  const browserMouseMoveRef = useRef<{ x: number; y: number; modifiers: number; buttons?: number } | null>(null);
  const browserMouseMoveRafRef = useRef<number | null>(null);

  const currentSessionIdRef = useRef<string | null>(currentSessionId);
  const viewModeRef = useRef<"work" | "stage">(viewMode);

  useEffect(() => {
    currentSessionIdRef.current = currentSessionId;
  }, [currentSessionId]);

  useEffect(() => {
    viewModeRef.current = viewMode;
  }, [viewMode]);

  const targetAgentSession = useMemo(() => {
    if (viewMode !== "stage" || !currentSessionId) return "";
    return resolveTargetAgentSession(approvals, events);
  }, [approvals, currentSessionId, events, viewMode]);

  const targetAgentSessionRef = useRef<string>(targetAgentSession);
  useEffect(() => {
    targetAgentSessionRef.current = targetAgentSession;
  }, [targetAgentSession]);

  const bumpBrowserFrame = useCallback(() => {
    if (browserFrameRafRef.current != null) return;
    browserFrameRafRef.current = requestAnimationFrame(() => {
      browserFrameRafRef.current = null;
      setBrowserFrameTick((tick) => tick + 1);
    });
  }, []);

  useEffect(() => {
    if (browserStreamTargetRef.current === targetAgentSession) return;
    browserStreamTargetRef.current = targetAgentSession;
    browserFrameRef.current = { data: "", metadata: null, ts: 0 };
    setBrowserFrameTick((tick) => tick + 1);
    setBrowserStreamState((state) => ({
      ...state,
      wsOpen: false,
      upstreamPort: undefined,
      agentSession: targetAgentSession || undefined,
      lastError: undefined,
    }));
  }, [targetAgentSession]);

  useEffect(() => {
    let cancelled = false;
    let reconnectAttempts = 0;
    let reconnectTimer: number | null = null;
    let heartbeatTimer: number | null = null;
    let pongTimeoutTimer: number | null = null;
    let lastPongAt = Date.now();
    let openedAt = 0;
    let sawServerTraffic = false;
    let stopReconnect = false;
    const intentionalClose = new WeakSet<WebSocket>();

    const clearReconnectTimer = () => {
      if (reconnectTimer != null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    };

    const clearHeartbeatTimers = () => {
      if (heartbeatTimer != null) {
        window.clearInterval(heartbeatTimer);
        heartbeatTimer = null;
      }
      if (pongTimeoutTimer != null) {
        window.clearTimeout(pongTimeoutTimer);
        pongTimeoutTimer = null;
      }
    };

    const closeWs = (ws: WebSocket, code?: number, reason?: string) => {
      intentionalClose.add(ws);
      try {
        if (typeof code === "number") ws.close(code, reason);
        else ws.close();
      } catch {
      }
    };

    const isStillCurrent = () => {
      if (cancelled) return false;
      if (viewModeRef.current !== "stage") return false;
      if (!currentSessionIdRef.current || currentSessionIdRef.current !== currentSessionId) return false;
      if (targetAgentSessionRef.current !== targetAgentSession) return false;
      return true;
    };

    const startHeartbeat = (ws: WebSocket) => {
      clearHeartbeatTimers();
      lastPongAt = Date.now();

      heartbeatTimer = window.setInterval(() => {
        if (!isStillCurrent() || ws.readyState !== WebSocket.OPEN) return;

        const sentAt = Date.now();
        try {
          ws.send(JSON.stringify({ type: "ping" }));
        } catch {
          return;
        }

        if (pongTimeoutTimer != null) window.clearTimeout(pongTimeoutTimer);
        pongTimeoutTimer = window.setTimeout(() => {
          if (!isStillCurrent() || ws.readyState !== WebSocket.OPEN) return;
          if (lastPongAt < sentAt) {
            closeWs(ws, 4001, "heartbeat_timeout");
          }
        }, BROWSER_WS_PONG_TIMEOUT_MS);
      }, BROWSER_WS_HEARTBEAT_MS);
    };

    const scheduleReconnect = (connect: () => void) => {
      if (!isStillCurrent()) return;

      reconnectAttempts += 1;
      const exp = Math.max(0, reconnectAttempts - 1);
      const backoff = Math.min(BROWSER_WS_RECONNECT_MAX_MS, BROWSER_WS_RECONNECT_BASE_MS * (2 ** exp));
      const jitter = Math.floor(Math.random() * BROWSER_WS_RECONNECT_JITTER_MS);
      const delay = backoff + jitter;

      clearReconnectTimer();
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };

    if (viewMode !== "stage") {
      if (browserWsRef.current) {
        closeWs(browserWsRef.current);
        browserWsRef.current = null;
      }
      setBrowserControl(false);
      setBrowserControlFocused(false);
      setBrowserStreamState((state) => ({
        ...state,
        wsOpen: false,
        lastError: undefined,
      }));
      return;
    }

    if (!currentSessionId) {
      if (browserWsRef.current) {
        closeWs(browserWsRef.current);
        browserWsRef.current = null;
      }
      setBrowserStreamState((state) => ({
        ...state,
        wsOpen: false,
        upstreamPort: undefined,
        agentSession: undefined,
      }));
      return;
    }

    const connect = () => {
      if (!isStillCurrent()) return;

      if (browserWsRef.current) {
        closeWs(browserWsRef.current);
        browserWsRef.current = null;
      }

      setBrowserStreamState((state) => ({
        ...state,
        wsOpen: false,
        upstreamPort: undefined,
        agentSession: targetAgentSession || undefined,
        lastError: undefined,
      }));

      const ws = connectBrowserStreamWs(
        currentSessionId,
        (msg: BrowserStreamMsg) => {
          lastPongAt = Date.now();
          sawServerTraffic = true;

          if (msg.type === "pong") {
            if (pongTimeoutTimer != null) {
              window.clearTimeout(pongTimeoutTimer);
              pongTimeoutTimer = null;
            }
            return;
          }

          if (msg.type === "frame" && typeof msg.data === "string") {
            const metadata = msg.metadata && typeof msg.metadata === "object"
              ? (msg.metadata as BrowserFrameMetadata)
              : null;
            browserFrameRef.current = { data: msg.data, metadata, ts: Date.now() };
            setBrowserStreamState((state) => ({ ...state, lastFrameAt: Date.now(), lastError: undefined }));
            bumpBrowserFrame();
            return;
          }

          if (msg.type === "status") {
            setBrowserStreamState((state) => ({
              ...state,
              upstreamPort: typeof msg.port === "number" ? msg.port : state.upstreamPort,
              agentSession: typeof msg.agent_session === "string" ? msg.agent_session : state.agentSession,
              lastStatusAt: Date.now(),
              lastError: undefined,
            }));
            return;
          }

          if (msg.type === "error") {
            const message = String(msg.message || "browser_stream_error");
            if (message.includes("browser_stream_disabled")) {
              stopReconnect = true;
            }
            setBrowserStreamState((state) => ({ ...state, lastError: message }));
            return;
          }
        },
        { agentSession: targetAgentSession || undefined },
      );

      browserWsRef.current = ws;

      ws.addEventListener("open", () => {
        if (!isStillCurrent()) {
          closeWs(ws);
          return;
        }

        openedAt = Date.now();
        sawServerTraffic = false;
        clearReconnectTimer();
        setBrowserStreamState((state) => ({
          ...state,
          wsOpen: true,
          agentSession: targetAgentSession || state.agentSession,
          lastError: undefined,
        }));
        startHeartbeat(ws);
      });

      ws.addEventListener("close", (event) => {
        clearHeartbeatTimers();
        if (browserWsRef.current === ws) browserWsRef.current = null;
        if (intentionalClose.has(ws)) return;
        if (!isStillCurrent()) return;
        setBrowserStreamState((state) => ({ ...state, wsOpen: false }));
        if (stopReconnect || event.code === 1008) return;
        if (sawServerTraffic && openedAt > 0 && Date.now() - openedAt >= BROWSER_WS_STABLE_OPEN_MS) {
          reconnectAttempts = 0;
        }
        scheduleReconnect(connect);
      });

      ws.addEventListener("error", () => {
        closeWs(ws);
      });
    };

    connect();

    return () => {
      cancelled = true;
      clearReconnectTimer();
      clearHeartbeatTimers();
      if (browserWsRef.current) {
        closeWs(browserWsRef.current);
        browserWsRef.current = null;
      }
    };
  }, [bumpBrowserFrame, currentSessionId, targetAgentSession, viewMode]);

  useEffect(() => {
    browserFrameRef.current = { data: "", metadata: null, ts: 0 };
    setBrowserFrameTick(0);
    setBrowserStreamState({ wsOpen: false });
    setBrowserControl(false);
    setBrowserControlFocused(false);
  }, [currentSessionId]);

  return {
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
  };
}
