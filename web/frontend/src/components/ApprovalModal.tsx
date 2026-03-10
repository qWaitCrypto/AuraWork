import React, { useEffect, useState } from "react";
import { Modal } from "./Modal";
import { Button } from "./Button";
import { Badge } from "./Badge";
import type { ApprovalRecord } from "../lib/types";
import { apiFetchArtifact } from "../lib/api";

export interface ApprovalModalProps {
    approval: ApprovalRecord | null;
    currentSessionId: string | null;
    onDecide: (decision: "approve" | "deny") => void;
}

export function ApprovalModal({ approval, currentSessionId, onDecide }: ApprovalModalProps) {
    const [diffText, setDiffText] = useState<string | null>(null);
    const [diffLoading, setDiffLoading] = useState(false);
    const [diffError, setDiffError] = useState<string | null>(null);

    // Fetch diff when approval changes
    useEffect(() => {
        setDiffText(null);
        setDiffLoading(false);
        setDiffError(null);
        if (!approval || !currentSessionId) return;
        const diffRef = approval.diff_ref?.locator;
        if (!diffRef) return;
        setDiffLoading(true);
        apiFetchArtifact(currentSessionId, diffRef)
            .then((text) => setDiffText(typeof text === "string" ? text : null))
            .catch((error: unknown) => {
                setDiffText(null);
                const message = String((error as { message?: unknown } | null)?.message || "Failed to load diff preview.");
                setDiffError(message);
            })
            .finally(() => setDiffLoading(false));
    }, [approval, currentSessionId]);

    return (
        <Modal
            open={Boolean(approval)}
            title="Approval required"
            dismissible={false}
            onClose={() => { }}
            footer={
                <div className="flex justify-end gap-2">
                    <Button onClick={() => onDecide("deny")}>Deny</Button>
                    <Button variant="primary" onClick={() => onDecide("approve")}>Approve</Button>
                </div>
            }
        >
            {approval ? (
                <div className="space-y-3">
                    <div className="grid grid-cols-2 gap-3 text-sm">
                        <div className="rounded-xl border border-surface-200 bg-surface-50 p-3">
                            <div className="text-xs text-ink-500">Approval</div>
                            <div className="mt-1 font-mono text-xs text-ink-700">{approval.approval_id}</div>
                        </div>
                        <div className="rounded-xl border border-surface-200 bg-surface-50 p-3">
                            <div className="text-xs text-ink-500">Risk</div>
                            <div className="mt-1 text-sm font-semibold text-ink-900">{approval.risk_level || "high"}</div>
                        </div>
                    </div>

                    <div className="rounded-xl border border-surface-200 bg-surface-50 p-3">
                        <div className="text-xs text-ink-500">Summary</div>
                        <div className="mt-1 text-sm text-ink-900">{approval.action_summary}</div>
                        {approval.reason ? <div className="mt-2 text-xs text-ink-700">{approval.reason}</div> : null}
                    </div>

                    <div className="rounded-xl border border-surface-200 bg-surface-50 p-3">
                        <div className="flex items-center justify-between">
                            <div className="text-xs text-ink-500">Diff preview</div>
                            {diffLoading ? <Badge tone="gray">loading</Badge> : null}
                            {!diffLoading && diffError ? <Badge tone="red">error</Badge> : null}
                        </div>
                        <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-surface-200 bg-surface-0 p-3 font-mono text-xs text-ink-700">
                            {diffLoading
                                ? "Loading…"
                                : diffError
                                    ? `Load failed: ${diffError}`
                                    : diffText || "No diff preview."}
                        </pre>
                    </div>
                </div>
            ) : null}
        </Modal>
    );
}
