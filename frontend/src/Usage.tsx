import { useEffect, useRef, useState } from "react";
import { authFailureMessage, formatCost, formatPercent } from "./format";
import { buildScorecard, isQualityWarning } from "./scorecard";
import type {
  CorrectionStat,
  FallbackModelCount,
  FeedbackStat,
  UsageByModel,
} from "./scorecard";
import { useModalFocus } from "./useModalFocus";

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
  // How much work the caches saved over the window. Both rates are null when
  // the window holds no requests at all — distinct from a measured 0%, which
  // means the cache is on and never hitting.
  cache: {
    total_requests: number;
    exact_hits: number;
    semantic_hits: number;
    exact_hit_rate: number | null;
    semantic_hit_rate: number | null;
    avoided_cost_usd: number;
  };
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

type FeedbackSummary = {
  by_model: Record<string, FeedbackStat>;
  by_category: Record<string, FeedbackStat>;
  by_lane: Record<string, FeedbackStat>;
};

type CorrectionSummary = {
  by_model: Record<string, CorrectionStat>;
};

type FallbackSummary = {
  // Live-window only, unlike `reasons`: the rollup that survives pruning
  // keeps reasons, not model names (see app/routers/usage.py).
  models: FallbackModelCount[];
};

type SelfReportStatus = {
  last_generated_at: string | null;
  narrate_enabled: boolean;
};

// The "is the free lane costing quality" comparison: the free lane's own
// down-rate against every OTHER lane's ratings combined (budget/fast/smart/
// forced) — the single number that answers whether routing free-tier
// traffic is trading quality for cost.
function paidLaneStat(byLane: Record<string, FeedbackStat>): FeedbackStat | null {
  const paidEntries = Object.entries(byLane).filter(([lane]) => lane !== "free");
  const rated = paidEntries.reduce((sum, [, stat]) => sum + stat.answers_rated, 0);
  if (rated === 0) {
    return null;
  }
  const down = paidEntries.reduce((sum, [, stat]) => sum + stat.down, 0);
  return { answers_rated: rated, up: rated - down, down, down_rate: down / rated };
}

