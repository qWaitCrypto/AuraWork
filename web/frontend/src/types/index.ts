// ─── Shared UI types ───────────────────────────────────────────────

export type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  ts: number;
  locator?: string;
  summary?: string;
  text?: string;
  requestId?: string | null;
  turnId?: string | null;
};

export type ToolRunStatus =
  | "running"
  | "succeeded"
  | "failed"
  | "blocked"
  | "needs_approval"
  | "cancelled"
  | "unknown";

export type TimelineRow = {
  key: string;
  kind: "llm" | "tool" | "plan" | "approval" | "error";
  title: string;
  subtitle?: string;
  details?: string;
  status?: ToolRunStatus;
  startedAt?: number;
  endedAt?: number;
  durationMs?: number;
  toolRunId?: string;
  count?: number;
  onOpenTab?: "plan" | "terminal";
  thinkingLocator?: string;
};

export type TimelineCard = {
  id: string;
  ts: number;
  requestId?: string | null;
  turnId?: string | null;
  rows: TimelineRow[];
};

export type ChatItem =
  | { kind: "message"; msg: ChatMessage }
  | { kind: "timeline"; card: TimelineCard };

export type WorkSpecView = {
  goal?: string;
  expectedOutputs: string[];
  workspaceRoots: string[];
  domainAllowlist: string[];
  fileTypeAllowlist: string[];
};

export type ToolRun = {
  id: string;
  tool: string;
  summary: string;
  startedAt: number;
  endedAt?: number;
  durationMs?: number;
  status: ToolRunStatus;
  preset?: string;
  subagentRunId?: string;
  browserAgentSession?: string;
  requestId?: string | null;
  turnId?: string | null;
  workSpec?: WorkSpecView;
};

export type ToolLog = {
  id: string;
  tool: string;
  summary: string;
  status: ToolRunStatus;
  durationMs?: number;
  preset?: string;
  subagentRunId?: string;
};

export type TerminalLogKind = "llm" | "tool" | "plan" | "approval" | "error";

export type TerminalLogItem = {
  id: string;
  ts: number;
  kind: TerminalLogKind;
  level: "info" | "error";
  title: string;
  subtitle?: string;
  status?: ToolRunStatus;
  durationMs?: number;
  toolRunId?: string;
  expandable?: boolean;
  details?: string;
};
