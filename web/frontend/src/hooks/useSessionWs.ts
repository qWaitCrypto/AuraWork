import { useEffect, useRef, useState, useCallback } from "react";
import { connectSessionWs, wsSend, type ServerMsg } from "../lib/ws";
import { apiBootstrap, apiDeleteSession, apiGetApprovals, apiGetSession } from "../lib/api";
import type { AuraEvent } from "../lib/types";
import { waitForBackendGlobals } from "../lib/backendBase";
import { useEventStore } from "../store/eventStore";
import { useUiStore } from "../store/uiStore";
import { useArtifactStore } from "../store/artifactStore";

const SESSION_WS_HEARTBEAT_MS = 15000;
const SESSION_WS_PONG_TIMEOUT_MS = 10000;
const SESSION_WS_RECONNECT_BASE_MS = 800;
const SESSION_WS_RECONNECT_MAX_MS = 12000;
const SESSION_WS_RECONNECT_JITTER_MS = 400;
const SESSION_WS_STABLE_OPEN_MS = 1500;

function uiDebugEnabled() {
    try {
        return localStorage.getItem("AURA_WEB_DEBUG") === "1";
    } catch {
        return false;
    }
}

/**
 * Core session WebSocket hook.
 *
 * Manages:
 * - WS connection lifecycle (connect / reconnect / close)
 * - Event batching + flushing into EventStore
 * - LLM streaming buffer (text + thinking deltas → primeArtifactText)
 * - Approval refreshes
 * - Bootstrap / session CRUD helpers
 */