type Props = {
  apiBase: string;
  getHeaders: (extra?: Record<string, string>) => Record<string, string>;
  onClose: () => void;
  jwtEnabled: boolean;
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

export function Usage({ apiBase, getHeaders, onClose, jwtEnabled }: Props) {
  const [days, setDays] = useState(14);
  const [data, setData] = useState<UsageSummary | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [freeTier, setFreeTier] = useState<FreeTierStatus | null>(null);
  const [feedback, setFeedback] = useState<FeedbackSummary | null>(null);
  const [correction, setCorrection] = useState<CorrectionSummary | null>(null);
  const [fallback, setFallback] = useState<FallbackSummary | null>(null);
  const [reportStatus, setReportStatus] = useState<SelfReportStatus | null>(null);
  const [reportGenerating, setReportGenerating] = useState(false);
  const [reportMessage, setReportMessage] = useState("");

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/usage?days=${days}`, { headers: getHeaders() });
        if (!res.ok) {
          throw new Error(
            res.status === 401
              ? authFailureMessage(jwtEnabled)
              : `Failed to load usage (${res.status})`,
          );
        }
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

  // Quality feedback summary (best-effort; the section is hidden if nothing
  // has been rated yet in this window). Reuses the same `days` window as
  // the spend series above.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/feedback/summary?days=${days}`, {
          headers: getHeaders(),
        });
        if (res.ok && !cancelled) {
          setFeedback((await res.json()) as FeedbackSummary);
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
  }, [days]);

  // The other two halves of the Scorecard: the implicit-correction rate (a
  // NOISY PROXY — see app/correction_tracking.py) and how often the router
  // had to fall back AWAY from each model. Both best-effort and both on the
  // same `days` window, so every column of a Scorecard row describes the
  // same period; a source that fails to load leaves its columns as "—"
  // rather than blanking the whole table.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const fetchJson = async <T,>(path: string): Promise<T | null> => {
        try {
          const res = await fetch(`${apiBase}${path}?days=${days}`, {
            headers: getHeaders(),
          });
          return res.ok ? ((await res.json()) as T) : null;
        } catch {
          return null;
        }
      };
      const [corrections, fallbacks] = await Promise.all([
        fetchJson<CorrectionSummary>("/v1/correction/summary"),
        fetchJson<FallbackSummary>("/v1/fallback/summary"),
      ]);
      if (!cancelled) {
        setCorrection(corrections);
        setFallback(fallbacks);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days]);

  // Weekly self-report status (best-effort; the section still renders with
  // "never generated" if this fails). See app/self_report.py.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/self-report/status`, {
          headers: getHeaders(),
        });
        if (res.ok && !cancelled) {
          setReportStatus((await res.json()) as SelfReportStatus);
        }
      } catch {
        // Leave the section showing its default state if unreachable.
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function generateReportNow() {
    setReportGenerating(true);
    setReportMessage("");
    try {
      const res = await fetch(`${apiBase}/v1/self-report/generate`, {
        method: "POST",
        headers: getHeaders(),
      });
      if (res.ok) {
        setReportMessage("Report generated — check your conversation list.");
        const statusRes = await fetch(`${apiBase}/v1/self-report/status`, {
          headers: getHeaders(),
        });
        if (statusRes.ok) {
          setReportStatus((await statusRes.json()) as SelfReportStatus);
        }
      } else {
        setReportMessage("Failed to generate the report.");
      }
    } catch {
      setReportMessage("Failed to generate the report.");
    } finally {
      setReportGenerating(false);
    }
  }

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
  const scorecard = buildScorecard({
    spend: data?.by_model,
    feedback: feedback?.by_model,
    corrections: correction?.by_model,
    fallbacks: fallback?.models,
  });

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
    lines.push("Cache");
    lines.push("total_requests,exact_hits,semantic_hits,exact_hit_rate,semantic_hit_rate,avoided_cost_usd");
    lines.push(
      `${csvField(data.cache.total_requests)},${csvField(data.cache.exact_hits)},${csvField(data.cache.semantic_hits)},${csvField(data.cache.exact_hit_rate ?? "")},${csvField(data.cache.semantic_hit_rate ?? "")},${csvField(data.cache.avoided_cost_usd)}`,
    );
    lines.push("");
    lines.push("By model");
    lines.push("model,calls,input_tokens,output_tokens,cost_usd");
    for (const row of data.by_model) {
      lines.push(
        `${csvField(row.model)},${csvField(row.calls)},${csvField(row.input_tokens)},${csvField(row.output_tokens)},${csvField(row.cost_usd ?? "unknown")}`,
      );
    }
    lines.push("");
    // The joined view, exported as it is rendered: an empty field means the
    // source had nothing for that model, never a measured zero.
    lines.push("Scorecard");
    lines.push(
      "model,calls,tokens,cost_usd,cost_per_call_usd,rated,down,down_rate,corrections_flagged,corrections_of,correction_rate,fallbacks",
    );
    for (const row of scorecard) {
      lines.push(
        [
          row.model,
          row.calls ?? "",
          row.tokens ?? "",
          row.costUsd ?? "unknown",
          row.costPerCall ?? "",
          row.feedback?.answers_rated ?? "",
          row.feedback?.down ?? "",
          row.feedback?.down_rate ?? "",
          row.correction?.flagged ?? "",
          row.correction?.answers ?? "",
          row.correction?.correction_rate ?? "",
          row.fallbacks,
        ]
          .map(csvField)
          .join(","),
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

            <div
              className="usage-kpi usage-kpi-cache"
              title="How often an answer was served without calling a model at all. The denominator is every request that produced an answer over this window — real calls plus cache hits — so a rising rate really does mean more answers served for free, not just fewer calls made."
            >
              {data.cache.exact_hit_rate !== null && data.cache.semantic_hit_rate !== null ? (
                <>
                  <span className="usage-kpi-figure">
                    {formatPercent(data.cache.exact_hit_rate + data.cache.semantic_hit_rate)}
                  </span>
                  <span className="usage-kpi-label">
                    cache hit rate · {formatPercent(data.cache.exact_hit_rate)} exact +{" "}
                    {formatPercent(data.cache.semantic_hit_rate)} semantic, of{" "}
                    {data.cache.total_requests.toLocaleString()} requests
                  </span>
                </>
              ) : (
                <span className="usage-kpi-label">
                  No requests yet in the last {data.days} days.
                </span>
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

            {feedback &&
            (Object.keys(feedback.by_category).length > 0 || feedback.by_lane.free) ? (
              <section className="settings-section">
                <h3>Quality by category</h3>
                {feedback.by_lane.free ? (
                  <p className="usage-budget-remaining">
                    Free lane 👎 rate: {(feedback.by_lane.free.down_rate * 100).toFixed(0)}%
                    {(() => {
                      const paid = paidLaneStat(feedback.by_lane);
                      return paid
                        ? ` vs paid lanes: ${(paid.down_rate * 100).toFixed(0)}%`
                        : " (no paid-lane ratings yet to compare)";
                    })()}
                  </p>
                ) : null}
                {Object.keys(feedback.by_category).length > 0 ? (
                  <table className="usage-model-table">
                    <thead>
                      <tr>
                        <th>Category</th>
                        <th>Rated</th>
                        <th>👍</th>
                        <th>👎</th>
                        <th>👎 rate</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(feedback.by_category).map(([category, stat]) => (
                        <tr
                          key={category}
                          className={isQualityWarning(stat) ? "usage-quality-row-warning" : ""}
                        >
                          <td>{category}</td>
                          <td>{stat.answers_rated}</td>
                          <td>{stat.up}</td>
                          <td>{stat.down}</td>
                          <td>{(stat.down_rate * 100).toFixed(0)}%</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : null}
              </section>
            ) : null}

            <section className="settings-section">
              <div className="usage-section-header">
                <h3>Scorecard</h3>
              </div>
              <p className="settings-readonly">
                Every model you used in this window, on one row: what it cost, how
                you rated it, and how often it failed. Most expensive first.
              </p>
              {scorecard.length === 0 ? (
                <p className="settings-readonly">No models used in this window.</p>
              ) : (
                <div className="usage-table-scroll">
                  <table className="usage-model-table usage-scorecard-table">
                    <thead>
                      <tr>
                        <th>Model</th>
                        <th className="usage-num">Calls</th>
                        <th className="usage-num">Tokens</th>
                        <th className="usage-num">Cost</th>
                        <th className="usage-num" title="Cost divided by calls — what one more question to this model costs you. The column to compare models on, since a cheap model called constantly can outspend an expensive one called rarely.">
                          Per call
                        </th>
                        <th className="usage-num" title="Share of rated answers you marked 👎, and how many answers that rate is based on. A rate with a small n means little — the count is shown so you can judge it.">
                          👎 rate
                        </th>
                        <th className="usage-num" title="How often you immediately re-asked, rephrased, or corrected this model's answer. A NOISY PROXY for quality, not a verified error rate: a follow-up question counts the same as a correction.">
                          Corrections
                        </th>
                        <th className="usage-num" title="How many times the router had to give up on this model and answer on another one. Counts only the last RETENTION_DAYS_DETAIL days of detail rows, so an older outage may not appear here.">
                          Fallbacks
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {scorecard.map((row) => (
                        <tr
                          key={row.model}
                          className={
                            row.feedback && isQualityWarning(row.feedback)
                              ? "usage-quality-row-warning"
                              : ""
                          }
                        >
                          <td>{row.model}</td>
                          <td className="usage-num">{row.calls ?? "—"}</td>
                          <td className="usage-num">
                            {row.tokens === null ? "—" : row.tokens.toLocaleString()}
                          </td>
                          <td className="usage-num">
                            {row.costUsd == null ? (
                              <span title="This model isn't in the price list, so its cost can't be estimated. That is not the same as free — a free model reports $0.">
                                Unknown
                              </span>
                            ) : (
                              formatCost(row.costUsd) || "$0.00"
                            )}
                          </td>
                          <td className="usage-num">
                            {row.costPerCall == null ? "—" : formatCost(row.costPerCall)}
                          </td>
                          <td className="usage-num">
                            {row.feedback ? (
                              <span title={`${row.feedback.up} 👍 / ${row.feedback.down} 👎 of ${row.feedback.answers_rated} rated`}>
                                {formatPercent(row.feedback.down_rate)}{" "}
                                <span className="usage-scorecard-n">
                                  n={row.feedback.answers_rated}
                                </span>
                              </span>
                            ) : (
                              <span title="No answers from this model have been rated in this window.">
                                —
                              </span>
                            )}
                          </td>
                          <td className="usage-num">
                            {row.correction && row.correction.answers > 0 ? (
                              <span title={`${row.correction.flagged} of ${row.correction.answers} answers looked corrected`}>
                                {formatPercent(row.correction.correction_rate)}
                              </span>
                            ) : (
                              "—"
                            )}
                          </td>
                          <td className="usage-num">
                            {row.fallbacks === 0 ? (
                              <span className="usage-scorecard-zero">0</span>
                            ) : (
                              row.fallbacks
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section className="settings-section">
              <h3>Weekly self-report</h3>
              <p className="settings-readonly">
                {reportStatus?.last_generated_at
                  ? `Last generated: ${reportStatus.last_generated_at}`
                  : "Never generated yet — one lands as a 📊 System report conversation once a week."}
              </p>
              <button
                type="button"
                className="secondary-button"
                onClick={() => void generateReportNow()}
                disabled={reportGenerating}
              >
                {reportGenerating ? "Generating…" : "📊 Generate now"}
              </button>
              {reportMessage ? <p className="settings-readonly">{reportMessage}</p> : null}
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
