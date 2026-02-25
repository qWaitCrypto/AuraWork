import { create } from "zustand";
import { apiFetchArtifact } from "../lib/api";

function debugEnabled(): boolean {
  try {
    return typeof window !== "undefined" && localStorage.getItem("AURA_WEB_DEBUG") === "1";
  } catch {
    return false;
  }
}

type TextMap = Record<string, string>;
type BoolMap = Record<string, boolean>;
type SessionRecordMap<T> = Record<string, Record<string, T>>;

const EMPTY_SESSION_RECORD: Record<string, never> = {};

function updateSessionValue<T>(
  source: SessionRecordMap<T>,
  sessionId: string,
  key: string,
  value: T,
): SessionRecordMap<T> {
  const session = source[sessionId] ?? EMPTY_SESSION_RECORD;
  if (Object.is(session[key], value)) return source;
  return {
    ...source,
    [sessionId]: {
      ...session,
      [key]: value,
    },
  };
}

function removeSessionValue<T>(source: SessionRecordMap<T>, sessionId: string, key: string): SessionRecordMap<T> {
  const session = source[sessionId];
  if (!session || !(key in session)) return source;

  const nextSession = { ...session };
  delete nextSession[key];

  if (!Object.keys(nextSession).length) {
    const nextSource = { ...source };
    delete nextSource[sessionId];
    return nextSource;
  }

  return {
    ...source,
    [sessionId]: nextSession,
  };
}

type State = {
  textsBySession: SessionRecordMap<string>;
  fetchedBySession: SessionRecordMap<boolean>;
  loadingBySession: SessionRecordMap<boolean>;
  errorsBySession: SessionRecordMap<string>;
  ensureText: (sessionId: string, locator: string) => Promise<string | null>;
  primeText: (sessionId: string, locator: string, text: string) => void;
  clear: () => void;
};

export const useArtifactStore = create<State>((set, get) => ({
  textsBySession: {},
  fetchedBySession: {},
  loadingBySession: {},
  errorsBySession: {},
  ensureText: async (sessionId: string, locator: string) => {
    const sid = String(sessionId || "").trim();
    const loc = String(locator || "").trim();
    if (!sid || !loc) return null;

    const state = get();
    const texts = state.textsBySession[sid] ?? EMPTY_SESSION_RECORD;
    const loading = state.loadingBySession[sid] ?? EMPTY_SESSION_RECORD;
    const fetched = state.fetchedBySession[sid] ?? EMPTY_SESSION_RECORD;

    const existing = texts[loc];
    if (fetched[loc] && existing !== undefined) return existing;
    if (loading[loc]) return existing ?? null;

    set((s) => {
      const loadingBySession = updateSessionValue(s.loadingBySession, sid, loc, true);
      const errorsBySession = removeSessionValue(s.errorsBySession, sid, loc);
      if (loadingBySession === s.loadingBySession && errorsBySession === s.errorsBySession) return s;
      return { loadingBySession, errorsBySession };
    });

    try {
      const debug = debugEnabled();
      if (debug) console.debug("[artifact] fetch start", { sid, locator: loc });
      const text = await apiFetchArtifact(sid, loc);
      if (debug) console.debug("[artifact] fetch ok", { sid, locator: loc, len: text.length });

      set((s) => {
        const textsBySession = updateSessionValue(s.textsBySession, sid, loc, text);
        const fetchedBySession = updateSessionValue(s.fetchedBySession, sid, loc, true);
        const loadingBySession = updateSessionValue(s.loadingBySession, sid, loc, false);
        const errorsBySession = removeSessionValue(s.errorsBySession, sid, loc);

        if (
          textsBySession === s.textsBySession
          && fetchedBySession === s.fetchedBySession
          && loadingBySession === s.loadingBySession
          && errorsBySession === s.errorsBySession
        ) {
          return s;
        }

        return { textsBySession, fetchedBySession, loadingBySession, errorsBySession };
      });

      return text;
    } catch (error: unknown) {
      const message = String((error as { message?: unknown } | null)?.message || error || "fetch_failed");
      if (debugEnabled()) console.warn("[artifact] fetch failed", { sid, locator: loc, error: message });

      set((s) => {
        const errorsBySession = updateSessionValue(s.errorsBySession, sid, loc, message);
        const loadingBySession = updateSessionValue(s.loadingBySession, sid, loc, false);
        if (errorsBySession === s.errorsBySession && loadingBySession === s.loadingBySession) return s;
        return { errorsBySession, loadingBySession };
      });

      return null;
    }
  },
  primeText: (sessionId: string, locator: string, text: string) => {
    const sid = String(sessionId || "").trim();
    const loc = String(locator || "").trim();
    if (!sid || !loc) return;

    const value = String(text ?? "");

    set((s) => {
      const textsBySession = updateSessionValue(s.textsBySession, sid, loc, value);
      const fetchedBySession = updateSessionValue(s.fetchedBySession, sid, loc, false);
      const loadingBySession = updateSessionValue(s.loadingBySession, sid, loc, false);
      const errorsBySession = removeSessionValue(s.errorsBySession, sid, loc);

      if (
        textsBySession === s.textsBySession
        && fetchedBySession === s.fetchedBySession
        && loadingBySession === s.loadingBySession
        && errorsBySession === s.errorsBySession
      ) {
        return s;
      }

      return { textsBySession, fetchedBySession, loadingBySession, errorsBySession };
    });
  },
  clear: () => set({ textsBySession: {}, fetchedBySession: {}, loadingBySession: {}, errorsBySession: {} }),
}));

export function useArtifactSessionTexts(sessionId: string | null): TextMap {
  return useArtifactStore((s) => {
    if (!sessionId) return EMPTY_SESSION_RECORD as TextMap;
    return s.textsBySession[sessionId] ?? (EMPTY_SESSION_RECORD as TextMap);
  });
}

export function useArtifactSessionFetched(sessionId: string | null): BoolMap {
  return useArtifactStore((s) => {
    if (!sessionId) return EMPTY_SESSION_RECORD as BoolMap;
    return s.fetchedBySession[sessionId] ?? (EMPTY_SESSION_RECORD as BoolMap);
  });
}

export function useArtifactSessionLoading(sessionId: string | null): BoolMap {
  return useArtifactStore((s) => {
    if (!sessionId) return EMPTY_SESSION_RECORD as BoolMap;
    return s.loadingBySession[sessionId] ?? (EMPTY_SESSION_RECORD as BoolMap);
  });
}
