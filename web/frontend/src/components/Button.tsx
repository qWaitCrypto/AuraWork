import React from "react";
import { clsx } from "clsx";

export function Button(
  props: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" | "outline" }
) {
  const v = props.variant ?? "outline";
  const base =
    "inline-flex items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-accent-500/30 disabled:opacity-50 disabled:cursor-not-allowed";
  const cls =
    v === "primary"
      ? "bg-accent-600 text-white hover:bg-accent-700 border border-accent-600 shadow-soft"
      : v === "ghost"
        ? "bg-transparent hover:bg-surface-100 text-ink-700"
        : "bg-surface-0 hover:bg-surface-100 text-ink-900 border border-surface-200 shadow-soft";
  return (
    <button {...props} className={clsx(base, cls, props.className)}>
      {props.children}
    </button>
  );
}

