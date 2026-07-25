import { useEffect, useState } from "react";
import { formatCost } from "./format";

type UsageByModel = {
  model: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
};

type UsageByDay = {
  date: string;
  cost_usd: number;
};

type UsageSummary = {
  today_usd: number;
  days: number;
  by_model: UsageByModel[];
  by_day: UsageByDay[];
};

type Props = {
  apiBase: string;
  getHeaders: (extra?: Record<string, string>) => Record<string, string>;
  onClose: () => void;
};

const DAY_OPTIONS = [7, 14, 30, 90];

export function Usage({ apiBase, getHeaders, onClose }: Props) {
  const [days, setDays] = useState(14);
  const [data, setData] = useState<UsageSummary | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/usage?days=${days}`, { headers: getHeaders() });
        if (!res.ok) throw new Error(`Failed to load usage (${res.status})`);
        const view = (await res.json()) as UsageSummary;
        if (!cancelled) {
          setData(view);
          setError("");
          setLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load usage");
          setLoading(false);
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const maxDayCost = data ? Math.max(...data.by_day.map((day) => day.cost_usd), 0.000001) : 1;

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
        aria-label="Usage"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="settings-header">
          <h2>Usage</h2>
          <button className="link-button" onClick={onClose} aria-label="Close usage">
            ✕
          </button>
        </header>

        <p className="settings-intro">
          Your own spend, tracked from the same daily-budget ledger — never other
          users' figures.
        </p>

        {error ? <p className="settings-error">{error}</p> : null}

        {loading && !data ? (
          <p className="settings-loading">Loading…</p>
        ) : data ? (
          <>
            <div className="usage-today">
              <span className="usage-today-figure">{formatCost(data.today_usd) || "$0.00"}</span>
              <span className="usage-today-label">spent today</span>
            </div>

            <section className="settings-section">
              <div className="usage-section-header">
                <h3>Last {data.days} days</h3>
                <select
                  value={days}
                  onChange={(event) => {
                    setLoading(true);
                    setDays(Number(event.target.value));
                  }}
                  aria-label="Usage window"
                >
                  {DAY_OPTIONS.map((option) => (
                    <option key={option} value={option}>
                      {option} days
                    </option>
                  ))}
                </select>
              </div>
              <div className="usage-bars" role="img" aria-label={`Daily spend over the last ${data.days} days`}>
                {data.by_day.map((day) => (
                  <div
                    key={day.date}
                    className="usage-bar"
                    title={`${day.date}: ${formatCost(day.cost_usd) || "$0.00"}`}
                    style={{ height: `${Math.max((day.cost_usd / maxDayCost) * 100, 2)}%` }}
                  />
                ))}
              </div>
            </section>

            <section className="settings-section">
              <h3>By model</h3>
              {data.by_model.length === 0 ? (
                <p className="settings-readonly">No spend recorded in this window.</p>
              ) : (
                <table className="usage-model-table">
                  <thead>
                    <tr>
                      <th>Model</th>
                      <th>Calls</th>
                      <th>Tokens</th>
                      <th>Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_model.map((row) => (
                      <tr key={row.model}>
                        <td>{row.model}</td>
                        <td>{row.calls}</td>
                        <td>{(row.input_tokens + row.output_tokens).toLocaleString()}</td>
                        <td>{formatCost(row.cost_usd) || "$0.00"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>

            <footer className="settings-footer">
              <button className="secondary-button" onClick={onClose}>
                Done
              </button>
            </footer>
          </>
        ) : null}
      </div>
    </div>
  );
}
