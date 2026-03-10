import React, { useState, useEffect, useCallback } from "react";
import { CheckCircle, AlertCircle } from "lucide-react";
import { Modal } from "./Modal";
import { Button } from "./Button";
import { apiGetModelSettings, apiPutModelSettings, type GlobalModelSettings } from "../lib/api";

type ProviderKind = "anthropic" | "openai_compatible" | "gemini" | "openai_codex";

const PROVIDER_LABELS: Record<ProviderKind, string> = {
  anthropic: "Anthropic",
  openai_compatible: "OpenAI / Compatible",
  gemini: "Google Gemini",
  openai_codex: "OpenAI Codex",
};

const PROVIDER_BASE_URLS: Record<ProviderKind, string> = {
  anthropic: "https://api.anthropic.com",
  openai_compatible: "https://api.openai.com/v1",
  gemini: "https://generativelanguage.googleapis.com/v1beta",
  openai_codex: "",
};

const PROVIDER_MODEL_PLACEHOLDERS: Record<ProviderKind, string> = {
  anthropic: "e.g. claude-opus-4-5-20251101",
  openai_compatible: "e.g. gpt-4o or a local model name",
  gemini: "e.g. gemini-2.0-flash",
  openai_codex: "e.g. codex-mini-latest",
};

// Providers that require api_key and max_tokens
const REQUIRES_API_KEY: ProviderKind[] = ["anthropic", "gemini", "openai_codex"];
const REQUIRES_MAX_TOKENS: ProviderKind[] = ["anthropic"];

