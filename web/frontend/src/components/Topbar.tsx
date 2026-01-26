import React from "react";
import { ChevronDown, Cpu, MoreVertical } from "lucide-react";

export const Topbar = React.memo(function Topbar(props: {
  currentSessionId: string | null;
  modelProfiles: any[];
  sessionMeta: any;
  onChangeChatProfile: (profileId: string) => void;
}) {
  const { currentSessionId, modelProfiles, sessionMeta, onChangeChatProfile } = props;

  return (
    <header className="z-10 flex h-14 flex-shrink-0 items-center justify-between border-b border-surface-200 bg-surface-0/80 px-6 backdrop-blur-sm">
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.4)]" />
          <span className="text-sm font-semibold text-ink-900">Aura Agent</span>
        </div>
        <span className="rounded border border-surface-200 bg-surface-100 px-2 py-0.5 font-mono text-[10px] text-ink-500">web</span>
        {currentSessionId ? <span className="truncate font-mono text-[10px] text-ink-500">{currentSessionId}</span> : null}
      </div>
      <div className="flex items-center gap-4">
        <div
          className="flex items-center gap-2 rounded px-2 py-1 text-xs text-ink-500 hover:bg-surface-100 hover:text-accent-600"
          title="Model"
        >
          <Cpu className="h-3.5 w-3.5" />
          <select
            className="cursor-pointer bg-transparent text-xs text-ink-500 outline-none"
            value={sessionMeta?.chat_profile_id || ""}
            onChange={(e) => onChangeChatProfile(e.target.value || "")}
          >
            <option value="" disabled>
              Select model…
            </option>
            {modelProfiles.map((p) => (
              <option key={p.profile_id} value={p.profile_id}>
                {p.profile_id} · {p.provider_kind}
              </option>
            ))}
          </select>
          <ChevronDown className="h-3 w-3" />
        </div>
        <button
          className="rounded p-1 text-ink-400 transition-colors hover:bg-surface-100 hover:text-ink-700"
          title="Menu"
        >
          <MoreVertical className="h-4 w-4" />
        </button>
      </div>
    </header>
  );
});
