/**
 * The per-model Scorecard's join: one row per model, gathering what that
 * model cost, how you rated it, how often you corrected it, and how often
 * the router had to give up on it.
 *
 * Four owner-scoped endpoints each hold one piece of this — `/v1/usage`,
 * `/v1/feedback/summary`, `/v1/correction/summary` and `/v1/fallback/summary`
 * — and reading a model across all four is the question the Usage panel
 * exists to answer ("is this model worth what it costs"). Kept out of
 * Usage.tsx because it is pure data work with no React in it, and because a
 * join with this many "the sources disagree" cases deserves its own tests.
 *
 * Deliberately takes the four per-model collections rather than the four API
 * envelopes: the join has no business knowing about daily budgets or cache
 * hit rates, and a caller can feed it from anywhere.
 */

// One model's row in GET /v1/usage's by_model breakdown.
export type UsageByModel = {
  model: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  // null when this model has no known cost at all (unpriced), distinct from
  // a genuinely free model, which reports 0.
  cost_usd: number | null;
};

export type FeedbackStat = {
  answers_rated: number;
  up: number;
  down: number;
  down_rate: number;
};

export type CorrectionStat = {
  flagged: number;
  answers: number;
  correction_rate: number;
};

export type FallbackModelCount = {
  model: string;
  count: number;
};

/**
 * Everything known about a single model over the window.
 *
 * Every field is nullable because the sources genuinely disagree about which
 * models they know: a free-lane model answers with no spend_log row, a
 * never-rated model has no feedback, and a model that has never failed has
 * no fallback row. Rendering "—" for those is honest; rendering 0 would
 * claim a measurement nobody took.
 */
export type ScorecardRow = {
  model: string;
  calls: number | null;
  tokens: number | null;
  // null means "not in the price list", NOT free — a genuinely free model
  // reports 0. The same distinction /v1/usage draws.
  costUsd: number | null;
  costPerCall: number | null;
  feedback: FeedbackStat | null;
  correction: CorrectionStat | null;
  fallbacks: number;
};

export type ScorecardSources = {
  spend?: UsageByModel[] | null;
  feedback?: Record<string, FeedbackStat> | null;
  corrections?: Record<string, CorrectionStat> | null;
  fallbacks?: FallbackModelCount[] | null;
};

// A row worth calling out: enough ratings to mean something, and a 👎 rate
// high enough to be worth investigating — both thresholds are somewhat
// arbitrary, chosen so a single 👎 out of one or two ratings doesn't flag.
const QUALITY_WARNING_MIN_RATED = 5;
const QUALITY_WARNING_DOWN_RATE = 0.15;

export function isQualityWarning(stat: FeedbackStat): boolean {
  return (
    stat.answers_rated >= QUALITY_WARNING_MIN_RATED &&
    stat.down_rate > QUALITY_WARNING_DOWN_RATE
  );
}

/**
 * Joins the four sources into one row per model, most expensive first.
 *
 * The row set is the UNION of every source, not just the models with spend:
 * a local model that answered 200 questions for free and was thumbed down 30
 * times has no spend_log row at all, and dropping it would hide the single
 * most useful row on the panel.
 */
export function buildScorecard({
  spend,
  feedback,
  corrections,
  fallbacks,
}: ScorecardSources): ScorecardRow[] {
  const spendRows = spend ?? [];
  const fallbackCounts = new Map((fallbacks ?? []).map((row) => [row.model, row.count]));
  const names = new Set<string>([
    ...spendRows.map((row) => row.model),
    ...Object.keys(feedback ?? {}),
    ...Object.keys(corrections ?? {}),
    ...fallbackCounts.keys(),
  ]);

  const rows = [...names].map((model) => {
    const row = spendRows.find((entry) => entry.model === model) ?? null;
    const costUsd = row?.cost_usd ?? null;
    return {
      model,
      calls: row ? row.calls : null,
      tokens: row ? row.input_tokens + row.output_tokens : null,
      costUsd,
      // Guarded against a 0-call row rather than emitting Infinity, which
      // would render as "$Infinity" in the cost column.
      costPerCall: costUsd !== null && row && row.calls > 0 ? costUsd / row.calls : null,
      feedback: feedback?.[model] ?? null,
      correction: corrections?.[model] ?? null,
      fallbacks: fallbackCounts.get(model) ?? 0,
    };
  });

  // Most expensive first — the row an operator is looking for is the one
  // costing the most, and a model with unknown cost sorts as if it were free
  // rather than jumping the queue on a number nobody has. Ties break on
  // calls then name so the order is stable across reloads.
  return rows.sort(
    (a, b) =>
      (b.costUsd ?? 0) - (a.costUsd ?? 0) ||
      (b.calls ?? 0) - (a.calls ?? 0) ||
      a.model.localeCompare(b.model),
  );
}
