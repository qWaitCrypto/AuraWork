import { create } from "zustand";
import { apiFetchArtifact } from "../lib/api";

type State = {
  texts: Record<string, string>;
  loading: Record<string, boolean>;
  errors: Record<string, string>;
  ensureText: (locator: string) => Promise<string | null>;
  clear: () => void;
};

export const useArtifactStore = create<State>((set, get) => ({
  texts: {},
  loading: {},
  errors: {},
  ensureText: async (locator: string) => {
    const loc = String(locator || "").trim();
    if (!loc) return null;

    const { texts, loading } = get();
    if (texts[loc] !== undefined) return texts[loc];
    if (loading[loc]) return null;

    set((s) => ({ loading: { ...s.loading, [loc]: true } }));
    try {
      const text = await apiFetchArtifact(loc);
      set((s) => ({
        texts: { ...s.texts, [loc]: text },
        loading: { ...s.loading, [loc]: false },
      }));
      return text;
    } catch (e: any) {
      set((s) => ({
        errors: { ...s.errors, [loc]: String(e?.message || e || "fetch_failed") },
        loading: { ...s.loading, [loc]: false },
      }));
      return null;
    }
  },
  clear: () => set({ texts: {}, loading: {}, errors: {} }),
}));
