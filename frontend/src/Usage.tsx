import { useEffect, useRef, useState } from "react";
import { formatCost } from "./format";
import { useModalFocus } from "./useModalFocus";

type UsageByModel = {
  model: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  // null when this model has no known cost at all (unpriced), distinct from
  // a genuinely free model, which reports 0.
  cost_usd: number | null;
};

type UsageByDay = {
  date: string;
  cost_usd: number;
  // input_tokens + output_tokens billed that day, across every model.
  tokens: number;
};

type UsageSummary = {
  today_usd: number;
  days: number;
  by_model: UsageByModel[];
  by_day: UsageByDay[];
  // The configured cap(s), never the live global spend — that stays private
  // to the operator. null when that particular cap isn't set.
  daily_budget_usd: number | null;
  daily_budget_per_owner_usd: number | null;
  // How much of the caller's OWN per-owner cap is left today, floored at 0;
  // null when no per-owner cap is configured (distinct from "$0 left").
  owner_remaining_usd: number | null;
  // The KPI this app actually exists to move: total tokens processed per
  // dollar spent over the window. null when the window spent nothing —
  // either no usage at all, or every call in it was free; window_tokens
  // tells those two apart.
  tokens_per_dollar: number | null;
  window_tokens: number;
};

type FreeTierModelStatus = {
  model: string;
  quota: number;
  used: number;
  remaining: number;
};

type FreeTierStatus = {
  enabled: boolean;
  models: FreeTierModelStatus[];
};

type Props = {
  apiBase: string;
  getHeaders: (extra?: Record<string, string>) => Record<string, string>;
  onClose: () => void;
};

const DAY_OPTIONS = [7, 14, 30, 90];

// Quotes a CSV field only when it contains a character that would otherwise
// break column alignment (comma, quote, or newline) — model names never do,
// but this stays correct if that ever changes.
function csvField(value: string | number): string {
  const text = String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function downloadCsv(content: string, filename: string) {
  const blob = new Blob([content], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

export function Usage({ apiBase, getHeaders, onClose }: Props) {
  const [days, setDays] = useState(14);
  const [data, setData] = useState<UsageSummary | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [freeTier, setFreeTier] = useState<FreeTierStatus | null>(null);

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

  // Free-lane quota status (best-effort; the section is hidden if unavailable
  // or nothing is configured).
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/free-tier`, { headers: getHeaders() });
        if (res.ok && !cancelled) {
          setFreeTier((await res.json()) as FreeTierStatus);
        }
      } catch {
        // Leave the section hidden if the endpoint is unreachable.
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const dialogRef = useRef<HTMLDivElement | null>(null);
  useModalFocus(dialogRef);

  const maxDayCost = data ? Math.max(...data.by_day.map((day) => day.cost_usd), 0.000001) : 1;

  function exportCsv() {
    if (!data) {
      return;
    }
    const lines: string[] = [];
    lines.push("Daily spend");
    lines.push("date,cost_usd,tokens");
    for (const day of data.by_day) {
      lines.push(`${csvField(day.date)},${csvField(day.cost_usd)},${csvField(day.tokens)}`);
    }
    lines.push("");
    lines.push("Efficiency");
    lines.push("window_tokens,tokens_per_dollar");
    lines.push(
      `${csvField(data.window_tokens)},${csvField(data.tokens_per_dollar ?? "")}`,
    );
    lines.push("");
    lines.push("By model");
    lines.push("model,calls,input_tokens,output_tokens,cost_usd");
    for (const row of data.by_model) {
      lines.push(
        `${csvField(row.model)},${csvField(row.calls)},${csvField(row.input_tokens)},${csvField(row.output_tokens)},${csvField(row.cost_usd ?? "unknown")}`,
      );
    }
    downloadCsv(lines.join("\n"), `ai-workbench-usage-${data.days}d.csv`);
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

        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}

        {loading && !data ? (
          <p className="settings-loading">Loading…</p>
        ) : data ? (
          <>
            <div className="usage-today">
              <span className="usage-today-figure">{formatCost(data.today_usd) || "$0.00"}</span>
              <span className="usage-today-label">spent today</span>
            </div>

            <div
              className="usage-kpi"
              title="Total tokens processed divided by total spend over this window — the actual thing routing, caching, and downscaling are meant to improve. A rising number means the app is doing more with the same money; falling spend alone doesn't tell you that."
            >
              {data.tokens_per_dollar !== null ? (
                <>
                  <span className="usage-kpi-figure">
                    {Math.round(data.tokens_per_dollar).toLocaleString()}
                  </span>
                  <span className="usage-kpi-label">tokens per $1 · last {data.days} days</span>
                </>
              ) : data.window_tokens > 0 ? (
                <>
                  <span className="usage-kpi-figure">All free</span>
                  <span className="usage-kpi-label">
                    {data.window_tokens.toLocaleString()} tokens, $0 spent · last {data.days} days
                  </span>
                </>
              ) : (
                <span className="usage-kpi-label">No usage yet in the last {data.days} days.</span>
              )}
            </div>

            {data.owner_remaining_usd !== null && data.daily_budget_per_owner_usd !== null ? (
              <p className="usage-budget-remaining">
                {formatCost(data.owner_remaining_usd)} left of your{" "}
                {formatCost(data.daily_budget_per_owner_usd)} daily cap
              </p>
            ) : data.daily_budget_usd !== null ? (
              <p className="usage-budget-remaining">
                Global daily cap: {formatCost(data.daily_budget_usd)}
              </p>
            ) : null}

            <section className="settings-section">
              <div className="usage-section-header">
                <h3>Last {data.days} days</h3>
                <div className="usage-section-header-actions">
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
                  <button type="button" className="secondary-button" onClick={exportCsv}>
                    ⬇️ Export CSV
                  </button>
                </div>
              </div>
              <div className="usage-bars" role="img" aria-label={`Daily spend over the last ${data.days} days`}>
                {data.by_day.map((day) => (
                  <div
                    key={day.date}
                    className="usage-bar"
                    title={`${day.date}: ${formatCost(day.cost_usd) || "$0.00"} · ${day.tokens.toLocaleString()} tokens`}
                    style={{ height: `${Math.max((day.cost_usd / maxDayCost) * 100, 2)}%` }}
                  />
                ))}
              </div>
            </section>

            {freeTier && freeTier.enabled && freeTier.models.length > 0 ? (
              <section className="settings-section">
                <h3>Free lane remaining today</h3>
                <table className="usage-model-table">
                  <thead>
                    <tr>
                      <th>Model</th>
                      <th>Used</th>
                      <th>Quota</th>
                      <th>Remaining</th>
                    </tr>
                  </thead>
                  <tbody>
                    {freeTier.models.map((row) => (
                      <tr key={row.model}>
                        <td>{row.model}</td>
                        <td>{row.used}</td>
                        <td>{row.quota}</td>
                        <td>{row.remaining}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            ) : null}

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
                        <td>
                          {row.cost_usd == null ? (
                            <span title="This model isn't in the price list, so its cost can't be estimated.">
                              Unknown
                            </span>
                          ) : (
                            formatCost(row.cost_usd) || "$0.00"
                          )}
                        </td>
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
