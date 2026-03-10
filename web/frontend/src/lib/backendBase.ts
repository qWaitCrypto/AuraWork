declare global {
  interface Window {
    __AURA_DESKTOP__?: boolean;
    __AURA_BACKEND_HTTP__?: string;
    __AURA_BACKEND_WS__?: string;
    __AURA_BACKEND_TOKEN__?: string;
    __TAURI__?: unknown;
    __TAURI_INTERNALS__?: unknown;
  }
}

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && (window.__TAURI__ !== undefined || window.__TAURI_INTERNALS__ !== undefined);
}

export function isDesktop(): boolean {
  return Boolean(window.__AURA_DESKTOP__) || isTauriRuntime();
}

export function httpBase(): string {
  return String(window.__AURA_BACKEND_HTTP__ || "");
}

export function wsBase(): string {
  return String(window.__AURA_BACKEND_WS__ || "");
}

export function backendToken(): string {
  const fromWindow = String(window.__AURA_BACKEND_TOKEN__ || "").trim();
  if (fromWindow) return fromWindow;
  const fromEnv = String(import.meta.env.VITE_AURA_WEB_TOKEN || "").trim();
  return fromEnv;
}

export async function waitForBackendGlobals(timeoutMs = 10_000): Promise<void> {
  if (!isDesktop()) return;
  if (httpBase() && wsBase() && backendToken()) return;

  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (httpBase() && wsBase() && backendToken()) return;
    await new Promise((r) => setTimeout(r, 50));
  }
}
