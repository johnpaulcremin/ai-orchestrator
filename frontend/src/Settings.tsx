import { useCallback, useEffect, useRef, useState } from "react";
import { useModalFocus } from "./useModalFocus";
import { Users } from "./Users";
import { authFailureMessage } from "./format";

export type SettingItem = {
  key: string;
  label: string;
  category?: string;
  tier?: string;
  effective_model: string;
  source: "override" | "env" | "default";
  override: string | null;
  env: string | null;
  default?: string;
  inherits?: string;
  provider: string;
  key_env: string;
  key_present: boolean | null;
};

export type FeatureFlagItem = {
  key: string;
  label: string;
  description: string;
  effective_enabled: boolean;
  source: "override" | "env" | "default";
  override: string | null;
  env: string | null;
  default: boolean;
};

export type PromptItem = {
  key: string;
  category: string;
  label: string;
  effective_prompt: string;
  source: "override" | "env" | "default";
  override: string | null;
  env: string | null;
  default: string;
};

export type FreeLaneItem = {
  key: string;
  label: string;
  effective_value: string;
  source: "override" | "env" | "default";
  override: string | null;
  env: string | null;
  default: string;
};

// Same shape as FreeLaneItem (RETENTION_DAYS_DETAIL/SHARE_EXPIRY_DAYS are
// plain string-valued settings, describe_settings() reports them
// identically) — a distinct alias rather than reusing FreeLaneItem directly
// so retentionRow's own placeholder logic isn't tied to freeLaneRow's.
export type RetentionItem = FreeLaneItem;

export type SettingsView = {
  editable: boolean;
  tiers: SettingItem[];
  categories: SettingItem[];
  features: FeatureFlagItem[];
  prompts: PromptItem[];
  free_lane: FreeLaneItem[];
  retention: RetentionItem[];
  // Whether ADMIN_USERNAMES-gated multi-user mode is active, and whether
  // the caller is one of those admins — `editable` already folds in this
  // check (false for a locked-out non-admin), these two just let the
  // banner distinguish "ALLOW_SETTINGS_WRITE=false" from "not an admin"
  // and let the Users section gate its own visibility.
  admin_gated: boolean;
  is_admin: boolean;
};

type ModelCatalogStatus = {
  enabled: boolean;
  synced_at: string | null;
  model_count: number;
  // Optional in the type (not just guarded at the render site below) since
  // this is exactly the shape of field that's absent from a malformed or
  // partial backend response rather than a genuine empty list — see the
  // `?? []` guard where it's read.
  new_models?: string[];
  stale: boolean;
  error?: string | null;
};

// GET/DELETE /v1/memory and GET/DELETE /v1/semantic-cache share this exact
// shape (see app/memory.py's and app/semantic_cache.py's stats()) — one type
// for both, same as the backend's own symmetry.
type RecallCacheStats = {
  enabled: boolean;
  entries: number;
  threshold: number;
  max_entries: number;
  // Only memory's stats() reports this (its top-k recall limit); absent
  // from semantic-cache's response.
  top_k?: number;
};

type CorrectionStat = {
  flagged: number;
  answers: number;
  correction_rate: number;
};

// GET /v1/correction/summary's response shape (app/correction_tracking.py's
// summarize(), folded with retention rollups — see app/routers/usage.py).
type CorrectionSummary = {
  overall: CorrectionStat;
  by_model: Record<string, CorrectionStat>;
};

// GET /v1/fallback/summary's response shape (app/fallback_reason.py's
// tally, folded with retention rollups).
type FallbackSummary = {
  reasons: { reason: string; count: number }[];
};

// GET /v1/retry-cost/summary's response shape (app/retry_cost.py's
// summarize()). `reads_as` is the sufficiency verdict on the rate, and the
// reason this panel never prints the percentage on its own: on a small
// deployment the sample is tiny, and a bare "40%" would read as a finding.
type RetryStat = {
  turns: number;
  retried_turns: number;
  retries: number;
  continued_turns: number;
  continuations: number;
  retry_rate: number;
  retry_rate_ci: [number, number] | null;
  turns_for_directional: number | null;
  reads_as: "no_data" | "insufficient" | "directional";
  first_attempt_cost_usd: number;
  total_cost_usd: number;
  cost_multiplier: number | null;
};

type RetryCostSummary = {
  overall: RetryStat;
  by_signal: Record<string, { retries: number; retry_cost_usd: number }>;
};

