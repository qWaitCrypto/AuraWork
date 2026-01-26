import { create } from "zustand";
import type { AuraEvent } from "../lib/types";

type State = {
  events: AuraEvent[];
  eventIds: Set<string>;
  appendMany: (evs: AuraEvent[]) => void;
  appendOne: (ev: AuraEvent) => void;
  clear: () => void;
};

function sortEvents(a: AuraEvent, b: AuraEvent) {
  if (a.timestamp !== b.timestamp) return a.timestamp - b.timestamp;
  const as = typeof a.sequence === "number" ? a.sequence : Number.MAX_SAFE_INTEGER;
  const bs = typeof b.sequence === "number" ? b.sequence : Number.MAX_SAFE_INTEGER;
  if (as !== bs) return as - bs;
  return a.event_id.localeCompare(b.event_id);
}

export const useEventStore = create<State>((set, get) => ({
  events: [],
  eventIds: new Set(),
  appendMany: (evs) => {
    const ids = new Set(get().eventIds);
    const prev = get().events;
    const merged = [...prev];

    let appended = 0;
    let monotonic = true;
    const last = merged.length ? merged[merged.length - 1] : null;

    for (const ev of evs) {
      if (!ev?.event_id || ids.has(ev.event_id)) continue;
      ids.add(ev.event_id);
      merged.push(ev);
      appended += 1;

      if (monotonic && last) {
        // Fast-path: if batch appears to be non-decreasing by our sort key, we can skip resort.
        if (sortEvents(last, ev) > 0) monotonic = false;
      }
    }

    if (!appended) return;

    if (!last || !monotonic) merged.sort(sortEvents);
    set({ events: merged, eventIds: ids });
  },
  appendOne: (ev) => {
    const ids = new Set(get().eventIds);
    if (!ev?.event_id || ids.has(ev.event_id)) return;
    ids.add(ev.event_id);

    const prev = get().events;
    const last = prev.length ? prev[prev.length - 1] : null;
    const merged = [...prev, ev];

    // Fast-path for strictly appending in-order.
    if (last && sortEvents(last, ev) > 0) merged.sort(sortEvents);
    set({ events: merged, eventIds: ids });
  },
  clear: () => set({ events: [], eventIds: new Set() }),
}));
