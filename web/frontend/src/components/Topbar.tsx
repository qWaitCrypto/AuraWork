import React from "react";
import { ChevronDown, Cpu, Moon, Sun } from "lucide-react";
import type { ModelProfile, SessionMeta } from "../lib/types";

export const Topbar = React.memo(function Topbar(props: {
  currentSessionId: string | null;
  modelProfiles: ModelProfile[];
  sessionMeta: SessionMeta | null;
  onChangeChatProfile: (profileId: string) => void;
  theme: "light" | "dark";
  onToggleTheme: () => void;
}) {
  const { currentSessionId, modelProfiles, sessionMeta, onChangeChatProfile, theme, onToggleTheme } = props;
  const [dropdownOpen, setDropdownOpen] = React.useState(false);
  const dropdownRef = React.useRef<HTMLDivElement | null>(null);

  React.useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const isDark = theme === "dark";

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
        <div className="relative" ref={dropdownRef}>
          <button
            onClick={() => setDropdownOpen(!dropdownOpen)}
            className="flex items-center gap-2 rounded-lg border border-surface-200 bg-surface-0 px-3 py-1.5 text-xs font-medium text-ink-700 shadow-soft transition-all hover:border-accent-400 hover:shadow-medium"
            title="Model"
            type="button"
          >
            <Cpu className="h-3.5 w-3.5 text-accent-500" />
            <span className="max-w-[140px] truncate">
              {sessionMeta?.chat_profile_id
                ? modelProfiles.find((profile) => profile.profile_id === sessionMeta.chat_profile_id)?.profile_id || "Select model…"
                : "Select model…"}
            </span>
            <ChevronDown className={`h-3 w-3 transition-transform ${dropdownOpen ? "rotate-180" : ""}`} />
          </button>

          {dropdownOpen && (
            <div className="absolute right-0 top-full z-50 mt-1 min-w-[240px] rounded-xl border border-surface-200 bg-surface-0 py-1 shadow-large animate-fade-in">
              {modelProfiles.map((profile) => {
                const isActive = profile.profile_id === sessionMeta?.chat_profile_id;
                return (
                  <button
                    key={profile.profile_id}
                    onClick={() => {
                      onChangeChatProfile(profile.profile_id);
                      setDropdownOpen(false);
                    }}
                    className={`w-full px-3 py-2 text-left transition-colors hover:bg-surface-100 ${isActive ? "bg-accent-50" : ""}`}
                    type="button"
                  >
                    <div className={`text-xs font-medium ${isActive ? "text-accent-700" : "text-ink-700"}`}>
                      {profile.profile_id}
                    </div>
                    <div className="mt-0.5 text-[10px] text-ink-400">{profile.provider_kind}</div>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <button
          className="rounded-lg border border-surface-200 bg-surface-0 p-1.5 text-ink-500 shadow-soft transition-colors hover:bg-surface-100 hover:text-ink-700"
          title={isDark ? "Switch to light mode" : "Switch to dark mode"}
          onClick={onToggleTheme}
          type="button"
        >
          {isDark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </button>
      </div>
    </header>
  );
});