// Mirrors app/retry_attribution.py's SIGNAL_LABELS, duplicated for the same
// reason as FALLBACK_REASON_LABELS below: they're static, and this app has no
// GET-the-labels endpoint.
const RETRY_SIGNAL_LABELS: Record<string, string> = {
  regenerated_unrated: "regenerated, unrated (may be taste)",
  regenerated_after_downvote: "regenerated after 👎 (quality failure)",
  regenerated_after_upvote: "regenerated after 👍 (not a failure)",
  edited: "edited and re-asked",
  continued: "continued a cut-off answer (the cap was too small)",
};

const asPercent = (value: number) => `${Math.round(value * 100)}%`;
const asUsd = (value: number) => `$${value.toFixed(2)}`;

/**
 * The retry rate, its n, its interval and — when the sample is too small to
 * support a conclusion — how many turns it would take before it could be. The
 * bare percentage is deliberately never rendered on its own: see
 * app/retry_cost.py's docstring, and the weekly report's _fmt_retry_rate,
 * which this mirrors.
 */
function retryRateText(stat: RetryStat): string {
  let text = `${asPercent(stat.retry_rate)} (${stat.retried_turns}/${stat.turns} turns)`;
  if (stat.retry_rate_ci) {
    text += `, 95% CI ${asPercent(stat.retry_rate_ci[0])}–${asPercent(stat.retry_rate_ci[1])}`;
  }
  if (stat.reads_as === "insufficient") {
    text += ", too few to be a finding";
    if (stat.turns_for_directional) {
      text += ` (~${stat.turns_for_directional} turns at this rate would be)`;
    }
  }
  return text;
}

// Mirrors app/fallback_reason.py's REASON_LABELS — this app has no
// GET-the-labels endpoint (they're static), so the same short list is
// duplicated here, same as e.g. tool_labels in self_report.py's own
// rendering being independent of its backend source of truth.
const FALLBACK_REASON_LABELS: Record<string, string> = {
  context_length_exceeded: "context-length exceeded",
  timeout: "timeout",
  connection_error: "connection refused",
  quota_cooldown: "quota/cooldown",
  tool_unsupported: "tool unsupported by that model",
  budget_refusal: "budget refusal",
  provider_error: "provider error",
};

type Props = {
  apiBase: string;
  getHeaders: (extra?: Record<string, string>) => Record<string, string>;
  onClose: () => void;
  onChanged?: () => void;
  // Whether this deployment uses JWT accounts (vs. a static API token) —
  // public config, from /v1/status — so a 401 here can say "sign in again"
  // instead of "enter a token" when that's not actually the right advice.
  jwtEnabled: boolean;
};

