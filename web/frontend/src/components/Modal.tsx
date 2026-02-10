import React, { useEffect } from "react";

export function Modal(props: {
  open: boolean;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  onClose: () => void;
  dismissible?: boolean;
}) {
  const dismissible = props.dismissible ?? true;

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (!dismissible) return;
      if (e.key === "Escape") props.onClose();
    }
    if (props.open) window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [props.open, props.onClose, dismissible]);

  if (!props.open) return null;
  return (
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-ink-900/30" onClick={dismissible ? props.onClose : undefined} aria-hidden="true" />
      <div className="absolute left-1/2 top-1/2 w-[min(900px,calc(100%-24px))] -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-surface-200 bg-surface-0 shadow-elevated">
        <div className="flex items-center justify-between border-b border-surface-200 px-4 py-3">
          <div className="text-sm font-semibold">{props.title}</div>
          {dismissible ? (
            <button
              className="rounded-lg px-2 py-1 text-ink-500 hover:bg-surface-100 hover:text-ink-700"
              aria-label="Close"
              onClick={props.onClose}
            >
              ×
            </button>
          ) : null}
        </div>
        <div className="max-h-[70vh] overflow-auto px-4 py-3">{props.children}</div>
        {props.footer ? <div className="border-t border-surface-200 bg-surface-50 px-4 py-3">{props.footer}</div> : null}
      </div>
    </div>
  );
}

