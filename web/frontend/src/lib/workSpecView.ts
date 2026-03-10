import type { WorkSpecView } from "../types";

export function cleanText(raw: unknown, maxLen = 220): string | undefined {
  if (typeof raw !== "string") return undefined;
  const text = raw.replace(/\s+/g, " ").trim();
  if (!text) return undefined;
  if (text.length <= maxLen) return text;
  return `${text.slice(0, maxLen - 1)}…`;
}

export function cleanStringList(raw: unknown, limit = 4, itemMaxLen = 120): string[] {
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  for (const item of raw) {
    const value = cleanText(item, itemMaxLen);
    if (!value) continue;
    out.push(value);
    if (out.length >= limit) break;
  }
  return out;
}

export function parseWorkSpecView(raw: unknown): WorkSpecView | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const ws = raw as Record<string, unknown>;
  const goal = cleanText(ws.goal, 220);

  const expectedOutputs = Array.isArray(ws.expected_outputs)
    ? ws.expected_outputs
      .map((item) => {
        if (!item || typeof item !== "object") return undefined;
        const rec = item as Record<string, unknown>;
        const outputType = cleanText(rec.type, 40);
        const outputPath = cleanText(rec.path, 120);
        if (outputType && outputPath) return `${outputType}: ${outputPath}`;
        return outputPath || outputType || cleanText(rec.format, 60);
      })
      .filter((item): item is string => Boolean(item))
      .slice(0, 6)
    : [];

  const scopeRaw = ws.resource_scope && typeof ws.resource_scope === "object" ? (ws.resource_scope as Record<string, unknown>) : {};
  const workspaceRoots = cleanStringList(scopeRaw.workspace_roots, 4, 120);
  const domainAllowlist = cleanStringList(scopeRaw.domain_allowlist, 4, 100);
  const fileTypeAllowlist = cleanStringList(scopeRaw.file_type_allowlist, 6, 40);

  if (!goal && !expectedOutputs.length && !workspaceRoots.length && !domainAllowlist.length && !fileTypeAllowlist.length) {
    return undefined;
  }

  return {
    goal,
    expectedOutputs,
    workspaceRoots,
    domainAllowlist,
    fileTypeAllowlist,
  };
}

export function formatWorkSpecSummary(ws?: WorkSpecView): string | undefined {
  if (!ws) return undefined;
  const parts: string[] = [];
  if (ws.goal) parts.push(`goal: ${ws.goal}`);
  if (ws.expectedOutputs.length) parts.push(`outputs: ${ws.expectedOutputs.length}`);
  if (ws.workspaceRoots.length) parts.push(`roots: ${ws.workspaceRoots.length}`);
  if (ws.domainAllowlist.length) parts.push(`domains: ${ws.domainAllowlist.length}`);
  if (ws.fileTypeAllowlist.length) parts.push(`types: ${ws.fileTypeAllowlist.length}`);
  return parts.length ? parts.join(" · ") : undefined;
}

export function formatWorkSpecDetails(ws?: WorkSpecView): string | undefined {
  if (!ws) return undefined;
  const lines: string[] = [];
  if (ws.goal) lines.push(`goal: ${ws.goal}`);
  if (ws.expectedOutputs.length) lines.push(`expected_outputs:\n- ${ws.expectedOutputs.join("\n- ")}`);
  if (ws.workspaceRoots.length) lines.push(`workspace_roots: ${ws.workspaceRoots.join(", ")}`);
  if (ws.domainAllowlist.length) lines.push(`domain_allowlist: ${ws.domainAllowlist.join(", ")}`);
  if (ws.fileTypeAllowlist.length) lines.push(`file_type_allowlist: ${ws.fileTypeAllowlist.join(", ")}`);
  return lines.length ? lines.join("\n") : undefined;
}

export function joinDetails(parts: Array<string | undefined>): string | undefined {
  const rows = parts.map((item) => String(item || "").trim()).filter(Boolean);
  return rows.length ? rows.join("\n") : undefined;
}

export function normalizeApproverDecision(raw: unknown): "allow" | "deny" | "escalate" | "unknown" {
  const value = String(raw || "").trim().toLowerCase();
  if (!value) return "unknown";
  if (value === "allow") return "allow";
  if (value === "deny") return "deny";
  if (["require_approval", "needs_approval", "escalate"].includes(value)) return "escalate";
  return "unknown";
}

export function summarizeApproverTrace(raw: unknown): string | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const trace = raw as Record<string, unknown>;
  const decision = cleanText(trace.decision ?? trace.final_decision, 40);
  const parsed = cleanText(trace.parsed_decision ?? trace.parsed, 40);
  const reason = cleanText(trace.reason, 260);
  const error = cleanText(trace.error, 260);
  const skipped = trace.skipped === true;

  return joinDetails([
    decision ? `decision: ${decision}` : undefined,
    parsed ? `parsed: ${parsed}` : undefined,
    skipped ? "skipped: true" : undefined,
    error ? `error: ${error}` : undefined,
    reason ? `reason: ${reason}` : undefined,
  ]);
}
