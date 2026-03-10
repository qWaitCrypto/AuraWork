import type { ApprovalRecord, AuraEvent, JsonObject, SessionMeta } from "./types";
import { backendToken, wsBase } from "./backendBase";

export type ServerMsg =
  | { type: "session_meta"; meta: SessionMeta }
  | { type: "replay"; events: AuraEvent[] }
  | { type: "event"; event: AuraEvent }
  | { type: "ack"; op: string; request_id?: string; turn_id?: string }
  | { type: "approvals"; approvals: ApprovalRecord[] }
  | { type: "error"; message: string }
  | { type: "pong" }
  | { type: string; [k: string]: unknown };

export type ClientMsg =
  | { type: "ping" }
  | { type: "hello"; since_event_id?: string }
  | { type: "chat"; text: string }
  | { type: "compact" }
  | {
      type: "approval";
      approval_id: string;
      decision: "approve" | "deny" | "edit" | "dry_run";
      note?: string;
      edited_arguments?: Record<string, unknown>;
    }
  | { type: "settings"; chat_profile_id?: string; llm_streaming?: boolean; tool_approval_mode?: string }
  | { type: "list_approvals" };

export function connectSessionWs(sessionId: string, onMsg: (msg: ServerMsg) => void) {
  const base = wsBase();
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const baseUrl = base ? `${base}/ws/${encodeURIComponent(sessionId)}` : `${scheme}://${location.host}/ws/${encodeURIComponent(sessionId)}`;
  const token = backendToken();
  const url = token ? `${baseUrl}?access_token=${encodeURIComponent(token)}` : baseUrl;
  const ws = new WebSocket(url);
  ws.addEventListener("message", (ev) => {
    try {
      onMsg(JSON.parse(ev.data) as ServerMsg);
    } catch {
      onMsg({ type: "error", message: "Invalid server message" });
    }
  });
  return ws;
}

export type BrowserFrameMetadata = {
  deviceWidth?: number;
  deviceHeight?: number;
  [k: string]: unknown;
};

export type BrowserStreamMsg =
  | { type: "frame"; data: string; metadata?: BrowserFrameMetadata | null }
  | { type: "status"; connected?: boolean; screencasting?: boolean; port?: number; agent_session?: string }
  | { type: "error"; message: string }
  | { type: "pong" }
  | { type: string; [k: string]: unknown };

export function connectBrowserStreamWs(
  sessionId: string,
  onMsg: (msg: BrowserStreamMsg) => void,
  opts?: { agentSession?: string | null },
) {
  const base = wsBase();
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const endpoint = base
    ? `${base}/ws/${encodeURIComponent(sessionId)}/browser`
    : `${scheme}://${location.host}/ws/${encodeURIComponent(sessionId)}/browser`;
  const token = backendToken();
  const agentSession = typeof opts?.agentSession === "string" ? opts.agentSession.trim() : "";
  const qs = new URLSearchParams();
  if (token) qs.set("access_token", token);
  if (agentSession) qs.set("agent_session", agentSession);
  const query = qs.toString();
  const url = query ? `${endpoint}?${query}` : endpoint;
  const ws = new WebSocket(url);
  ws.addEventListener("message", (ev) => {
    const raw = ev.data;
    if (raw === "pong") {
      onMsg({ type: "pong" });
      return;
    }
    try {
      onMsg(JSON.parse(raw) as BrowserStreamMsg);
    } catch {
      onMsg({ type: "error", message: "Invalid browser stream message" });
    }
  });
  return ws;
}

export function wsSend(ws: WebSocket | null, msg: ClientMsg | JsonObject) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  ws.send(JSON.stringify(msg));
  return true;
}
