import React, { useMemo } from "react";
import { Check, ChevronDown, Circle, Loader2, X } from "lucide-react";

type PlanStep = {
  id: string;
  step?: string;
  status?: "pending" | "in_progress" | "completed" | "failed";
  depends_on?: string[];
};

const STATUS_CONFIG = {
  pending: {
    icon: Circle,
    color: "text-ink-400",
    bg: "bg-surface-50",
    border: "border-surface-200",
    label: "Pending",
  },
  in_progress: {
    icon: Loader2,
    color: "text-amber-500",
    bg: "bg-amber-50",
    border: "border-amber-300",
    label: "Running",
  },
  completed: {
    icon: Check,
    color: "text-accent-600",
    bg: "bg-accent-50",
    border: "border-accent-200",
    label: "Done",
  },
  failed: {
    icon: X,
    color: "text-rose-500",
    bg: "bg-rose-50",
    border: "border-rose-200",
    label: "Failed",
  },
} as const;

function clampLabel(label: string, max = 28) {
  const s = String(label || "").trim();
  if (s.length <= max) return s || "Untitled step";
  return `${s.slice(0, max)}…`;
}

function clampId(id: string, max = 10) {
  const s = String(id || "").trim();
  if (s.length <= max) return s || "—";
  return `${s.slice(0, max)}…`;
}

export default function DagPanel({ latestPlan }: { latestPlan: any }) {
  const steps = useMemo<PlanStep[]>(() => {
    const raw = (latestPlan as any)?.plan;
    if (!Array.isArray(raw)) return [];
    return raw.filter((x) => x && typeof x.id === "string") as PlanStep[];
  }, [latestPlan]);

  if (!steps.length) {
    return (
      <div className="flex flex-col items-center justify-center py-10 text-ink-400">
        <Circle className="mb-2 h-8 w-8 opacity-30" />
        <div className="text-sm">No plan yet</div>
        <div className="mt-1 text-xs text-ink-400">Create a plan via chat and `update_plan`.</div>
      </div>
    );
  }

  return (
    <div className="flex flex-col">
      {steps.map((step, idx) => {
        const status = (step.status || "pending") as keyof typeof STATUS_CONFIG;
        const cfg = STATUS_CONFIG[status] ?? STATUS_CONFIG.pending;
        const Icon = cfg.icon;
        const label = String(step.step || step.id);
        const deps = Array.isArray(step.depends_on) ? step.depends_on : [];
        const isLast = idx === steps.length - 1;

        return (
          <div key={step.id} className="relative">
            {idx > 0 ? <div className="absolute left-4 -top-3 h-3 w-0.5 bg-surface-200" /> : null}

            <div
              className={[
                "relative rounded-lg border p-3 transition-all",
                cfg.bg,
                cfg.border,
                status === "in_progress" ? "shadow-medium ring-2 ring-amber-200/70" : "shadow-soft",
              ].join(" ")}
            >
              <div className="flex items-start gap-2.5">
                <div className={["mt-0.5 flex-shrink-0", cfg.color].join(" ")}>
                  <Icon className={["h-4 w-4", status === "in_progress" ? "animate-spin" : ""].join(" ")} />
                </div>

                <div className="min-w-0 flex-1">
                  <div
                    className={[
                      "truncate text-sm font-medium",
                      status === "completed"
                        ? "text-accent-700"
                        : status === "failed"
                          ? "text-rose-700"
                          : status === "in_progress"
                            ? "text-amber-700"
                            : "text-ink-700",
                    ].join(" ")}
                    title={label}
                  >
                    {clampLabel(label)}
                  </div>

                  {deps.length ? (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {deps.slice(0, 3).map((d) => (
                        <span
                          key={d}
                          title={d}
                          className="inline-flex items-center rounded-md bg-surface-100 px-1.5 py-0.5 text-[10px] text-ink-500"
                        >
                          ← {clampId(d, 12)}
                        </span>
                      ))}
                      {deps.length > 3 ? <span className="text-[10px] text-ink-400">+{deps.length - 3}</span> : null}
                    </div>
                  ) : null}
                </div>

                <span className={["flex-shrink-0 rounded bg-white/50 px-1.5 py-0.5 text-[10px] font-medium", cfg.color].join(" ")}>
                  {cfg.label}
                </span>
              </div>
            </div>

            {!isLast ? (
              <div className="flex justify-center py-1">
                <ChevronDown className="h-3 w-3 text-surface-200" />
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

