import React from "react";
import { clsx } from "clsx";

export function Badge(props: { children: React.ReactNode; tone?: "blue" | "gray" | "orange" | "red" }) {
  const tone = props.tone ?? "gray";
  const cls =
    tone === "blue"
      ? "bg-accent-100 text-accent-700 border-accent-100"
      : tone === "orange"
        ? "bg-orange-50 text-orange-700 border-orange-100"
        : tone === "red"
          ? "bg-red-50 text-red-700 border-red-100"
          : "bg-surface-100 text-ink-700 border-surface-200";
  return (
    <span className={clsx("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium", cls)}>
      {props.children}
    </span>
  );
}