export function Settings({ apiBase, getHeaders, onClose, onChanged, jwtEnabled }: Props) {
  const [data, setData] = useState<SettingsView | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [cacheStats, setCacheStats] = useState<{ enabled: boolean; entries: number } | null>(null);
  const [memoryStats, setMemoryStats] = useState<RecallCacheStats | null>(null);
  const [semanticCacheStats, setSemanticCacheStats] = useState<RecallCacheStats | null>(
    null,
  );
  const [correctionSummary, setCorrectionSummary] = useState<CorrectionSummary | null>(
    null,
  );
  const [fallbackSummary, setFallbackSummary] = useState<FallbackSummary | null>(null);
  const [retryCost, setRetryCost] = useState<RetryCostSummary | null>(null);
  const [modelCatalog, setModelCatalog] = useState<ModelCatalogStatus | null>(null);
  const [catalogSyncing, setCatalogSyncing] = useState(false);
  const [configBusy, setConfigBusy] = useState(false);
  const [configStatus, setConfigStatus] = useState("");
  const configFileInputRef = useRef<HTMLInputElement | null>(null);
  const [query, setQuery] = useState("");

  // Reset every input to the persisted overrides. Used on (re)load and reset —
  // NOT after a single-row save, which must preserve unsaved edits elsewhere.
  const syncAllDrafts = useCallback((view: SettingsView) => {
    const next: Record<string, string> = {};
    for (const item of [
      ...(view.tiers ?? []),
      ...(view.categories ?? []),
      ...(view.prompts ?? []),
      ...(view.free_lane ?? []),
      ...(view.retention ?? []),
    ]) {
      next[item.key] = item.override ?? "";
    }
    setDrafts(next);
  }, []);

  // Load (and reload, via reloadNonce) the settings view.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/settings`, { headers: getHeaders() });
        if (!res.ok) {
          throw new Error(
            res.status === 401
              ? authFailureMessage(jwtEnabled)
              : `Failed to load settings (${res.status})`,
          );
        }
        const view = (await res.json()) as SettingsView;
        if (!cancelled) {
          setData(view);
          syncAllDrafts(view);
          setError("");
          setLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load settings");
          setLoading(false);
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadNonce]);

  // Response-cache stats (best-effort; the cache row is hidden if unavailable).
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/cache`, { headers: getHeaders() });
        if (res.ok && !cancelled) {
          const s = (await res.json()) as { enabled: boolean; entries: number };
          setCacheStats({ enabled: s.enabled, entries: s.entries });
        }
      } catch {
        // Leave the cache row hidden if the endpoint is unreachable.
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadNonce]);

  // Cross-conversation memory stats (best-effort; the row is hidden if
  // unavailable) — same pattern as the response-cache load above.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/memory`, { headers: getHeaders() });
        if (res.ok && !cancelled) {
          setMemoryStats((await res.json()) as RecallCacheStats);
        }
      } catch {
        // Leave the memory row hidden if the endpoint is unreachable.
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadNonce]);

  // Semantic (paraphrase) cache stats (best-effort; same pattern again).
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/semantic-cache`, {
          headers: getHeaders(),
        });
        if (res.ok && !cancelled) {
          setSemanticCacheStats((await res.json()) as RecallCacheStats);
        }
      } catch {
        // Leave the semantic-cache row hidden if the endpoint is unreachable.
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadNonce]);

  // Implicit-correction, paid-fallback-cause and re-run-cost on-demand stats
  // (best-effort; the section is hidden if unavailable) — the same data the
  // weekly System report tallies, one click away instead of waiting for the
  // next report.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [correctionRes, fallbackRes, retryRes] = await Promise.all([
          fetch(`${apiBase}/v1/correction/summary`, { headers: getHeaders() }),
          fetch(`${apiBase}/v1/fallback/summary`, { headers: getHeaders() }),
          fetch(`${apiBase}/v1/retry-cost/summary`, { headers: getHeaders() }),
        ]);
        if (correctionRes.ok && !cancelled) {
          setCorrectionSummary((await correctionRes.json()) as CorrectionSummary);
        }
        if (fallbackRes.ok && !cancelled) {
          setFallbackSummary((await fallbackRes.json()) as FallbackSummary);
        }
        if (retryRes.ok && !cancelled) {
          setRetryCost((await retryRes.json()) as RetryCostSummary);
        }
      } catch {
        // Leave the section hidden if either endpoint is unreachable.
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadNonce]);

  // Model-catalog status (best-effort; the row is hidden if unavailable).
  // This GET may itself trigger one backend sync if the catalog is enabled
  // and stale — opening Settings is what "on a schedule" means here, since
  // this app has no background scheduler (see app/model_catalog.py).
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/model-catalog`, { headers: getHeaders() });
        if (res.ok && !cancelled) {
          setModelCatalog((await res.json()) as ModelCatalogStatus);
        }
      } catch {
        // Leave the catalog row hidden if the endpoint is unreachable.
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadNonce]);

  async function syncModelCatalogNow() {
    setCatalogSyncing(true);
    try {
      const res = await fetch(`${apiBase}/v1/model-catalog/sync`, {
        method: "POST",
        headers: getHeaders(),
      });
      if (res.ok) {
        setModelCatalog((await res.json()) as ModelCatalogStatus);
      }
    } catch {
      // Non-fatal — the status row just doesn't update.
    } finally {
      setCatalogSyncing(false);
    }
  }

  async function clearCache() {
    try {
      const res = await fetch(`${apiBase}/v1/cache`, {
        method: "DELETE",
        headers: getHeaders(),
      });
      if (res.ok) {
        const s = (await res.json()) as { enabled: boolean; entries: number };
        setCacheStats({ enabled: s.enabled, entries: s.entries });
      }
    } catch {
      // Non-fatal.
    }
  }

  async function clearMemory() {
    try {
      const res = await fetch(`${apiBase}/v1/memory`, {
        method: "DELETE",
        headers: getHeaders(),
      });
      if (res.ok) {
        setMemoryStats((await res.json()) as RecallCacheStats);
      }
    } catch {
      // Non-fatal.
    }
  }

  async function clearSemanticCache() {
    try {
      const res = await fetch(`${apiBase}/v1/semantic-cache`, {
        method: "DELETE",
        headers: getHeaders(),
      });
      if (res.ok) {
        setSemanticCacheStats((await res.json()) as RecallCacheStats);
      }
    } catch {
      // Non-fatal.
    }
  }

  // Escape closes the modal no matter where focus currently sits (it opens on
  // the header button, which is outside this overlay's DOM subtree).
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const dialogRef = useRef<HTMLDivElement | null>(null);
  useModalFocus(dialogRef);

  function retry() {
    setError("");
    setLoading(true);
    setReloadNonce((nonce) => nonce + 1);
  }

  async function mutate(method: "PUT" | "DELETE", key: string, value?: string) {
    setBusyKey(key);
    try {
      const res = await fetch(`${apiBase}/v1/settings/${key}`, {
        method,
        headers: getHeaders({ "Content-Type": "application/json" }),
        body: method === "PUT" ? JSON.stringify({ value: value ?? "" }) : undefined,
      });
      if (!res.ok) {
        if (res.status === 401) throw new Error(authFailureMessage(jwtEnabled));
        const body = (await res.json().catch(() => ({}))) as { detail?: string };
        throw new Error(body.detail ?? `Request failed (${res.status})`);
      }
      const view = (await res.json()) as SettingsView;
      setData(view);
      // Re-sync only the row we changed; leave other rows' unsaved edits intact.
      const changed = [
        ...(view.tiers ?? []),
        ...(view.categories ?? []),
        ...(view.prompts ?? []),
        ...(view.free_lane ?? []),
        ...(view.retention ?? []),
      ].find((i) => i.key === key);
      setDrafts((prev) => ({ ...prev, [key]: changed?.override ?? "" }));
      setError("");
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setBusyKey(null);
    }
  }

  async function resetAll() {
    setBusyKey("__reset__");
    try {
      const res = await fetch(`${apiBase}/v1/settings/reset`, {
        method: "POST",
        headers: getHeaders(),
      });
      if (!res.ok) {
        throw new Error(
          res.status === 401 ? authFailureMessage(jwtEnabled) : `Reset failed (${res.status})`,
        );
      }
      const view = (await res.json()) as SettingsView;
      setData(view);
      syncAllDrafts(view);
      setError("");
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reset failed");
    } finally {
      setBusyKey(null);
    }
  }

  // Backup/restore the runtime model-map overrides — mirrors the
  // conversation export/import pattern (client-side JSON file, best-effort
  // on import) rather than adding a dedicated backend endpoint.
  function exportConfig() {
    if (!data) {
      return;
    }
    const overrides: Record<string, string> = {};
    for (const item of [
      ...(data.tiers ?? []),
      ...(data.categories ?? []),
      ...(data.features ?? []),
      ...(data.prompts ?? []),
    ]) {
      if (item.override) {
        overrides[item.key] = item.override;
      }
    }
    const content = JSON.stringify({ overrides }, null, 2);
    const blob = new Blob([content], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "ai-workbench-settings.json";
    link.click();
    URL.revokeObjectURL(url);
    setConfigStatus(
      Object.keys(overrides).length > 0
        ? `Exported ${Object.keys(overrides).length} override${Object.keys(overrides).length === 1 ? "" : "s"}.`
        : "No overrides are set — exported an empty file.",
    );
  }

  async function importConfig(files: FileList | null) {
    const file = files?.[0];
    if (!file) {
      return;
    }
    setConfigBusy(true);
    setConfigStatus("");
    try {
      const text = await file.text();
      const parsed = JSON.parse(text) as { overrides?: Record<string, string> };
      const entries = Object.entries(parsed.overrides ?? {});
      if (entries.length === 0) {
        throw new Error("That file doesn't contain any settings overrides.");
      }

      let successCount = 0;
      for (const [key, value] of entries) {
        try {
          const res = await fetch(`${apiBase}/v1/settings/${key}`, {
            method: "PUT",
            headers: getHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({ value }),
          });
          if (res.ok) successCount += 1;
        } catch {
          // Counted as a failure below via the successCount shortfall.
        }
      }

      setReloadNonce((nonce) => nonce + 1);
      onChanged?.();
      const failureCount = entries.length - successCount;
      if (successCount === 0) {
        setError(`Failed to import all ${entries.length} settings.`);
      } else {
        setError("");
        setConfigStatus(
          failureCount > 0
            ? `Imported ${successCount} of ${entries.length} overrides (${failureCount} failed).`
            : `Imported ${successCount} override${successCount === 1 ? "" : "s"}.`,
        );
      }
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to import — is it valid JSON?",
      );
    } finally {
      setConfigBusy(false);
    }
  }

  const editable = data?.editable ?? false;

  const trimmedQuery = query.trim().toLowerCase();
  function matches(candidate: { label: string; key: string }): boolean {
    return (
      !trimmedQuery ||
      candidate.label.toLowerCase().includes(trimmedQuery) ||
      candidate.key.toLowerCase().includes(trimmedQuery)
    );
  }
  // Each list is guarded independently (`data?.tiers ?? []`, not just
  // `data?.` at the front) so a response that's present but missing one of
  // these keys — a malformed/partial backend reply, or an older frontend
  // build talking to a newer/older backend schema — degrades that one
  // section to empty instead of crashing the whole panel: `?.` only
  // short-circuits when the object itself is null/undefined, not when a
  // property on it is.
  const filteredTiers = (data?.tiers ?? []).filter(matches);
  const filteredCategories = (data?.categories ?? []).filter(matches);
  const filteredFeatures = (data?.features ?? []).filter(matches);
  const filteredPrompts = (data?.prompts ?? []).filter(matches);
  const filteredFreeLane = (data?.free_lane ?? []).filter(matches);
  const filteredRetention = (data?.retention ?? []).filter(matches);
  const hasAnyMatch =
    filteredTiers.length > 0 ||
    filteredCategories.length > 0 ||
    filteredFeatures.length > 0 ||
    filteredPrompts.length > 0 ||
    filteredFreeLane.length > 0 ||
    filteredRetention.length > 0;

  function row(item: SettingItem) {
    const draft = drafts[item.key] ?? "";
    const placeholder = item.inherits
      ? `inherits ${item.inherits}`
      : item.default || item.effective_model || "model name";
    return (
      <div className="setting-row" key={item.key}>
        <div className="setting-label">
          <strong>{item.label}</strong>
          <code>{item.key}</code>
        </div>
        <input
          aria-label={`${item.label} model`}
          value={draft}
          placeholder={placeholder}
          disabled={!editable || busyKey === item.key}
          onChange={(event) =>
            setDrafts((prev) => ({ ...prev, [item.key]: event.target.value }))
          }
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void mutate("PUT", item.key, draft);
            }
          }}
        />
        <div className="setting-meta">
          <span className={`source-badge source-${item.source}`}>{item.source}</span>
          <span className="setting-effective">→ {item.effective_model || "—"}</span>
          {item.key_present === false ? (
            <span className="key-warning">⚠ {item.key_env} not set</span>
          ) : null}
        </div>
        <div className="setting-actions">
          <button
            className="secondary-button"
            onClick={() => mutate("PUT", item.key, draft)}
            disabled={!editable || busyKey === item.key}
            aria-label={`Save ${item.label}`}
          >
            Save
          </button>
          <button
            className="link-button"
            onClick={() => mutate("DELETE", item.key)}
            disabled={!editable || busyKey === item.key || !item.override}
            aria-label={`Revert ${item.label}`}
          >
            Revert
          </button>
        </div>
      </div>
    );
  }

  function featureRow(item: FeatureFlagItem) {
    return (
      <div className="setting-row feature-flag-row" key={item.key}>
        <div className="setting-label">
          <label>
            <input
              type="checkbox"
              checked={item.effective_enabled}
              disabled={!editable || busyKey === item.key}
              onChange={(event) =>
                void mutate("PUT", item.key, event.target.checked ? "true" : "false")
              }
              aria-label={item.label}
            />
            <strong>{item.label}</strong>
          </label>
          <code>{item.key}</code>
          <span className="feature-flag-description">{item.description}</span>
        </div>
        <div className="setting-meta">
          <span className={`source-badge source-${item.source}`}>{item.source}</span>
        </div>
        <div className="setting-actions">
          <button
            className="link-button"
            onClick={() => mutate("DELETE", item.key)}
            disabled={!editable || busyKey === item.key || !item.override}
            aria-label={`Revert ${item.label}`}
          >
            Revert
          </button>
        </div>
      </div>
    );
  }

  function promptRow(item: PromptItem) {
    const draft = drafts[item.key] ?? "";
    return (
      <div className="setting-row prompt-row" key={item.key}>
        <div className="setting-label">
          <strong>{item.label}</strong>
          <code>{item.key}</code>
        </div>
        <textarea
          aria-label={`${item.label} role prompt`}
          value={draft}
          placeholder="No role prompt configured for this category."
          maxLength={4000}
          rows={3}
          disabled={!editable || busyKey === item.key}
          onChange={(event) =>
            setDrafts((prev) => ({ ...prev, [item.key]: event.target.value }))
          }
        />
        <div className="setting-meta">
          <span className={`source-badge source-${item.source}`}>{item.source}</span>
          <span className="setting-effective">
            {item.effective_prompt ? `${draft.length}/4000 chars` : "no role prompt"}
          </span>
        </div>
        <div className="setting-actions">
          <button
            className="secondary-button"
            onClick={() => mutate("PUT", item.key, draft)}
            disabled={!editable || busyKey === item.key}
            aria-label={`Save ${item.label} role prompt`}
          >
            Save
          </button>
          <button
            className="link-button"
            onClick={() => mutate("DELETE", item.key)}
            disabled={!editable || busyKey === item.key || !item.override}
            aria-label={`Revert ${item.label} role prompt`}
          >
            Revert
          </button>
        </div>
      </div>
    );
  }

  function freeLaneRow(item: FreeLaneItem) {
    const draft = drafts[item.key] ?? "";
    const isModelList = item.key === "FREE_TIER_MODELS";
    return (
      <div className="setting-row" key={item.key}>
        <div className="setting-label">
          <strong>{item.label}</strong>
          <code>{item.key}</code>
        </div>
        <input
          aria-label={item.label}
          value={draft}
          placeholder={
            isModelList
              ? "e.g. openrouter/meta-llama/llama-3.3-70b:free, groq/llama-3.3-70b-versatile"
              : item.default || "100"
          }
          disabled={!editable || busyKey === item.key}
          onChange={(event) =>
            setDrafts((prev) => ({ ...prev, [item.key]: event.target.value }))
          }
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void mutate("PUT", item.key, draft);
            }
          }}
        />
        <div className="setting-meta">
          <span className={`source-badge source-${item.source}`}>{item.source}</span>
          <span className="setting-effective">→ {item.effective_value || "—"}</span>
        </div>
        <div className="setting-actions">
          <button
            className="secondary-button"
            onClick={() => mutate("PUT", item.key, draft)}
            disabled={!editable || busyKey === item.key}
            aria-label={`Save ${item.label}`}
          >
            Save
          </button>
          <button
            className="link-button"
            onClick={() => mutate("DELETE", item.key)}
            disabled={!editable || busyKey === item.key || !item.override}
            aria-label={`Revert ${item.label}`}
          >
            Revert
          </button>
        </div>
      </div>
    );
  }

  function retentionRow(item: RetentionItem) {
    const draft = drafts[item.key] ?? "";
    const placeholder =
      item.key === "SHARE_EXPIRY_DAYS"
        ? "blank = links never expire"
        : item.default || "365";
    return (
      <div className="setting-row" key={item.key}>
        <div className="setting-label">
          <strong>{item.label}</strong>
          <code>{item.key}</code>
        </div>
        <input
          aria-label={item.label}
          value={draft}
          placeholder={placeholder}
          disabled={!editable || busyKey === item.key}
          onChange={(event) =>
            setDrafts((prev) => ({ ...prev, [item.key]: event.target.value }))
          }
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void mutate("PUT", item.key, draft);
            }
          }}
        />
        <div className="setting-meta">
          <span className={`source-badge source-${item.source}`}>{item.source}</span>
          <span className="setting-effective">→ {item.effective_value || "—"}</span>
        </div>
        <div className="setting-actions">
          <button
            className="secondary-button"
            onClick={() => mutate("PUT", item.key, draft)}
            disabled={!editable || busyKey === item.key}
            aria-label={`Save ${item.label}`}
          >
            Save
          </button>
          <button
            className="link-button"
            onClick={() => mutate("DELETE", item.key)}
            disabled={!editable || busyKey === item.key || !item.override}
            aria-label={`Revert ${item.label}`}
          >
            Revert
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      className="settings-overlay"
      role="presentation"
      onClick={onClose}
      onKeyDown={(event) => {
        if (event.key === "Escape") onClose();
      }}
    >
      <div
        ref={dialogRef}
        className="settings-modal"
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        aria-label="Model settings"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="settings-header">
          <h2>Model settings</h2>
          <button className="link-button" onClick={onClose} aria-label="Close settings">
            ✕
          </button>
        </header>

        <p className="settings-intro">
          Route each task to the model best suited to it. A saved value overrides
          the matching environment variable; clearing it reverts to the env / default.
        </p>

        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}
        {data && !data.editable && data.admin_gated && !data.is_admin ? (
          <p className="settings-readonly">
            Settings are managed by an admin account on this deployment. Values are
            read-only for your account.
          </p>
        ) : data && !data.editable ? (
          <p className="settings-readonly">
            Editing is disabled on this server (ALLOW_SETTINGS_WRITE=false). Values
            are read-only.
          </p>
        ) : null}
        {configStatus ? <p className="settings-readonly">{configStatus}</p> : null}

        {loading ? (
          <p className="settings-loading">Loading…</p>
        ) : data ? (
          <>
            <input
              type="search"
              className="settings-search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search settings…"
              aria-label="Search settings"
              disabled={
                (data.tiers?.length ?? 0) +
                  (data.categories?.length ?? 0) +
                  (data.features?.length ?? 0) +
                  (data.prompts?.length ?? 0) ===
                0
              }
            />
            {!hasAnyMatch && trimmedQuery ? (
              <p className="settings-readonly">No settings match "{query.trim()}".</p>
            ) : null}
            {filteredTiers.length > 0 ? (
              <section className="settings-section">
                <h3>Tiers</h3>
                {filteredTiers.map(row)}
              </section>
            ) : null}
            {filteredCategories.length > 0 ? (
              <section className="settings-section">
                <h3>Task categories</h3>
                {filteredCategories.map(row)}
              </section>
            ) : null}
            {filteredPrompts.length > 0 ? (
              <section className="settings-section">
                <h3>Role prompts</h3>
                <p className="settings-section-hint">
                  An optional persona/system prompt per task category, applied
                  automatically in auto mode once routing resolves that
                  category — e.g. a coder persona for Coding requests.
                </p>
                {filteredPrompts.map(promptRow)}
              </section>
            ) : null}
            {filteredFeatures.length > 0 ? (
              <section className="settings-section">
                <h3>Optional features</h3>
                {filteredFeatures.map(featureRow)}
              </section>
            ) : null}
            {filteredFreeLane.length > 0 ? (
              <section className="settings-section">
                <h3>Free-first routing lane</h3>
                <p className="settings-section-hint">
                  Ordered free-tier models tried before the routed budget/fast
                  model in auto mode (toggle above). Each model gets its own
                  daily request quota, tracked locally and reset at UTC
                  midnight.
                </p>
                {filteredFreeLane.map(freeLaneRow)}
              </section>
            ) : null}
            {filteredRetention.length > 0 ? (
              <section className="settings-section">
                <h3>Data retention</h3>
                <p className="settings-section-hint">
                  How long detailed spend/feedback/correction/fallback history
                  is kept before being rolled up into monthly totals and
                  pruned, and how long a share link stays live by default.
                </p>
                {filteredRetention.map(retentionRow)}
              </section>
            ) : null}
            {cacheStats ? (
              <div className="settings-cache">
                <span>
                  Response cache: {cacheStats.entries} stored
                  {cacheStats.enabled ? "" : " (caching off)"}
                </span>
                <button
                  className="link-button"
                  onClick={clearCache}
                  disabled={cacheStats.entries === 0}
                >
                  Clear cache
                </button>
              </div>
            ) : null}
            {memoryStats ? (
              <div className="settings-cache">
                <span>
                  Cross-conversation memory: {memoryStats.entries} stored
                  {memoryStats.enabled ? "" : " (memory off)"}
                </span>
                <button
                  className="link-button"
                  onClick={() => void clearMemory()}
                  disabled={memoryStats.entries === 0}
                >
                  Clear memory
                </button>
              </div>
            ) : null}
            {semanticCacheStats ? (
              <div className="settings-cache">
                <span>
                  Semantic cache: {semanticCacheStats.entries} stored
                  {semanticCacheStats.enabled ? "" : " (semantic cache off)"}
                </span>
                <button
                  className="link-button"
                  onClick={() => void clearSemanticCache()}
                  disabled={semanticCacheStats.entries === 0}
                >
                  Clear semantic cache
                </button>
              </div>
            ) : null}
            {correctionSummary || fallbackSummary || retryCost ? (
              <div className="settings-cache settings-model-catalog">
                {retryCost && retryCost.overall.turns > 0 ? (
                  <span>
                    Re-run cost: true cost {asUsd(retryCost.overall.total_cost_usd)} vs{" "}
                    {asUsd(retryCost.overall.first_attempt_cost_usd)} first-attempt
                    {retryCost.overall.cost_multiplier
                      ? ` (${retryCost.overall.cost_multiplier.toFixed(2)}×)`
                      : ""}
                    {" — retry rate "}
                    {retryRateText(retryCost.overall)}.
                  </span>
                ) : null}
                {retryCost && retryCost.overall.retries > 0 ? (
                  <span>
                    Why re-run:{" "}
                    {Object.entries(retryCost.by_signal)
                      .filter(([, stat]) => stat.retries > 0)
                      .map(
                        ([signal, stat]) =>
                          `${RETRY_SIGNAL_LABELS[signal] ?? signal} (${stat.retries})`,
                      )
                      .join(", ")}
                    {" — kept apart because a regeneration with no rating may just be"}
                    {" taste, not a quality failure."}
                  </span>
                ) : null}
                {correctionSummary ? (
                  <span>
                    Implicit correction rate:{" "}
                    {(correctionSummary.overall.correction_rate * 100).toFixed(0)}%
                    {" "}({correctionSummary.overall.flagged}/
                    {correctionSummary.overall.answers}) — a noisy proxy, not a
                    verified error rate.
                  </span>
                ) : null}
                {fallbackSummary && fallbackSummary.reasons.length > 0 ? (
                  <span>
                    Paid fallback causes:{" "}
                    {fallbackSummary.reasons
                      .map(
                        (r) =>
                          `${FALLBACK_REASON_LABELS[r.reason] ?? r.reason} (${r.count})`,
                      )
                      .join(", ")}
                  </span>
                ) : null}
              </div>
            ) : null}
            {data.is_admin ? <Users apiBase={apiBase} getHeaders={getHeaders} /> : null}
            {modelCatalog ? (
              <div className="settings-cache settings-model-catalog">
                <div className="settings-model-catalog-row">
                  <span>
                    Model catalog:{" "}
                    {modelCatalog.enabled
                      ? modelCatalog.synced_at
                        ? `${modelCatalog.model_count.toLocaleString()} models synced ${modelCatalog.synced_at}`
                        : "not synced yet"
                      : "sync off"}
                  </span>
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => void syncModelCatalogNow()}
                    disabled={!modelCatalog.enabled || catalogSyncing}
                  >
                    {catalogSyncing ? "Syncing…" : "🔄 Sync now"}
                  </button>
                </div>
                {modelCatalog.error ? (
                  <p className="settings-error" role="alert">
                    {modelCatalog.error}
                  </p>
                ) : null}
                {(modelCatalog.new_models ?? []).length > 0 ? (
                  <p className="settings-readonly">
                    🆕 {(modelCatalog.new_models ?? []).length} new model
                    {(modelCatalog.new_models ?? []).length === 1 ? "" : "s"} since the last sync:{" "}
                    {(modelCatalog.new_models ?? []).join(", ")}
                  </p>
                ) : null}
              </div>
            ) : null}
            <footer className="settings-footer">
              <input
                ref={configFileInputRef}
                type="file"
                accept="application/json"
                className="visually-hidden"
                aria-label="Import settings config from a JSON file"
                onChange={(event) => {
                  void importConfig(event.target.files);
                  event.target.value = "";
                }}
              />
              <button
                type="button"
                className="secondary-button"
                onClick={() => configFileInputRef.current?.click()}
                disabled={!editable || configBusy}
              >
                {configBusy ? "Importing…" : "⬆️ Import config"}
              </button>
              <button type="button" className="secondary-button" onClick={exportConfig}>
                ⬇️ Export config
              </button>
              <button
                className="danger-button"
                onClick={resetAll}
                disabled={!editable || busyKey !== null}
              >
                Reset all to defaults
              </button>
              <button className="secondary-button" onClick={onClose}>
                Done
              </button>
            </footer>
          </>
        ) : (
          <div className="settings-footer">
            <button className="secondary-button" onClick={retry}>
              Retry
            </button>
            <button className="secondary-button" onClick={onClose}>
              Close
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
