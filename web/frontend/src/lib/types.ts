export type ProviderKind = "openai_compatible" | "anthropic" | "gemini" | string;

export type JsonObject = Record<string, unknown>;

export function isRecord(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null;
}

export type ModelProfile = {
  profile_id: string;
  provider_kind: ProviderKind;
  model: string;
  supports_tools: boolean;
  supports_streaming: boolean;
};

export type WorkspaceRecord = {
  workspace_id: string;
  project_root: string;
  last_used_at?: number | null;
};

export type PlanStepStatus = "pending" | "in_progress" | "completed" | "failed";

export type PlanStep = {
  id?: string;
  step?: string;
  status?: PlanStepStatus;
  depends_on?: string[];
  [k: string]: unknown;
};

export type PlanEnvelope = {
  plan: PlanStep[];
  plan_type?: string;
  explanation?: string;
  [k: string]: unknown;
};

export type SessionSummary = {
  session_id: string;
  updated_at: number | null;
  created_at: number | null;
  mode: string | null;
  chat_profile_id: string | null;
  tool_approval_mode: string | null;
  llm_streaming: boolean | null;

  // Workspace linkage (multi-workspace web backend)
  workspace_id?: string;
  project_root?: string;
};

export type SessionMeta = {
  session_id?: string;
  updated_at?: number | null;
  created_at?: number | null;
  mode?: string | null;
  chat_profile_id?: string | null;
  tool_approval_mode?: string | null;
  llm_streaming?: boolean | null;
  plan?: PlanStep[];
  [k: string]: unknown;
};

export type Bootstrap = {
  // Legacy compatibility field (target removal in Web API >= 0.4.0).
  project_root: string;

  workspaces: WorkspaceRecord[];
  model_profiles: ModelProfile[];
  sessions: SessionSummary[];
};

export type ArtifactRef = {
  locator: string;
  summary?: string | null;
  kind?: string | null;
  meta?: JsonObject | null;
};

export type TakeoverContext = {
  current_url?: string;
  browser_agent_session?: string;
  agent_session?: string;
  [k: string]: unknown;
};

export type ApprovalResumeSubagent = {
  takeover?: boolean;
  takeover_context?: TakeoverContext;
  [k: string]: unknown;
};

export type ApprovalResumeDag = {
  takeover?: boolean;
  takeover_context?: TakeoverContext;
  pending_queue?: JsonObject[];
  [k: string]: unknown;
};

export type ApprovalResumePayload = {
  subagent?: ApprovalResumeSubagent;
  dag?: ApprovalResumeDag;
  [k: string]: unknown;
};

export type AuraEvent = {
  kind: string;
  payload: JsonObject;
  session_id: string;
  event_id: string;
  timestamp: number;
  sequence?: number;
  request_id?: string | null;
  turn_id?: string | null;
  step_id?: string | null;
  schema_version?: string | null;
};

export type ApprovalRecord = {
  approval_id: string;
  session_id: string;
  request_id: string | null;
  created_at: number;
  status: string;
  turn_id: string | null;
  action_summary: string | null;
  risk_level: string | null;
  options: string[] | null;
  reason: string | null;
  diff_ref: ArtifactRef | null;
  resume_kind: string | null;
  resume_payload: ApprovalResumePayload | null;
  decision?: Record<string, unknown> | null;
};
