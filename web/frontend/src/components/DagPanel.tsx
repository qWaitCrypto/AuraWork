import React, { useEffect, useMemo, useRef } from "react";

function safeId(s: string) {
  return s.replace(/[^a-zA-Z0-9_]/g, "_");
}

export default function DagPanel({ latestPlan }: { latestPlan: any }) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  const code = useMemo(() => {
    if (!latestPlan) return null;
    const plan = (latestPlan.plan as any[]).filter((x) => x && typeof x.id === "string");
    const lines: string[] = ["flowchart TD"];

    for (const item of plan) {
      const id = safeId(item.id);
      const label = String(item.step || item.id).replaceAll('"', "'");
      lines.push(`${id}["${label}"]`);
    }
    for (const item of plan) {
      const id = safeId(item.id);
      const deps = Array.isArray(item.depends_on) ? item.depends_on : [];
      for (const dep of deps) lines.push(`${safeId(dep)} --> ${id}`);
    }

    // Style by status (best-effort)
    for (const item of plan) {
      const st = String(item.status || "pending");
      const id = safeId(item.id);
      if (st === "completed") lines.push(`style ${id} fill:#eff6ff,stroke:#3b82f6,color:#1d4ed8`);
      else if (st === "in_progress") lines.push(`style ${id} fill:#fff7ed,stroke:#f97316,color:#9a3412`);
      else if (st === "failed") lines.push(`style ${id} fill:#fef2f2,stroke:#ef4444,color:#991b1b`);
      else lines.push(`style ${id} fill:#ffffff,stroke:#e4e4e7,color:#18181b`);
    }
    return lines.join("\n");
  }, [latestPlan]);

  const renderGenRef = useRef(0);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    renderGenRef.current += 1;
    const gen = renderGenRef.current;

    if (!code) {
      el.innerHTML = '<div class="text-sm text-ink-500">No DAG plan yet. Create a plan via chat and `update_plan`.</div>';
      return;
    }

    let cancelled = false;

    (async () => {
      const { default: mermaid } = await import("mermaid");
      if (cancelled) return;

      mermaid.initialize({
        startOnLoad: false,
        theme: "default",
        themeVariables: {
          fontFamily: "Inter",
          primaryColor: "#ffffff",
          primaryTextColor: "#3f3f46",
          primaryBorderColor: "#e4e4e7",
          lineColor: "#a1a1aa",
        },
      });

      const id = `mmd_${Math.random().toString(16).slice(2)}`;
      try {
        const res = await mermaid.render(id, code);
        if (cancelled || renderGenRef.current !== gen) return;
        const cur = containerRef.current;
        if (!cur) return;
        cur.innerHTML = res.svg;
      } catch {
        if (cancelled || renderGenRef.current !== gen) return;
        const cur = containerRef.current;
        if (!cur) return;
        cur.innerHTML = `<pre class="text-xs font-mono text-ink-700">${code}</pre>`;
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [code]);

  return <div ref={containerRef} />;
}
