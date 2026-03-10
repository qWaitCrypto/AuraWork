import type { ApprovalRecord, Bootstrap, SessionMeta, WorkspaceRecord } from "./types";
import { backendToken, httpBase } from "./backendBase";

function authFetch(input: string, init?: RequestInit): Promise<Response> {
  const token = backendToken();
  const headers = new Headers(init?.headers ?? {});
  if (token) headers.set("authorization", `Bearer ${token}`);
  return fetch(input, { ...init, headers });
}

export async function apiBootstrap(): Promise<Bootstrap> {
  const res = await authFetch(`${httpBase()}/api/bootstrap`);
  if (!res.ok) throw new Error(`bootstrap failed: ${res.status}`);
  return res.json();
}

export async function apiListWorkspaces(): Promise<{ workspaces: WorkspaceRecord[] }> {
  const res = await authFetch(`${httpBase()}/api/workspaces`);
  if (!res.ok) throw new Error(`workspaces failed: ${res.status}`);
  return res.json();
}

export async function apiRegisterWorkspace(projectRoot: string): Promise<WorkspaceRecord> {
  const res = await authFetch(`${httpBase()}/api/workspaces`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ project_root: projectRoot }),
  });
  if (!res.ok) throw new Error(`register workspace failed: ${res.status}`);
  return res.json();
}

export async function apiCreateSession(workspaceId: string): Promise<{ session_id: string; workspace_id: string; project_root: string }> {
  const res = await authFetch(`${httpBase()}/api/sessions`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ workspace_id: workspaceId }),
  });
  if (!res.ok) throw new Error(`create session failed: ${res.status}`);
  return res.json();
}

export async function apiGetSession(sessionId: string): Promise<SessionMeta> {
  const res = await authFetch(`${httpBase()}/api/sessions/${encodeURIComponent(sessionId)}`);
  if (!res.ok) throw new Error(`get session failed: ${res.status}`);
  return res.json();
}

export async function apiDeleteSession(sessionId: string): Promise<void> {
  const res = await authFetch(`${httpBase()}/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`delete session failed: ${res.status}`);
}

export async function apiGetApprovals(sessionId: string): Promise<ApprovalRecord[]> {
  const res = await authFetch(`${httpBase()}/api/sessions/${encodeURIComponent(sessionId)}/approvals`);
  if (!res.ok) throw new Error(`approvals failed: ${res.status}`);
  const data = await res.json();
  return (data.approvals || []) as ApprovalRecord[];
}

export async function apiFetchArtifact(sessionId: string, locator: string): Promise<string> {
  const res = await authFetch(`${httpBase()}/api/sessions/${encodeURIComponent(sessionId)}/artifacts/${encodeURIComponent(locator)}`);
  if (!res.ok) throw new Error(`artifact failed: ${res.status}`);
  return res.text();
}

export type GlobalModelSettings = {
  configured: boolean;
  provider_kind?: string;
  base_url?: string;
  model?: string;
  api_key_set?: boolean;
  api_key_hint?: string;
  max_tokens?: number | null;
};

export async function apiGetModelSettings(): Promise<GlobalModelSettings> {
  const res = await authFetch(`${httpBase()}/api/settings/model`);
  if (!res.ok) throw new Error(`get model settings failed: ${res.status}`);
  return res.json();
}

export async function apiPutModelSettings(settings: {
  provider_kind: string;
  base_url: string;
  model: string;
  api_key?: string;
  max_tokens?: number | null;
}): Promise<void> {
  const res = await authFetch(`${httpBase()}/api/settings/model`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(settings),
  });
  if (!res.ok) throw new Error(`save model settings failed: ${res.status}`);
}

export type WorkspaceFileEntry = {
  path: string;
  name: string;
  is_dir: boolean;
  size_bytes?: number | null;
  modified_at: number;
};

export async function apiListWorkspaceFiles(
  sessionId: string,
  dir: string,
  opts?: { limit?: number; showHidden?: boolean }
): Promise<{ dir: string; entries: WorkspaceFileEntry[] }> {
  const qs = new URLSearchParams();
  if (dir) qs.set("dir", dir);
  if (typeof opts?.limit === "number") qs.set("limit", String(opts.limit));
  if (opts?.showHidden) qs.set("show_hidden", "true");
  const url = `${httpBase()}/api/sessions/${encodeURIComponent(sessionId)}/workspace/files${qs.toString() ? `?${qs.toString()}` : ""}`;
  const res = await authFetch(url);
  if (!res.ok) throw new Error(`workspace files failed: ${res.status}`);
  return res.json();
}

export async function apiFetchWorkspaceFileText(
  sessionId: string,
  path: string,
  opts?: { maxBytes?: number }
): Promise<{ path: string; text: string; bytes: number; truncated: boolean }> {
  const qs = new URLSearchParams();
  qs.set("path", path);
  if (typeof opts?.maxBytes === "number") qs.set("max_bytes", String(opts.maxBytes));
  const url = `${httpBase()}/api/sessions/${encodeURIComponent(sessionId)}/workspace/file_text?${qs.toString()}`;
  const res = await authFetch(url);
  if (!res.ok) throw new Error(`workspace file_text failed: ${res.status}`);
  return res.json();
}
