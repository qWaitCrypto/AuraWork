import React from "react";

export type VirtualWindow = {
  enabled: boolean;
  total: number;
  start: number;
  end: number;
  topPadding: number;
  bottomPadding: number;
};

export function useVirtualWindow<T>(opts: {
  containerRef: React.RefObject<HTMLElement | null>;
  items: T[];
  estimateSize: (item: T, index: number) => number;
  enabled?: boolean;
  threshold?: number;
  overscanPx?: number;
  gapPx?: number;
}): VirtualWindow {
  const {
    containerRef,
    items,
    estimateSize,
    enabled = true,
    threshold = 120,
    overscanPx = 900,
    gapPx = 0,
  } = opts;

  const [scrollTop, setScrollTop] = React.useState(0);
  const [viewportHeight, setViewportHeight] = React.useState(0);

  React.useEffect(() => {
    if (!enabled) return;
    const el = containerRef.current;
    if (!el) return;

    let rafId: number | null = null;
    const scheduleUpdate = () => {
      if (rafId != null) return;
      rafId = requestAnimationFrame(() => {
        rafId = null;
        setScrollTop(el.scrollTop);
        setViewportHeight(el.clientHeight);
      });
    };

    scheduleUpdate();

    el.addEventListener("scroll", scheduleUpdate, { passive: true });

    let ro: ResizeObserver | null = null;
    const onWindowResize = () => scheduleUpdate();
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(scheduleUpdate);
      ro.observe(el);
    } else {
      window.addEventListener("resize", onWindowResize);
    }

    return () => {
      if (rafId != null) cancelAnimationFrame(rafId);
      el.removeEventListener("scroll", scheduleUpdate);
      if (ro) {
        ro.disconnect();
      } else {
        window.removeEventListener("resize", onWindowResize);
      }
    };
  }, [containerRef, enabled, items.length]);

  return React.useMemo(() => {
    const total = items.length;
    if (!total) {
      return { enabled: false, total: 0, start: 0, end: 0, topPadding: 0, bottomPadding: 0 };
    }

    if (!enabled || total <= threshold || viewportHeight <= 0) {
      return { enabled: false, total, start: 0, end: total, topPadding: 0, bottomPadding: 0 };
    }

    const prefix: number[] = new Array(total + 1);
    prefix[0] = 0;
    for (let i = 0; i < total; i++) {
      const raw = Number(estimateSize(items[i], i));
      const size = Number.isFinite(raw) ? Math.max(28, raw) : 80;
      const gap = i < total - 1 ? gapPx : 0;
      prefix[i + 1] = prefix[i] + size + gap;
    }

    const totalSize = prefix[total];
    const startY = Math.max(0, scrollTop - overscanPx);
    const endY = scrollTop + viewportHeight + overscanPx;

    let start = 0;
    while (start < total) {
      const itemTop = prefix[start];
      const itemBottom = prefix[start + 1] - (start < total - 1 ? gapPx : 0);
      if (itemBottom >= startY) break;
      start += 1;
    }

    let end = start;
    while (end < total) {
      const itemTop = prefix[end];
      if (itemTop > endY) break;
      end += 1;
    }

    if (end <= start) end = Math.min(total, start + 1);

    return {
      enabled: true,
      total,
      start,
      end,
      topPadding: prefix[start],
      bottomPadding: Math.max(0, totalSize - prefix[end]),
    };
  }, [items, enabled, threshold, viewportHeight, scrollTop, overscanPx, gapPx, estimateSize]);
}
