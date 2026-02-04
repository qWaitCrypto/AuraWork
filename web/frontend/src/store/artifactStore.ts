import { create } from "zustand";
import { apiFetchArtifact } from "../lib/api";

export function artifactKey(sessionId: string, locator: string) {
  return `${sessionId}::${locator}`;
}

function debugEnabled(): boolean {
  try {
    return typeof window !== "undefined" && localStorage.getItem("AURA_WEB_DEBUG") === "1";
  } catch {
    return false;
  }
}

type State = {
  texts: Record<string, string>; // key = `${sessionId}::${locator}`
  fetched: Record<string, boolean>;
  loading: Record<string, boolean>;
  errors: Record<string, string>;
  ensureText: (sessionId: string, locator: string) => Promise<string | null>;
  primeText: (sessionId: string, locator: string, text: string) => void;
  clear: () => void;
};

export const useArtifactStore = create<State>((set, get) => ({
  texts: {},
  fetched: {},
  loading: {},
  errors: {},
  ensureText: async (sessionId: string, locator: string) => {
    const sid = String(sessionId || "").trim();
    const loc = String(locator || "").trim();
    if (!sid || !loc) return null;

    const key = artifactKey(sid, loc);
    const { texts, loading, fetched } = get();
    const existing = texts[key];
    if (fetched[key] && existing !== undefined) return existing;
    if (loading[key]) return existing ?? null;

    set((s) => ({ loading: { ...s.loading, [key]: true } }));
    try {
      const debug = debugEnabled();
      if (debug) console.debug("[artifact] fetch start", { sid, locator: loc });
      const text = await apiFetchArtifact(sid, loc);
      if (debug) console.debug("[artifact] fetch ok", { sid, locator: loc, len: text.length });
      set((s) => ({
        texts: { ...s.texts, [key]: text },
        fetched: { ...s.fetched, [key]: true },
        loading: { ...s.loading, [key]: false },
      }));
      return text;
    } catch (e: any) {
      if (debugEnabled()) console.warn("[artifact] fetch failed", { sid, locator: loc, error: String(e?.message || e || "fetch_failed") });
      set((s) => ({
        errors: { ...s.errors, [key]: String(e?.message || e || "fetch_failed") },
        loading: { ...s.loading, [key]: false },
      }));
      return null;
    }
  },
  primeText: (sessionId: string, locator: string, text: string) => {
    const sid = String(sessionId || "").trim();
    const loc = String(locator || "").trim();
    if (!sid || !loc) return;
    const key = artifactKey(sid, loc);

    set((s) => {
      const errors = { ...s.errors };
      delete errors[key];
      return {
        texts: { ...s.texts, [key]: String(text ?? "") },
        fetched: { ...s.fetched, [key]: false },
        loading: { ...s.loading, [key]: false },
        errors,
      };
    });
  },
  clear: () => set({ texts: {}, fetched: {}, loading: {}, errors: {} }),
}));
