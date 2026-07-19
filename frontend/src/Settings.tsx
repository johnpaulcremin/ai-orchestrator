import { useCallback, useEffect, useState } from "react";

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

export type SettingsView = {
  editable: boolean;
  tiers: SettingItem[];
  categories: SettingItem[];
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

  const editable = data?.editable ?? false;

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
        className="settings-modal"
        role="dialog"
        aria-modal="true"
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

        {error ? <p className="settings-error">{error}</p> : null}
        {data && !data.editable ? (
          <p className="settings-readonly">
            Editing is disabled on this server (ALLOW_SETTINGS_WRITE=false). Values
            are read-only.
          </p>
        ) : null}

        {loading ? (
          <p className="settings-loading">Loading…</p>
        ) : data ? (
          <>
            <section className="settings-section">
              <h3>Tiers</h3>
              {data.tiers.map(row)}
            </section>
            <section className="settings-section">
              <h3>Task categories</h3>
              {data.categories.map(row)}
            </section>
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
            <footer className="settings-footer">
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
