import { create } from "zustand";
import type { ApprovalRecord, Bootstrap, SessionSummary } from "../lib/types";

type State = {
  bootstrap: Bootstrap | null;
  sessions: SessionSummary[];
  currentSessionId: string | null;
  sessionMeta: any | null;
  approvals: ApprovalRecord[];
  activeApproval: ApprovalRecord | null;

  setBootstrap: (b: Bootstrap) => void;
  setSessions: (s: SessionSummary[]) => void;
  setCurrentSession: (id: string | null) => void;
  setSessionMeta: (meta: any) => void;
  setApprovals: (a: ApprovalRecord[]) => void;
  popApproval: () => void;
};

export const useUiStore = create<State>((set, get) => ({
  bootstrap: null,
  sessions: [],
  currentSessionId: null,
  sessionMeta: null,
  approvals: [],
  activeApproval: null,

  setBootstrap: (b) => set({ bootstrap: b, sessions: b.sessions || [] }),
  setSessions: (s) => set({ sessions: s }),
  setCurrentSession: (id) => set({ currentSessionId: id }),
  setSessionMeta: (meta) => set({ sessionMeta: meta }),
  setApprovals: (a) => set({ approvals: a, activeApproval: a[0] ?? null }),
  popApproval: () => {
    const all = get().approvals;
    const next = all.slice(1);
    set({ approvals: next, activeApproval: next[0] ?? null });
  },
}));