export function SettingsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const [providerKind, setProviderKind] = useState<ProviderKind>("anthropic");
  const [baseUrl, setBaseUrl] = useState(PROVIDER_BASE_URLS.anthropic);
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiKeyHint, setApiKeyHint] = useState("");
  const [apiKeySet, setApiKeySet] = useState(false);
  const [maxTokens, setMaxTokens] = useState<string>("8096");

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setSuccess(false);
    try {
      const s: GlobalModelSettings = await apiGetModelSettings();
      if (s.configured) {
        const pk = (s.provider_kind || "anthropic") as ProviderKind;
        setProviderKind(pk);
        setBaseUrl(s.base_url || PROVIDER_BASE_URLS[pk] || "");
        setModel(s.model || "");
        setApiKey("");
        setApiKeyHint(s.api_key_hint || "");
        setApiKeySet(s.api_key_set ?? false);
        setMaxTokens(s.max_tokens != null ? String(s.max_tokens) : "8096");
      } else {
        setProviderKind("anthropic");
        setBaseUrl(PROVIDER_BASE_URLS.anthropic);
        setModel("");
        setApiKey("");
        setApiKeyHint("");
        setApiKeySet(false);
        setMaxTokens("8096");
      }
    } catch (e: unknown) {
      setErr(String((e as { message?: unknown })?.message || e || "load_failed"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  function handleProviderChange(pk: ProviderKind) {
    setProviderKind(pk);
    setBaseUrl(PROVIDER_BASE_URLS[pk] || "");
    setSuccess(false);
  }

  async function handleSave() {
    setErr(null);
    setSuccess(false);
    if (!model.trim()) { setErr("Model name is required."); return; }
    if (REQUIRES_API_KEY.includes(providerKind) && !apiKey.trim() && !apiKeySet) {
      setErr("API key is required for this provider."); return;
    }
    const mt = parseInt(maxTokens, 10);
    if (REQUIRES_MAX_TOKENS.includes(providerKind) && (isNaN(mt) || mt <= 0)) {
      setErr("Max tokens must be a positive number for this provider."); return;
    }
    setSaving(true);
    try {
      await apiPutModelSettings({
        provider_kind: providerKind,
        base_url: baseUrl.trim(),
        model: model.trim(),
        api_key: apiKey.trim() || undefined,
        max_tokens: REQUIRES_MAX_TOKENS.includes(providerKind) ? (isNaN(mt) ? undefined : mt) : undefined,
      });
      setApiKeySet(apiKey.trim() ? true : apiKeySet);
      if (apiKey.trim()) {
        setApiKeyHint(apiKey.trim().slice(0, 6) + "…");
        setApiKey("");
      }
      setSuccess(true);
    } catch (e: unknown) {
      setErr(String((e as { message?: unknown })?.message || e || "save_failed"));
    } finally {
      setSaving(false);
    }
  }

  const showMaxTokens = REQUIRES_MAX_TOKENS.includes(providerKind);
  const requiresApiKey = REQUIRES_API_KEY.includes(providerKind);

  return (
    <Modal
      open={open}
      title="Settings"
      onClose={onClose}
      footer={
        <div className="flex items-center justify-between gap-2">
          <div className="text-xs text-ink-400">
            Saved to <code className="rounded bg-surface-100 px-1 font-mono">~/.aura/config/models.json</code>
          </div>
          <div className="flex gap-2">
            <Button onClick={onClose} disabled={saving}>Close</Button>
            <Button variant="primary" onClick={() => void handleSave()} disabled={loading || saving}>
              {saving ? "Saving…" : "Save"}
            </Button>
          </div>
        </div>
      }
    >
      <div className="space-y-5">
        {/* ── Section: Global API ─────────────────────────────────── */}
        <div>
          <div className="mb-3 text-xs font-semibold uppercase tracking-wide text-ink-400">
            Default API Configuration
          </div>
          <div className="rounded-xl border border-surface-200 bg-surface-50 p-4 space-y-4">
            <div className="text-xs text-ink-500">
              Applies to all workspaces unless overridden by a project-level{" "}
              <code className="rounded bg-surface-100 px-1 font-mono">.aura/config/models.json</code>.
            </div>

            {loading ? (
              <div className="text-xs text-ink-400">Loading…</div>
            ) : (
              <div className="space-y-3">
                {/* Provider */}
                <div className="flex items-center gap-3">
                  <label className="w-28 flex-shrink-0 text-xs font-medium text-ink-600">Provider</label>
                  <select
                    className="flex-1 rounded-lg border border-surface-200 bg-surface-0 px-2.5 py-1.5 text-sm"
                    value={providerKind}
                    onChange={(e) => handleProviderChange(e.target.value as ProviderKind)}
                    disabled={saving}
                  >
                    {(Object.keys(PROVIDER_LABELS) as ProviderKind[]).map((pk) => (
                      <option key={pk} value={pk}>{PROVIDER_LABELS[pk]}</option>
                    ))}
                  </select>
                </div>

                {/* Base URL */}
                <div className="flex items-center gap-3">
                  <label className="w-28 flex-shrink-0 text-xs font-medium text-ink-600">Base URL</label>
                  <input
                    className="flex-1 rounded-lg border border-surface-200 bg-surface-0 px-2.5 py-1.5 font-mono text-sm"
                    value={baseUrl}
                    onChange={(e) => { setBaseUrl(e.target.value); setSuccess(false); }}
                    placeholder="https://…"
                    disabled={saving}
                  />
                </div>

                {/* Model */}
                <div className="flex items-center gap-3">
                  <label className="w-28 flex-shrink-0 text-xs font-medium text-ink-600">Model</label>
                  <input
                    className="flex-1 rounded-lg border border-surface-200 bg-surface-0 px-2.5 py-1.5 font-mono text-sm"
                    value={model}
                    onChange={(e) => { setModel(e.target.value); setSuccess(false); }}
                    placeholder={PROVIDER_MODEL_PLACEHOLDERS[providerKind]}
                    disabled={saving}
                  />
                </div>

                {/* API Key */}
                <div className="flex items-center gap-3">
                  <label className="w-28 flex-shrink-0 text-xs font-medium text-ink-600">
                    API Key
                    {!requiresApiKey && <span className="ml-1 text-ink-400">(optional)</span>}
                  </label>
                  <div className="flex flex-1 flex-col gap-1">
                    <input
                      type="password"
                      className="w-full rounded-lg border border-surface-200 bg-surface-0 px-2.5 py-1.5 font-mono text-sm"
                      value={apiKey}
                      onChange={(e) => { setApiKey(e.target.value); setSuccess(false); }}
                      placeholder={apiKeySet ? `Current: ${apiKeyHint} — type to replace` : "sk-…"}
                      disabled={saving}
                      autoComplete="new-password"
                    />
                  </div>
                </div>

                {/* Max Tokens */}
                {showMaxTokens && (
                  <div className="flex items-center gap-3">
                    <label className="w-28 flex-shrink-0 text-xs font-medium text-ink-600">Max Tokens</label>
                    <input
                      type="number"
                      className="w-36 rounded-lg border border-surface-200 bg-surface-0 px-2.5 py-1.5 font-mono text-sm"
                      value={maxTokens}
                      onChange={(e) => { setMaxTokens(e.target.value); setSuccess(false); }}
                      min={1}
                      disabled={saving}
                    />
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        {/* ── Status / error ──────────────────────────────────────── */}
        {err && (
          <div className="flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" />
            {err}
          </div>
        )}
        {success && (
          <div className="flex items-center gap-2 rounded-lg border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-700">
            <CheckCircle className="h-3.5 w-3.5 flex-shrink-0" />
            Saved. New sessions will use this configuration.
          </div>
        )}
      </div>
    </Modal>
  );
}
