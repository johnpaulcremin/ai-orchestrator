import { describe, expect, it } from "vitest";
import { buildScorecard, isQualityWarning } from "./scorecard";
import type { UsageByModel } from "./scorecard";

function spendRow(overrides: Partial<UsageByModel> & { model: string }): UsageByModel {
  return { calls: 1, input_tokens: 10, output_tokens: 10, cost_usd: 0, ...overrides };
}

describe("buildScorecard", () => {
  it("returns nothing when every source is empty", () => {
    expect(buildScorecard({})).toEqual([]);
  });

  it("joins all four sources onto one row per model", () => {
    const [row] = buildScorecard({
      spend: [spendRow({ model: "gpt-5", calls: 4, cost_usd: 0.8 })],
      feedback: { "gpt-5": { answers_rated: 10, up: 8, down: 2, down_rate: 0.2 } },
      corrections: { "gpt-5": { flagged: 1, answers: 20, correction_rate: 0.05 } },
      fallbacks: [{ model: "gpt-5", count: 3 }],
    });

    expect(row).toMatchObject({
      model: "gpt-5",
      calls: 4,
      tokens: 20,
      costUsd: 0.8,
      costPerCall: 0.2,
      fallbacks: 3,
    });
    expect(row.feedback?.down).toBe(2);
    expect(row.correction?.flagged).toBe(1);
  });

  it("includes a model that only ONE source knows about", () => {
    // The live case: an unreachable local model bills nothing and is never
    // rated, so only the fallback tally has ever heard of it. That row is
    // the whole diagnosis of the outage.
    const rows = buildScorecard({ fallbacks: [{ model: "ollama/llama3.1:8b", count: 40 }] });

    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      model: "ollama/llama3.1:8b",
      calls: null,
      tokens: null,
      costUsd: null,
      costPerCall: null,
      feedback: null,
      correction: null,
      fallbacks: 40,
    });
  });

  it("does not double-count a model that several sources know", () => {
    const rows = buildScorecard({
      spend: [spendRow({ model: "gpt-5" })],
      feedback: { "gpt-5": { answers_rated: 1, up: 1, down: 0, down_rate: 0 } },
      fallbacks: [{ model: "gpt-5", count: 1 }],
    });

    expect(rows.map((r) => r.model)).toEqual(["gpt-5"]);
  });

  it("sorts by cost, most expensive first", () => {
    const rows = buildScorecard({
      spend: [
        spendRow({ model: "cheap", cost_usd: 0.01 }),
        spendRow({ model: "pricey", cost_usd: 5 }),
        spendRow({ model: "middling", cost_usd: 0.5 }),
      ],
    });

    expect(rows.map((r) => r.model)).toEqual(["pricey", "middling", "cheap"]);
  });

  it("does not let an unpriced model jump the queue on a cost nobody has", () => {
    const rows = buildScorecard({
      spend: [
        spendRow({ model: "unpriced", cost_usd: null, calls: 99 }),
        spendRow({ model: "priced", cost_usd: 2 }),
      ],
    });

    expect(rows.map((r) => r.model)).toEqual(["priced", "unpriced"]);
  });

  it("breaks cost ties on calls, then on name, so the order is stable", () => {
    const rows = buildScorecard({
      spend: [
        spendRow({ model: "b-model", cost_usd: 1, calls: 2 }),
        spendRow({ model: "a-model", cost_usd: 1, calls: 2 }),
        spendRow({ model: "busiest", cost_usd: 1, calls: 9 }),
      ],
    });

    expect(rows.map((r) => r.model)).toEqual(["busiest", "a-model", "b-model"]);
  });

  it("leaves cost-per-call unknown when the cost itself is unknown", () => {
    // Treating an unpriced model as $0 would report a real cost as free.
    const [row] = buildScorecard({
      spend: [spendRow({ model: "mystery", cost_usd: null, calls: 4 })],
    });

    expect(row.costUsd).toBeNull();
    expect(row.costPerCall).toBeNull();
  });

  it("reports a free model's cost-per-call as 0, not as unknown", () => {
    // $0 is a measurement; null is the absence of one. The panel renders
    // them differently and must not conflate the two.
    const [row] = buildScorecard({
      spend: [spendRow({ model: "ollama/llama3.1:8b", cost_usd: 0, calls: 4 })],
    });

    expect(row.costUsd).toBe(0);
    expect(row.costPerCall).toBe(0);
  });

  it("never divides by zero calls", () => {
    const [row] = buildScorecard({
      spend: [spendRow({ model: "odd", cost_usd: 1, calls: 0 })],
    });

    expect(row.costPerCall).toBeNull();
  });

  it("treats null sources the same as absent ones", () => {
    expect(
      buildScorecard({ spend: null, feedback: null, corrections: null, fallbacks: null }),
    ).toEqual([]);
  });
});

describe("isQualityWarning", () => {
  it("flags a high down-rate backed by enough ratings", () => {
    expect(isQualityWarning({ answers_rated: 8, up: 2, down: 6, down_rate: 0.75 })).toBe(true);
  });

  it("does not flag a high rate from one or two ratings", () => {
    // The whole point of the count threshold: 1 of 2 is not a finding.
    expect(isQualityWarning({ answers_rated: 2, up: 0, down: 2, down_rate: 1 })).toBe(false);
  });

  it("does not flag an ordinary down-rate", () => {
    expect(isQualityWarning({ answers_rated: 50, up: 47, down: 3, down_rate: 0.06 })).toBe(false);
  });
});
