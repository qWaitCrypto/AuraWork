import React, { useState, useEffect } from "react";
import { Modal } from "./Modal";
import { Button } from "./Button";
import { isDesktop } from "../lib/backendBase";
import { apiRegisterWorkspace, apiCreateSession, apiBootstrap } from "../lib/api";
import type { Bootstrap, WorkspaceRecord } from "../lib/types";

function basename(p: string) {
    const s = String(p || "");
    const parts = s.split(/[/\\\\]/).filter(Boolean);
    return parts[parts.length - 1] || s || "workspace";
}

function fmtTs(ts: number | null | undefined) {
    if (typeof ts !== "number") return "";
    try { return new Date(ts).toLocaleString(); } catch { return ""; }
}

export interface WorkspacePickerModalProps {
    open: boolean;
    workspaces: WorkspaceRecord[];
    onClose: () => void;
    onSessionCreated: (sessionId: string) => void;
    refreshBootstrap: () => Promise<Bootstrap>;
}

export function WorkspacePickerModal({
    open,
    workspaces,
    onClose,
    onSessionCreated,
    refreshBootstrap,
}: WorkspacePickerModalProps) {
    const [pathDraft, setPathDraft] = useState("");
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState<string | null>(null);

    useEffect(() => {
        if (!open) return;
        refreshBootstrap().catch(() => { });
    }, [open, refreshBootstrap]);

    async function createInWorkspace(workspaceId: string) {
        const wid = String(workspaceId || "").trim();
        if (!wid) return;
        setBusy(true);
        setErr(null);
        try {
            const { session_id } = await apiCreateSession(wid);
            await refreshBootstrap();
            onSessionCreated(session_id);
            onClose();
        } catch (error: unknown) {
            setErr(String((error as { message?: unknown } | null)?.message || error || "create_session_failed"));
        } finally {
            setBusy(false);
        }
    }

    async function registerAndCreate(projectRoot: string) {
        const pr = String(projectRoot || "").trim();
        if (!pr) return;
        setBusy(true);
        setErr(null);
        try {
            const ws = (await apiRegisterWorkspace(pr)) as WorkspaceRecord;
            setPathDraft("");
            await createInWorkspace(ws.workspace_id);
        } catch (error: unknown) {
            setErr(String((error as { message?: unknown } | null)?.message || error || "register_workspace_failed"));
            setBusy(false);
        }
    }

    return (
        <Modal
            open={open}
            title="Select Workspace"
            onClose={() => { if (!busy) onClose(); }}
            footer={
                <div className="flex items-center justify-between gap-2">
                    <div className="text-xs text-rose-600">{err || ""}</div>
                    <div className="flex justify-end gap-2">
                        <Button onClick={() => { if (!busy) onClose(); }}>Close</Button>
                        <Button
                            variant="primary"
                            onClick={() => { void registerAndCreate(pathDraft); }}
                            disabled={busy || !pathDraft.trim()}
                        >
                            {busy ? "Processing…" : "Create & Enter"}
                        </Button>
                    </div>
                </div>
            }
        >
            <div className="space-y-4">
                <div className="rounded-xl border border-surface-200 bg-surface-50 p-3">
                    <div className="text-xs font-semibold text-ink-700">Registered Workspaces</div>
                    <div className="mt-2 space-y-2">
                        {workspaces.length ? (
                            workspaces.map((w) => (
                                <button
                                    key={w.workspace_id}
                                    className="flex w-full items-start justify-between gap-3 rounded-lg border border-surface-200 bg-surface-0 p-3 text-left hover:bg-surface-50 disabled:opacity-60"
                                    onClick={() => void createInWorkspace(w.workspace_id)}
                                    disabled={busy}
                                    title={String(w.project_root || "")}
                                    type="button"
                                >
                                    <div className="min-w-0">
                                        <div className="truncate text-sm font-semibold text-ink-900">{basename(String(w.project_root || ""))}</div>
                                        <div className="truncate font-mono text-[10px] text-ink-500">{String(w.project_root || "")}</div>
                                        {w.last_used_at ? <div className="mt-1 text-[10px] text-ink-400">Last used: {fmtTs(w.last_used_at)}</div> : null}
                                    </div>
                                    <div className="font-mono text-[10px] text-ink-400">{w.workspace_id}</div>
                                </button>
                            ))
                        ) : (
                            <div className="text-sm text-ink-500">No registered workspaces yet.</div>
                        )}
                    </div>
                </div>

                <div className="rounded-xl border border-surface-200 bg-surface-50 p-3">
                    <div className="text-xs font-semibold text-ink-700">Register New Directory</div>
                    <div className="mt-2 flex items-center gap-2">
                        <input
                            className="w-full rounded-lg border border-surface-200 bg-surface-0 px-3 py-2 text-sm font-mono"
                            placeholder="e.g. D:/Work/MyProject or /mnt/d/Work/MyProject"
                            value={pathDraft}
                            onChange={(e) => setPathDraft(e.target.value)}
                            disabled={busy}
                        />
                        {isDesktop() ? (
                            <Button
                                onClick={async () => {
                                    if (busy) return;
                                    try {
                                        const { open: openDialog } = await import("@tauri-apps/plugin-dialog");
                                        const selected = await openDialog({ directory: true, title: "Select Workspace" });
                                        if (typeof selected === "string" && selected.trim()) {
                                            void registerAndCreate(selected);
                                        }
                                    } catch (error: unknown) {
                                        setErr(String((error as { message?: unknown } | null)?.message || error || "desktop_directory_picker_failed"));
                                    }
                                }}
                                disabled={busy}
                            >
                                Browse…
                            </Button>
                        ) : null}
                    </div>
                    <div className="mt-2 text-[11px] text-ink-500">
                        Any local directory will work. If it doesn't exist, it will be created and initialized with .aura/ automatically.
                    </div>
                </div>
            </div>
        </Modal>
    );
}
