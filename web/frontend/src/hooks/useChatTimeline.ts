import { useMemo } from "react";
import type { AuraEvent } from "../lib/types";
import type { ChatItem, ChatMessage, TimelineCard, TimelineRow, ToolRun } from "../types";
import {
  cleanText,
  formatWorkSpecDetails,
  formatWorkSpecSummary,
  joinDetails,
  normalizeApproverDecision,
  parseWorkSpecView,
  summarizeApproverTrace,
} from "../lib/workSpecView";

function asRecord(raw: unknown): Record<string, unknown> | null {
  return raw && typeof raw === "object" ? (raw as Record<string, unknown>) : null;
}

type PendingUserMessage = { id: string; text: string; ts: number } | null;

export function useChatTimeline(opts: {
  chatMessages: ChatMessage[];
  events: AuraEvent[];
  pendingUserMessage: PendingUserMessage;
  toolRuns: ToolRun[];
}): ChatItem[] {
  const { chatMessages, events, pendingUserMessage, toolRuns } = opts;

  return useMemo<ChatItem[]>(() => {
    const items: ChatItem[] = [];

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

    const toolById = new Map<string, ToolRun>();
    for (const tr of toolRuns) toolById.set(tr.id, tr);

    const timelineByKey = new Map<string, TimelineCard>();

    for (const [k, g] of groups.entries()) {
      const rows: TimelineRow[] = [];

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
        const thinkingLocator = (() => {
          const ref = asRecord(llmEnd?.payload.thinking_ref);
          const loc = ref?.locator;
          return typeof loc === "string" && loc.trim() ? loc : undefined;
        })();
        rows.push({
          key: `llm:${k}`,
          kind: "llm",
          title: "Thinking",
          status,
          startedAt: startedAt ?? undefined,
          endedAt: endedAt ?? undefined,
          durationMs,
          thinkingLocator,
        });
      }

      const toolIds = new Set<string>();
      for (const e of g.events) {
        if (e.kind !== "tool_call_start" && e.kind !== "tool_call_end") continue;
        const payload = e.payload;
        const id = String(payload.tool_execution_id || payload.tool_call_id || e.event_id);
        toolIds.add(id);
      }
      for (const id of toolIds) {
        const tr = toolById.get(id);
        if (!tr) continue;
        const wsSummary = formatWorkSpecSummary(tr.workSpec);
        const wsDetails = formatWorkSpecDetails(tr.workSpec);
        rows.push({
          key: `tool:${id}`,
          kind: "tool",
          title: tr.summary,
          subtitle: wsSummary ? `${tr.tool} · ${wsSummary}` : tr.tool,
          details: wsDetails,
          status: tr.status,
          startedAt: tr.startedAt,
          endedAt: tr.endedAt,
          durationMs: tr.durationMs,
          toolRunId: tr.id,
          onOpenTab: "terminal",
        });
      }

      for (const e of g.events) {
        if (e.kind !== "plan_update") continue;
        const planLen = Array.isArray(e.payload.plan) ? e.payload.plan.length : undefined;
        rows.push({
          key: `plan:${e.event_id}`,
          kind: "plan",
          title: "Plan updated",
          subtitle: typeof planLen === "number" ? `${planLen} steps` : undefined,
          startedAt: e.timestamp,
          onOpenTab: "plan",
        });
      }

      for (const e of g.events) {
        const payload = e.payload;

        if (e.kind === "subagent_approver_started") {
          const inspection = asRecord(payload.inspection);
          const actionSummary = cleanText(inspection?.action_summary, 180) || cleanText(payload.tool_name, 80) || "Approver started";
          const risk = cleanText(inspection?.risk_level, 40);
          const reason = cleanText(inspection?.reason, 260);
          const ws = parseWorkSpecView(payload.work_spec);
          rows.push({
            key: `approver_start:${e.event_id}`,
            kind: "approval",
            title: `Approver started · ${actionSummary}`,
            subtitle: risk ? `risk: ${risk}` : undefined,
            details: joinDetails([
              reason ? `reason: ${reason}` : undefined,
              formatWorkSpecDetails(ws),
            ]),
            status: "running",
            startedAt: e.timestamp,
            onOpenTab: "terminal",
          });
          continue;
        }

        if (e.kind === "subagent_approver_completed") {
          const after = asRecord(payload.inspection_after);
          const decision = normalizeApproverDecision(after?.decision);
          const status = decision === "allow" ? "succeeded" : decision === "deny" ? "blocked" : decision === "escalate" ? "needs_approval" : "unknown";
          const actionSummary = cleanText(after?.action_summary, 180) || cleanText(payload.tool_name, 80) || "Approver completed";
          const reason = cleanText(after?.reason, 260);
          const trace = summarizeApproverTrace(payload.approver_trace);
          rows.push({
            key: `approver_done:${e.event_id}`,
            kind: "approval",
            title: `Approver decision · ${decision}`,
            subtitle: actionSummary,
            details: joinDetails([
              reason ? `reason: ${reason}` : undefined,
              trace,
            ]),
            status,
            startedAt: e.timestamp,
            onOpenTab: "terminal",
          });
          continue;
        }

        if (e.kind === "approval_required") {
          const actionSummary = cleanText(payload.action_summary, 180) || "Approval required";
          const risk = cleanText(payload.risk_level, 40);
          const reason = cleanText(payload.reason, 260);
          const approvalId = cleanText(payload.approval_id, 120);
          rows.push({
            key: `approval_required:${e.event_id}`,
            kind: "approval",
            title: actionSummary,
            subtitle: risk ? `risk: ${risk}` : "Approval required",
            details: joinDetails([
              approvalId ? `approval_id: ${approvalId}` : undefined,
              reason ? `reason: ${reason}` : undefined,
            ]),
            status: "needs_approval",
            startedAt: e.timestamp,
            onOpenTab: "terminal",
          });
          continue;
        }

        if (e.kind === "run_paused") {
          const pendingCount = Array.isArray(payload.pending_tools) ? payload.pending_tools.length : undefined;
          rows.push({
            key: `run_paused:${e.event_id}`,
            kind: "approval",
            title: "Run paused",
            subtitle: typeof pendingCount === "number" ? `pending tools: ${pendingCount}` : "Awaiting approval",
            details: cleanText(payload.approval_id, 120) ? `approval_id: ${String(payload.approval_id)}` : undefined,
            status: "needs_approval",
            startedAt: e.timestamp,
            onOpenTab: "terminal",
          });
          continue;
        }

        if (e.kind === "approval_granted") {
          const approvalId = cleanText(payload.approval_id, 120);
          rows.push({
            key: `approval_granted:${e.event_id}`,
            kind: "approval",
            title: "Approval granted",
            subtitle: approvalId,
            details: cleanText(payload.decision, 40) ? `decision: ${String(payload.decision)}` : undefined,
            status: "succeeded",
            startedAt: e.timestamp,
            onOpenTab: "terminal",
          });
          continue;
        }

        if (e.kind === "approval_denied") {
          const approvalId = cleanText(payload.approval_id, 120);
          rows.push({
            key: `approval_denied:${e.event_id}`,
            kind: "approval",
            title: "Approval denied",
            subtitle: approvalId,
            details: cleanText(payload.decision, 40) ? `decision: ${String(payload.decision)}` : undefined,
            status: "blocked",
            startedAt: e.timestamp,
            onOpenTab: "terminal",
          });
          continue;
        }

        if (e.kind === "run_resumed") {
          const pendingCount = Number.isFinite(Number(payload?.pending_tools_count)) ? Number(payload.pending_tools_count) : undefined;
          rows.push({
            key: `run_resumed:${e.event_id}`,
            kind: "approval",
            title: "Run resumed",
            subtitle: typeof pendingCount === "number" ? `pending tools: ${pendingCount}` : undefined,
            details: cleanText(payload.approval_id, 120) ? `approval_id: ${String(payload.approval_id)}` : undefined,
            status: "running",
            startedAt: e.timestamp,
            onOpenTab: "terminal",
          });
        }
      }

      for (const e of g.events) {
        if (e.kind !== "operation_failed") continue;
        const msg = String(e.payload.error || e.payload.message || e.kind);
        rows.push({ key: `err:${e.event_id}`, kind: "error", title: msg, status: "failed", startedAt: e.timestamp });
      }

      if (rows.length) {
        rows.sort((a, b) => (a.startedAt ?? 0) - (b.startedAt ?? 0));
        timelineByKey.set(k, { id: `tl_${k}`, ts: g.ts, requestId: g.requestId, turnId: g.turnId, rows });
      }
    }

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
    for (const it of msgs) items.push(it.item);
    return items;
  }, [chatMessages, events, pendingUserMessage, toolRuns]);
}
