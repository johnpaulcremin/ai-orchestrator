import { useCallback, useEffect, useRef, useState } from "react";
import { useModalFocus } from "./useModalFocus";

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

export type SettingsView = {
  editable: boolean;
  tiers: SettingItem[];
  categories: SettingItem[];
  features: FeatureFlagItem[];
};

type ModelCatalogStatus = {
  enabled: boolean;
  synced_at: string | null;
  model_count: number;
  new_models: string[];
  stale: boolean;
  error?: string | null;
};

type Props = {
  apiBase: string;
  getHeaders: (extra?: Record<string, string>) => Record<string, string>;
  onClose: () => void;
  onChanged?: () => void;
};

export function Settings({ apiBase, getHeaders, onClose, onChanged }: Props) {
  const [data, setData] = useState<SettingsView | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [cacheStats, setCacheStats] = useState<{ enabled: boolean; entries: number } | null>(null);
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
    for (const item of [...view.tiers, ...view.categories]) {
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
        if (!res.ok) throw new Error(`Failed to load settings (${res.status})`);
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
        const body = (await res.json().catch(() => ({}))) as { detail?: string };
        throw new Error(body.detail ?? `Request failed (${res.status})`);
      }
      const view = (await res.json()) as SettingsView;
      setData(view);
      // Re-sync only the row we changed; leave other rows' unsaved edits intact.
      const changed = [...view.tiers, ...view.categories].find((i) => i.key === key);
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
      if (!res.ok) throw new Error(`Reset failed (${res.status})`);
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
    for (const item of [...data.tiers, ...data.categories, ...data.features]) {
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
  const filteredTiers = data?.tiers.filter(matches) ?? [];
  const filteredCategories = data?.categories.filter(matches) ?? [];
  const filteredFeatures = data?.features.filter(matches) ?? [];
  const hasAnyMatch =
    filteredTiers.length > 0 || filteredCategories.length > 0 || filteredFeatures.length > 0;

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
        {data && !data.editable ? (
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
              disabled={data.tiers.length + data.categories.length + data.features.length === 0}
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
            {filteredFeatures.length > 0 ? (
              <section className="settings-section">
                <h3>Optional features</h3>
                {filteredFeatures.map(featureRow)}
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
                {modelCatalog.new_models.length > 0 ? (
                  <p className="settings-readonly">
                    🆕 {modelCatalog.new_models.length} new model
                    {modelCatalog.new_models.length === 1 ? "" : "s"} since the last sync:{" "}
                    {modelCatalog.new_models.join(", ")}
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