export function useSessionWs() {
    const appendMany = useEventStore((s) => s.appendMany);
    const clearEvents = useEventStore((s) => s.clear);

    const currentSessionId = useUiStore((s) => s.currentSessionId);
    const activeApproval = useUiStore((s) => s.activeApproval);

    const setBootstrap = useUiStore((s) => s.setBootstrap);
    const setSessions = useUiStore((s) => s.setSessions);
    const setCurrentSession = useUiStore((s) => s.setCurrentSession);
    const setSessionMeta = useUiStore((s) => s.setSessionMeta);
    const setApprovals = useUiStore((s) => s.setApprovals);
    const popApproval = useUiStore((s) => s.popApproval);

    const primeArtifactText = useArtifactStore((s) => s.primeText);

    const wsRef = useRef<WebSocket | null>(null);
    const currentSessionIdRef = useRef<string | null>(null);
    const [connected, setConnected] = useState(false);
    const [pendingUserMessage, setPendingUserMessage] = useState<{ id: string; text: string; ts: number } | null>(null);

    const wsEventQueueRef = useRef<AuraEvent[]>([]);
    const wsFlushScheduledRef = useRef(false);
    const liveStreamRef = useRef<{ open: boolean; text: string; thinking: string; startedAt: number }>({
        open: false,
        text: "",
        thinking: "",
        startedAt: 0,
    });

    // Keep the ref in sync
    useEffect(() => {
        currentSessionIdRef.current = currentSessionId;
    }, [currentSessionId]);

    // ── Event batching / flush ──────────────────────────────────────────

    const flushWsEvents = useCallback(() => {
        wsFlushScheduledRef.current = false;
        const batch = wsEventQueueRef.current;
        if (!batch.length) return;
        wsEventQueueRef.current = [];

        const stream = liveStreamRef.current;
        const debug = uiDebugEnabled();
        let shouldRefreshApprovals = false;

        for (const e of batch) {
            if (e.kind === "tool_approval_requested" || e.kind === "tool_approval_resolved") {
                shouldRefreshApprovals = true;
                continue;
            }
            if (e.kind === "llm_request_started") {
                stream.open = true;
                stream.startedAt = e.timestamp;
                stream.text = "";
                stream.thinking = "";
                if (debug) {
                    console.debug("[ui] llm_request_started", {
                        event_id: e.event_id,
                        request_id: e.request_id,
                        turn_id: e.turn_id,
                    });
                }
                continue;
            }
            if (e.kind === "llm_response_delta") {
                if (!stream.open) {
                    stream.open = true;
                    stream.startedAt = e.timestamp;
                    stream.text = "";
                    stream.thinking = "";
                }
                const textDelta = typeof e.payload.text_delta === "string" ? e.payload.text_delta : "";
                stream.text += textDelta;
                continue;
            }
            if (e.kind === "llm_thinking_delta") {
                if (!stream.open) {
                    stream.open = true;
                    stream.startedAt = e.timestamp;
                    stream.text = "";
                    stream.thinking = "";
                }
                const thinkingDelta = typeof e.payload.thinking_delta === "string" ? e.payload.thinking_delta : "";
                stream.thinking += thinkingDelta;
                continue;
            }
            if (e.kind === "llm_response_completed") {
                stream.open = false;
                const outputRef = e.payload.output_ref;
                const outputRefRecord = outputRef && typeof outputRef === "object" ? (outputRef as Record<string, unknown>) : null;
                const locator = typeof outputRefRecord?.locator === "string" ? outputRefRecord.locator : "";
                const finalText = typeof e.payload.final_text === "string" ? e.payload.final_text : "";
                const text = finalText || stream.text;
                const sessionId = currentSessionIdRef.current;
                if (sessionId && locator.trim() && text) {
                    primeArtifactText(sessionId, locator, text);
                }
                continue;
            }
            if (e.kind === "llm_request_failed") {
                stream.open = false;
                continue;
            }
        }

        appendMany(batch);

        if (shouldRefreshApprovals) {
            const sid = currentSessionIdRef.current;
            if (sid) {
                apiGetApprovals(sid).then((rows) => setApprovals(rows)).catch(() => { });
            }
        }

        // Clear optimistic chat message when the server acknowledges a chat op
        for (const e of batch) {
            if (e.kind === "operation_started" && String(e.payload.op_kind || "") === "chat") {
                setPendingUserMessage(null);
                break;
            }
        }
    }, [appendMany, primeArtifactText, setApprovals]);

    const enqueueWsEvent = useCallback((e: AuraEvent) => {
        wsEventQueueRef.current.push(e);
        if (wsFlushScheduledRef.current) return;
        wsFlushScheduledRef.current = true;
        requestAnimationFrame(flushWsEvents);
    }, [flushWsEvents]);

    // ── Refresh helpers ─────────────────────────────────────────────────

    const refreshBootstrap = useCallback(async () => {
        const b = await apiBootstrap();
        setBootstrap(b);
        setSessions(b.sessions || []);
        if (!currentSessionIdRef.current && b.sessions?.length) setCurrentSession(b.sessions[0].session_id);
        return b;
    }, [setBootstrap, setSessions, setCurrentSession]);

    const refreshApprovals = useCallback(async () => {
        const sid = currentSessionIdRef.current;
        if (!sid) return;
        const a = await apiGetApprovals(sid);
        setApprovals(a);
    }, [setApprovals]);

    // ── Bootstrap on mount ──────────────────────────────────────────────

    useEffect(() => {
        waitForBackendGlobals()
            .catch(() => { })
            .finally(() => {
                refreshBootstrap().catch(() => { });
            });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // ── Session WebSocket lifecycle ─────────────────────────────────────

    useEffect(() => {
        if (!currentSessionId) {
            try {
                wsRef.current?.close();
            } catch {
            }
            wsRef.current = null;
            wsEventQueueRef.current = [];
            wsFlushScheduledRef.current = false;
            setConnected(false);
            setPendingUserMessage(null);
            clearEvents();
            setSessionMeta(null);
            setApprovals([]);
            liveStreamRef.current = { open: false, text: "", thinking: "", startedAt: 0 };
            return;
        }

        clearEvents();
        setSessionMeta(null);
        setApprovals([]);
        setConnected(false);
        setPendingUserMessage(null);
        wsEventQueueRef.current = [];
        wsFlushScheduledRef.current = false;
        liveStreamRef.current = { open: false, text: "", thinking: "", startedAt: 0 };

        let cancelled = false;
        let reconnectAttempts = 0;
        let reconnectTimer: number | null = null;
        let heartbeatTimer: number | null = null;
        let pongTimeoutTimer: number | null = null;
        let lastPongAt = Date.now();
        let openedAt = 0;
        let sawServerTraffic = false;
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

        const startHeartbeat = (ws: WebSocket) => {
            clearHeartbeatTimers();
            lastPongAt = Date.now();

            heartbeatTimer = window.setInterval(() => {
                if (cancelled || ws.readyState !== WebSocket.OPEN) return;

                const sentAt = Date.now();
                const ok = wsSend(ws, { type: "ping" });
                if (!ok) return;

                if (pongTimeoutTimer != null) window.clearTimeout(pongTimeoutTimer);
                pongTimeoutTimer = window.setTimeout(() => {
                    if (cancelled || ws.readyState !== WebSocket.OPEN) return;
                    if (lastPongAt < sentAt) {
                        // Do NOT use closeWs() here — that marks the close as intentional and
                        // suppresses the reconnect in the 'close' handler. A heartbeat timeout
                        // should always trigger a reconnect.
                        try { ws.close(4001, "heartbeat_timeout"); } catch { /* ignore */ }
                    }
                }, SESSION_WS_PONG_TIMEOUT_MS);
            }, SESSION_WS_HEARTBEAT_MS);
        };

        const scheduleReconnect = () => {
            if (cancelled) return;
            if (currentSessionIdRef.current !== currentSessionId) return;

            reconnectAttempts += 1;
            const exp = Math.max(0, reconnectAttempts - 1);
            const backoff = Math.min(SESSION_WS_RECONNECT_MAX_MS, SESSION_WS_RECONNECT_BASE_MS * (2 ** exp));
            const jitter = Math.floor(Math.random() * SESSION_WS_RECONNECT_JITTER_MS);
            const delay = backoff + jitter;

            clearReconnectTimer();
            reconnectTimer = window.setTimeout(() => {
                reconnectTimer = null;
                connectWs();
            }, delay);
        };

        const connectWs = () => {
            if (cancelled) return;
            if (currentSessionIdRef.current !== currentSessionId) return;

            if (wsRef.current) {
                closeWs(wsRef.current);
                wsRef.current = null;
            }

            const ws = connectSessionWs(currentSessionId, (msg: ServerMsg) => {
                lastPongAt = Date.now();
                sawServerTraffic = true;
                if (msg.type === "pong") {
                    if (pongTimeoutTimer != null) {
                        window.clearTimeout(pongTimeoutTimer);
                        pongTimeoutTimer = null;
                    }
                    return;
                }
                if (msg.type === "session_meta") {
                    if (msg.meta && typeof msg.meta === "object") {
                        setSessionMeta(msg.meta as Record<string, unknown>);
                    }
                    return;
                }
                if (msg.type === "replay") {
                    if (Array.isArray(msg.events)) {
                        appendMany(msg.events as AuraEvent[]);
                    }
                    return;
                }
                if (msg.type === "event") {
                    if (msg.event && typeof msg.event === "object") {
                        enqueueWsEvent(msg.event as AuraEvent);
                    }
                    return;
                }
            });

            wsRef.current = ws;

            ws.addEventListener("open", () => {
                if (cancelled || currentSessionIdRef.current !== currentSessionId) {
                    closeWs(ws);
                    return;
                }

                const wasReconnect = reconnectAttempts > 0;
                openedAt = Date.now();
                sawServerTraffic = false;
                clearReconnectTimer();
                setConnected(true);
                startHeartbeat(ws);

                if (wasReconnect) {
                    const prevEvents = useEventStore.getState().events;
                    const sinceEventId = prevEvents.length ? prevEvents[prevEvents.length - 1]?.event_id : null;
                    if (sinceEventId) wsSend(ws, { type: "hello", since_event_id: sinceEventId });
                }

                apiGetSession(currentSessionId).then((meta) => setSessionMeta(meta)).catch(() => { });
                refreshApprovals().catch(() => { });
            });

            ws.addEventListener("close", (event) => {
                clearHeartbeatTimers();
                if (wsRef.current === ws) wsRef.current = null;
                if (intentionalClose.has(ws)) return;
                if (cancelled || currentSessionIdRef.current !== currentSessionId) return;
                setConnected(false);
                if (event.code === 1008) return;
                if (sawServerTraffic && openedAt > 0 && Date.now() - openedAt >= SESSION_WS_STABLE_OPEN_MS) {
                    reconnectAttempts = 0;
                }
                scheduleReconnect();
            });

            ws.addEventListener("error", () => {
                // A WebSocket error is always followed by the 'close' event, which
                // will handle reconnect. Do NOT call closeWs() here — that would mark
                // the socket as an intentional close and suppress the reconnect.
            });
        };

        connectWs();

        return () => {
            cancelled = true;
            clearReconnectTimer();
            clearHeartbeatTimers();
            if (wsRef.current) {
                closeWs(wsRef.current);
                wsRef.current = null;
            }
        };
    }, [appendMany, clearEvents, currentSessionId, enqueueWsEvent, refreshApprovals, setApprovals, setSessionMeta]);

    // ── Chat send ───────────────────────────────────────────────────────

    const sendChat = useCallback((text: string): boolean => {
        const trimmed = text.trim();
        if (!trimmed || !connected) return false;
        const ok = wsSend(wsRef.current, { type: "chat", text: trimmed });
        if (ok) {
            setPendingUserMessage({ id: `pending-${Date.now()}`, text: trimmed, ts: Date.now() });
        }
        return ok;
    }, [connected]);

    const sendCompact = useCallback((): boolean => {
        if (!connected) return false;
        return wsSend(wsRef.current, { type: "compact" });
    }, [connected]);

    // ── Approval decision ───────────────────────────────────────────────

    const decideApprovalById = useCallback((
        approvalId: string,
        decision: "approve" | "deny" | "edit" | "dry_run",
        note?: string,
        editedArguments?: Record<string, unknown>,
    ): boolean => {
        const id = String(approvalId || "").trim();
        if (!id) return false;
        const payload: {
            type: "approval";
            approval_id: string;
            decision: "approve" | "deny" | "edit" | "dry_run";
            note?: string;
            edited_arguments?: Record<string, unknown>;
        } = {
            type: "approval",
            approval_id: id,
            decision,
        };
        if (typeof note === "string" && note.trim()) payload.note = note.trim();
        if (editedArguments && typeof editedArguments === "object") payload.edited_arguments = editedArguments;

        const ok = wsSend(wsRef.current, payload);
        if (!ok) return false;

        if (activeApproval?.approval_id === id) popApproval();
        refreshApprovals().catch(() => { });
        return true;
    }, [activeApproval, popApproval, refreshApprovals]);

    const decideApproval = useCallback((decision: "approve" | "deny" | "edit" | "dry_run") => {
        if (!activeApproval) return false;
        return decideApprovalById(activeApproval.approval_id, decision);
    }, [activeApproval, decideApprovalById]);

    // ── Delete session ──────────────────────────────────────────────────

    const deleteSession = useCallback(async (sessionId: string) => {
        await apiDeleteSession(sessionId);
        await refreshBootstrap();
        if (currentSessionIdRef.current === sessionId) {
            const remainingSessions = useUiStore.getState().sessions;
            setCurrentSession(remainingSessions.length > 0 ? remainingSessions[0].session_id : null);
        }
    }, [refreshBootstrap, setCurrentSession]);

    return {
        // State
        connected,
        pendingUserMessage,
        wsRef,
        liveStreamRef,

        // Actions
        sendChat,
        sendCompact,
        decideApproval,
        decideApprovalById,
        deleteSession,
        refreshBootstrap,
        refreshApprovals,
        setCurrentSession,
        openSession: setCurrentSession,
    };
}
