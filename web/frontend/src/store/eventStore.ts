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
  const as = typeof a.sequence === "number" ? a.sequence : null;
  const bs = typeof b.sequence === "number" ? b.sequence : null;
  // Prefer sequence when both are present (stable replay across concurrent sources).
  if (as !== null && bs !== null && as !== bs) return as - bs;
  // Fallback to timestamp for older logs that may not include sequence.
  if (a.timestamp !== b.timestamp) return a.timestamp - b.timestamp;
  // Deterministic tie-breaker if one side has sequence and the other doesn't.
  if (as !== null && bs === null) return -1;
  if (as === null && bs !== null) return 1;
  return a.event_id.localeCompare(b.event_id);
}

function isNonDecreasing(events: AuraEvent[], seed: AuraEvent | null) {
  let prev = seed;
  for (const ev of events) {
    if (prev && sortEvents(prev, ev) > 0) return false;
    prev = ev;
  }
  return true;
}

function binaryInsertIndex(events: AuraEvent[], target: AuraEvent) {
  let low = 0;
  let high = events.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (sortEvents(events[mid], target) <= 0) {
      low = mid + 1;
    } else {
      high = mid;
    }
  }
  return low;
}

function mergeSortedEvents(existing: AuraEvent[], incoming: AuraEvent[]) {
  if (!existing.length) return incoming.slice();
  if (!incoming.length) return existing;

  const merged: AuraEvent[] = [];
  let i = 0;
  let j = 0;

  while (i < existing.length && j < incoming.length) {
    if (sortEvents(existing[i], incoming[j]) <= 0) {
      merged.push(existing[i]);
      i += 1;
    } else {
      merged.push(incoming[j]);
      j += 1;
    }
  }
  if (i < existing.length) merged.push(...existing.slice(i));
  if (j < incoming.length) merged.push(...incoming.slice(j));
  return merged;
}

export const useEventStore = create<State>((set) => ({
  events: [],
  eventIds: new Set(),
  appendMany: (evs) => {
    if (!Array.isArray(evs) || !evs.length) return;

    set((state) => {
      let nextIds: Set<string> | null = null;
      const incoming: AuraEvent[] = [];

      for (const ev of evs) {
        if (!ev?.event_id) continue;
        const ids = nextIds ?? state.eventIds;
        if (ids.has(ev.event_id)) continue;
        if (!nextIds) nextIds = new Set(state.eventIds);
        nextIds.add(ev.event_id);
        incoming.push(ev);
      }

      if (!incoming.length || !nextIds) return state;

      const prev = state.events;
      const last = prev.length ? prev[prev.length - 1] : null;

      if (isNonDecreasing(incoming, last)) {
        return { events: prev.concat(incoming), eventIds: nextIds };
      }

      const sortedIncoming = incoming.length > 1 ? [...incoming].sort(sortEvents) : incoming;
      return { events: mergeSortedEvents(prev, sortedIncoming), eventIds: nextIds };
    });
  },
  appendOne: (ev) => {
    if (!ev?.event_id) return;

    set((state) => {
      if (state.eventIds.has(ev.event_id)) return state;

      const nextIds = new Set(state.eventIds);
      nextIds.add(ev.event_id);

      const prev = state.events;
      const last = prev.length ? prev[prev.length - 1] : null;

      if (!last || sortEvents(last, ev) <= 0) {
        return { events: prev.concat(ev), eventIds: nextIds };
      }

      const insertAt = binaryInsertIndex(prev, ev);
      const nextEvents = [...prev.slice(0, insertAt), ev, ...prev.slice(insertAt)];
      return { events: nextEvents, eventIds: nextIds };
    });
  },
  clear: () => set({ events: [], eventIds: new Set() }),
}));
