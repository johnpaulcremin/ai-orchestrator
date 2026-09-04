import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Usage } from "./Usage";

type UsageSummary = {
  today_usd: number;
  days: number;
  by_model: {
    model: string;
    calls: number;
    input_tokens: number;
    output_tokens: number;
    cost_usd: number | null;
  }[];
  by_day: { date: string; cost_usd: number; tokens: number }[];
  daily_budget_usd: number | null;
  daily_budget_per_owner_usd: number | null;
  owner_remaining_usd: number | null;
  tokens_per_dollar: number | null;
  window_tokens: number;
  cache: {
    total_requests: number;
    exact_hits: number;
    semantic_hits: number;
    exact_hit_rate: number | null;
    semantic_hit_rate: number | null;
    avoided_cost_usd: number;
  };
};

function makeSummary(overrides: Partial<UsageSummary> = {}): UsageSummary {
  return {
    today_usd: 0.5,
    days: 14,
    by_model: [
      { model: "gpt-5", calls: 3, input_tokens: 300, output_tokens: 600, cost_usd: 0.4 },
      { model: "gpt-5-mini", calls: 5, input_tokens: 200, output_tokens: 200, cost_usd: 0.1 },
    ],
    by_day: Array.from({ length: 14 }, (_, i) => ({
      date: `2026-07-${String(i + 1).padStart(2, "0")}`,
      cost_usd: i === 13 ? 0.5 : 0,
      tokens: i === 13 ? 1300 : 0,
    })),
    daily_budget_usd: null,
    daily_budget_per_owner_usd: null,
    owner_remaining_usd: null,
    tokens_per_dollar: 2600,
    window_tokens: 1300,
    // 8 real calls (by_model above) + 2 cache hits = 10 requests, so the
    // rates below are the ones the panel should render.
    cache: {
      total_requests: 10,
      exact_hits: 1,
      semantic_hits: 1,
      exact_hit_rate: 0.1,
      semantic_hit_rate: 0.1,
      avoided_cost_usd: 0.03,
    },
    ...overrides,
  };
}

type FreeTierStatus = {
  enabled: boolean;
  models: { model: string; quota: number; used: number; remaining: number }[];
};

type FeedbackStat = { answers_rated: number; up: number; down: number; down_rate: number };
type FeedbackSummary = {
  by_model: Record<string, FeedbackStat>;
  by_category: Record<string, FeedbackStat>;
  by_lane: Record<string, FeedbackStat>;
};

type CorrectionStat = { flagged: number; answers: number; correction_rate: number };
type CorrectionSummary = { by_model: Record<string, CorrectionStat> };
type FallbackSummary = { models: { model: string; count: number }[] };

type SelfReportStatus = { last_generated_at: string | null; narrate_enabled: boolean };

type Captured = { url: string; method: string };
let requests: Captured[];
let currentSummary: UsageSummary;
let currentFreeTier: FreeTierStatus;
let currentFeedback: FeedbackSummary;
let currentCorrection: CorrectionSummary;
let currentFallback: FallbackSummary;
let currentSelfReportStatus: SelfReportStatus;
let selfReportGenerateOk: boolean;

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      requests.push({ url, method });

      if (url.includes("/v1/feedback/summary")) {
        return Response.json(currentFeedback);
      }
      if (url.includes("/v1/correction/summary")) {
        return Response.json(currentCorrection);
      }
      if (url.includes("/v1/fallback/summary")) {
        return Response.json(currentFallback);
      }
      if (url.includes("/v1/usage")) {
        return Response.json(currentSummary);
      }
      if (url.includes("/v1/free-tier")) {
        return Response.json(currentFreeTier);
      }
      if (url.includes("/v1/self-report/generate")) {
        if (!selfReportGenerateOk) {
          return new Response(null, { status: 500 });
        }
        currentSelfReportStatus = {
          last_generated_at: "2026-07-20 12:00:00",
          narrate_enabled: false,
        };
        return Response.json({ conversation_id: 42, narrated: false });
      }
      if (url.includes("/v1/self-report/status")) {
        return Response.json(currentSelfReportStatus);
      }
      throw new Error(`Unhandled request: ${method} ${url}`);
    }),
  );
}

const noop = () => {};
const headers = (extra: Record<string, string> = {}) => ({ ...extra });

beforeEach(() => {
  requests = [];
  currentSummary = makeSummary();
  currentFreeTier = { enabled: false, models: [] };
  currentFeedback = { by_model: {}, by_category: {}, by_lane: {} };
  currentCorrection = { by_model: {} };
  currentFallback = { models: [] };
  currentSelfReportStatus = { last_generated_at: null, narrate_enabled: false };
  selfReportGenerateOk = true;
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Usage", () => {
  it("loads and renders today's spend and the by-model breakdown", async () => {
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText("$0.5000")).toBeInTheDocument();
    expect(screen.getByText("spent today")).toBeInTheDocument();
    expect(screen.getByText("gpt-5")).toBeInTheDocument();
    expect(screen.getByText("gpt-5-mini")).toBeInTheDocument();
    expect(screen.getByText("900")).toBeInTheDocument(); // gpt-5: 300 + 600 tokens
  });

  it("shows the tokens-per-dollar KPI when the window has spend", async () => {
    currentSummary = makeSummary({ tokens_per_dollar: 12345.6, window_tokens: 50000 });
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText("12,346")).toBeInTheDocument();
    expect(screen.getByText("tokens per $1 · last 14 days")).toBeInTheDocument();
  });

  it("shows an 'All free' KPI when the window has tokens but zero spend", async () => {
    currentSummary = makeSummary({ tokens_per_dollar: null, window_tokens: 8000 });
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText("All free")).toBeInTheDocument();
    expect(screen.getByText("8,000 tokens, $0 spent · last 14 days")).toBeInTheDocument();
  });

  it("shows a no-usage KPI message when the window is entirely empty", async () => {
    currentSummary = makeSummary({ tokens_per_dollar: null, window_tokens: 0 });
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText("No usage yet in the last 14 days.")).toBeInTheDocument();
  });

  it("shows the cache hit rate, split into exact and semantic", async () => {
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText("20%")).toBeInTheDocument();
    expect(
      screen.getByText("cache hit rate · 10% exact + 10% semantic, of 10 requests"),
    ).toBeInTheDocument();
  });

  it("reports a rate that rounds to zero as '<1%' rather than 0%", async () => {
    // A cache working a little must never render as one that is not working.
    currentSummary = makeSummary({
      cache: {
        total_requests: 1000,
        exact_hits: 2,
        semantic_hits: 0,
        exact_hit_rate: 0.002,
        semantic_hit_rate: 0,
        avoided_cost_usd: 0.01,
      },
    });
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(
      await screen.findByText("cache hit rate · <1% exact + 0% semantic, of 1,000 requests"),
    ).toBeInTheDocument();
  });

  it("shows a no-requests message instead of a 0% cache rate on an empty window", async () => {
    currentSummary = makeSummary({
      cache: {
        total_requests: 0,
        exact_hits: 0,
        semantic_hits: 0,
        exact_hit_rate: null,
        semantic_hit_rate: null,
        avoided_cost_usd: 0,
      },
    });
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText("No requests yet in the last 14 days.")).toBeInTheDocument();
  });

  it("requests the selected window when changed", async () => {
    const user = userEvent.setup();
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("gpt-5");

    await user.selectOptions(screen.getByLabelText("Usage window"), "30");

    await waitFor(() => {
      expect(requests.some((r) => r.url.includes("days=30"))).toBe(true);
    });
  });

  it("shows a message when no model was used in the window", async () => {
    currentSummary = makeSummary({ by_model: [] });
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByText(/No models used in this window\./i)).toBeInTheDocument();
  });

  it("shows Unknown, not $0.00, for a model with no known cost", async () => {
    currentSummary = makeSummary({
      by_model: [
        {
          model: "some-custom-model",
          calls: 2,
          input_tokens: 100,
          output_tokens: 100,
          cost_usd: null,
        },
      ],
    });
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByText("some-custom-model")).toBeInTheDocument();
    expect(screen.getByText("Unknown")).toBeInTheDocument();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("shows an error message when the request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("boom", { status: 500 })),
    );
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to load usage/i);
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<Usage apiBase="/api" getHeaders={headers} onClose={onClose} />);
    await screen.findByText("gpt-5");

    await user.click(screen.getByRole("button", { name: "Close usage" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("shows nothing budget-related when no cap is configured", async () => {
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("gpt-5");
    expect(screen.queryByText(/daily cap/i)).not.toBeInTheDocument();
  });

  it("shows the global cap when only that one is configured", async () => {
    currentSummary = makeSummary({ daily_budget_usd: 10 });
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByText(/Global daily cap: \$10\.0000/)).toBeInTheDocument();
  });

  it("shows the caller's own remaining budget when a per-owner cap is configured", async () => {
    currentSummary = makeSummary({
      daily_budget_per_owner_usd: 1.0,
      owner_remaining_usd: 0.65,
    });
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(
      await screen.findByText(/\$0\.6500 left of your \$1\.0000 daily cap/),
    ).toBeInTheDocument();
  });

  it("prefers the per-owner remaining figure over the global cap line when both are set", async () => {
    currentSummary = makeSummary({
      daily_budget_usd: 50,
      daily_budget_per_owner_usd: 1.0,
      owner_remaining_usd: 0.2,
    });
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByText(/left of your \$1\.0000 daily cap/)).toBeInTheDocument();
    expect(screen.queryByText(/^Global daily cap:/)).not.toBeInTheDocument();
  });

  it("exports the daily spend and by-model breakdown as one CSV file", async () => {
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    let capturedBlob: Blob | null = null;
    let capturedFilename = "";
    URL.createObjectURL = vi.fn((blob: Blob) => {
      capturedBlob = blob;
      return "blob:fake-url";
    });
    URL.revokeObjectURL = vi.fn();
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        capturedFilename = this.download;
      });

    try {
      const user = userEvent.setup();
      render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
      await screen.findByText("gpt-5");

      await user.click(screen.getByRole("button", { name: "⬇️ Export CSV" }));

      expect(capturedBlob).not.toBeNull();
      expect(capturedBlob?.type).toBe("text/csv");
      expect(capturedFilename).toBe("ai-workbench-usage-14d.csv");
      const text = await capturedBlob?.text();
      expect(text).toContain("Daily spend");
      expect(text).toContain("date,cost_usd,tokens");
      expect(text).toContain("2026-07-14,0.5,1300");
      expect(text).toContain("Efficiency");
      expect(text).toContain("window_tokens,tokens_per_dollar");
      expect(text).toContain("1300,2600");
      expect(text).toContain("By model");
      expect(text).toContain("model,calls,input_tokens,output_tokens,cost_usd");
      expect(text).toContain("gpt-5,3,300,600,0.4");
    } finally {
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
      clickSpy.mockRestore();
    }
  });

  it("shows Unknown as the CSV cost for a model with no known price", async () => {
    currentSummary = makeSummary({
      by_model: [
        {
          model: "some-custom-model",
          calls: 2,
          input_tokens: 100,
          output_tokens: 100,
          cost_usd: null,
        },
      ],
    });
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    let capturedBlob: Blob | null = null;
    URL.createObjectURL = vi.fn((blob: Blob) => {
      capturedBlob = blob;
      return "blob:fake-url";
    });
    URL.revokeObjectURL = vi.fn();
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    try {
      const user = userEvent.setup();
      render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
      await screen.findByText("some-custom-model");

      await user.click(screen.getByRole("button", { name: "⬇️ Export CSV" }));

      const text = await capturedBlob?.text();
      expect(text).toContain("some-custom-model,2,100,100,unknown");
    } finally {
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
      clickSpy.mockRestore();
    }
  });

  it("shows the free-lane remaining-today table when the lane is enabled", async () => {
    currentFreeTier = {
      enabled: true,
      models: [
        { model: "gemini/gemini-flash-latest", quota: 100, used: 30, remaining: 70 },
        { model: "groq/llama-3.3-70b-versatile", quota: 50, used: 50, remaining: 0 },
      ],
    };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText("Free lane remaining today")).toBeInTheDocument();
    expect(screen.getByText("gemini/gemini-flash-latest")).toBeInTheDocument();
    expect(screen.getByText("groq/llama-3.3-70b-versatile")).toBeInTheDocument();
  });

  it("hides the free-lane section when the lane isn't enabled", async () => {
    currentFreeTier = { enabled: false, models: [] };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("gpt-5");
    expect(screen.queryByText("Free lane remaining today")).not.toBeInTheDocument();
  });

  it("puts a rated model's down-rate on its Scorecard row", async () => {
    currentFeedback = {
      by_model: {
        "gpt-5": { answers_rated: 10, up: 9, down: 1, down_rate: 0.1 },
      },
      by_category: {},
      by_lane: {},
    };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Scorecard");
    const row = screen.getByText("gpt-5").closest("tr");
    expect(row).toHaveTextContent("10%");
    expect(row).toHaveTextContent("n=10");
  });

  it("joins spend and ratings for one model onto a single Scorecard row", async () => {
    // The default summary fixture (makeSummary) gives gpt-5 3 calls / $0.4.
    currentFeedback = {
      by_model: {
        "gpt-5": { answers_rated: 2, up: 2, down: 0, down_rate: 0 },
      },
      by_category: {},
      by_lane: {},
    };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Scorecard");
    // One row, carrying BOTH sources — the join the panel used to leave to
    // the reader across two separate tables.
    const rows = screen.getAllByText("gpt-5").map((el) => el.closest("tr"));
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("3");
    expect(rows[0]).toHaveTextContent("$0.4000");
    expect(rows[0]).toHaveTextContent("n=2");
  });

  it("still lists a rated model that has no spend row at all", async () => {
    // A free-lane model bills nothing, so it never appears in by_model.
    // Dropping it would hide the row most worth reading: lots of answers,
    // no cost, and whatever quality it actually delivered.
    currentFeedback = {
      by_model: {
        "some-untracked-model": { answers_rated: 1, up: 1, down: 0, down_rate: 0 },
      },
      by_category: {},
      by_lane: {},
    };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Scorecard");
    const row = screen.getByText("some-untracked-model").closest("tr");
    // Dashes for the columns nobody measured, not zeroes.
    expect(row).toHaveTextContent("—");
  });

  it("hides the by-category section when nothing has been rated", async () => {
    currentFeedback = { by_model: {}, by_category: {}, by_lane: {} };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("gpt-5");
    expect(screen.queryByText("Quality by category")).not.toBeInTheDocument();
  });

  it("highlights a model row when its down-rate exceeds the warning threshold", async () => {
    currentFeedback = {
      by_model: {
        "gpt-5-mini": { answers_rated: 8, up: 2, down: 6, down_rate: 0.75 },
      },
      by_category: {},
      by_lane: {},
    };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Scorecard");
    const row = screen.getByText("gpt-5-mini").closest("tr");
    expect(row).toHaveClass("usage-quality-row-warning");
  });

  it("does not highlight a row below the ratings-count threshold even with a high down-rate", async () => {
    currentFeedback = {
      by_model: {
        "gpt-5-mini": { answers_rated: 2, up: 0, down: 2, down_rate: 1.0 },
      },
      by_category: {},
      by_lane: {},
    };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Scorecard");
    const row = screen.getByText("gpt-5-mini").closest("tr");
    expect(row).not.toHaveClass("usage-quality-row-warning");
  });

  it("shows the free-vs-paid down-rate headline when the free lane has ratings", async () => {
    currentFeedback = {
      by_model: { "groq/llama-3": { answers_rated: 4, up: 2, down: 2, down_rate: 0.5 } },
      by_category: {},
      by_lane: {
        free: { answers_rated: 4, up: 2, down: 2, down_rate: 0.5 },
        smart: { answers_rated: 10, up: 9, down: 1, down_rate: 0.1 },
      },
    };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(
      await screen.findByText(/Free lane 👎 rate: 50% vs paid lanes: 10%/),
    ).toBeInTheDocument();
  });

  it("shows by-category stats, which the per-model Scorecard cannot cover", async () => {
    // spend_log has no category column, so there is nothing to join a
    // category to — this table stays separate rather than being folded in.
    currentFeedback = {
      by_model: {},
      by_category: {
        coding: { answers_rated: 6, up: 5, down: 1, down_rate: 1 / 6 },
      },
      by_lane: {},
    };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Quality by category");
    expect(screen.getByText("coding")).toBeInTheDocument();
  });

  it("orders the Scorecard by cost, most expensive first", async () => {
    // The row an operator is hunting for is the one costing the most.
    currentSummary = makeSummary({
      by_model: [
        { model: "cheap", calls: 90, input_tokens: 10, output_tokens: 10, cost_usd: 0.01 },
        { model: "pricey", calls: 2, input_tokens: 10, output_tokens: 10, cost_usd: 5 },
        { model: "middling", calls: 4, input_tokens: 10, output_tokens: 10, cost_usd: 0.5 },
      ],
    });
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Scorecard");
    const table = screen.getByText("Scorecard").closest("section")!;
    const names = [...table.querySelectorAll("tbody tr td:first-child")].map(
      (cell) => cell.textContent,
    );
    expect(names).toEqual(["pricey", "middling", "cheap"]);
  });

  it("shows what one more call to each model costs", async () => {
    // gpt-5: $0.4 over 3 calls. The column that makes two models comparable
    // when one is called constantly and the other rarely.
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Scorecard");
    const row = screen.getByText("gpt-5").closest("tr");
    expect(row).toHaveTextContent("$0.1333");
  });

  it("does not invent a per-call cost for an unpriced model", async () => {
    currentSummary = makeSummary({
      by_model: [
        { model: "mystery", calls: 4, input_tokens: 10, output_tokens: 10, cost_usd: null },
      ],
    });
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Scorecard");
    const cells = [...screen.getByText("mystery").closest("tr")!.querySelectorAll("td")];
    expect(cells[3]).toHaveTextContent("Unknown");
    // Read the per-call CELL, not the row: a treat-unknown-as-zero bug
    // renders "$0" there, and a row-level assertion would sail past it
    // because "$0" appears elsewhere on the row anyway.
    expect(cells[4]).toHaveTextContent("—");
  });

  it("shows how often the router had to fall back away from a model", async () => {
    currentFallback = { models: [{ model: "gpt-5", count: 7 }] };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Scorecard");
    expect(screen.getByText("gpt-5").closest("tr")).toHaveTextContent("7");
  });

  it("lists a model that only ever failed, even with no spend and no ratings", async () => {
    // The live case this panel exists for: Ollama unreachable, so the model
    // has no spend row, no ratings — and 40 fallbacks away from it. That row
    // is the whole diagnosis, and a spend-only table could never show it.
    currentFallback = { models: [{ model: "ollama/llama3.1:8b", count: 40 }] };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Scorecard");
    const row = screen.getByText("ollama/llama3.1:8b").closest("tr");
    expect(row).toHaveTextContent("40");
  });

  it("shows a model's correction rate", async () => {
    currentCorrection = {
      by_model: { "gpt-5": { flagged: 3, answers: 12, correction_rate: 0.25 } },
    };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Scorecard");
    expect(screen.getByText("gpt-5").closest("tr")).toHaveTextContent("25%");
  });

  it("renders the Scorecard when the correction and fallback endpoints fail", async () => {
    // Both are best-effort extras. A deployment that has not migrated them,
    // or a transient 500, must cost those two columns and nothing else.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.includes("/v1/correction/summary") || url.includes("/v1/fallback/summary")) {
          return new Response(null, { status: 500 });
        }
        if (url.includes("/v1/feedback/summary")) return Response.json(currentFeedback);
        if (url.includes("/v1/usage")) return Response.json(currentSummary);
        if (url.includes("/v1/free-tier")) return Response.json(currentFreeTier);
        return Response.json(currentSelfReportStatus);
      }),
    );
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Scorecard");
    expect(screen.getByText("gpt-5").closest("tr")).toHaveTextContent("$0.4000");
  });

  it("refetches every source when the window length changes", async () => {
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("Scorecard");
    requests.length = 0;

    await userEvent.selectOptions(screen.getByLabelText("Usage window"), "30");

    await waitFor(() => {
      // Every column of a row must describe the same period, so all four
      // window-scoped sources move together.
      for (const path of ["/v1/usage", "/v1/feedback/summary", "/v1/correction/summary", "/v1/fallback/summary"]) {
        expect(requests.some((r) => r.url.includes(path) && r.url.includes("days=30"))).toBe(true);
      }
    });
  });

  it("shows never-generated messaging when no self-report has run yet", async () => {
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText("Weekly self-report")).toBeInTheDocument();
    expect(screen.getByText(/Never generated yet/)).toBeInTheDocument();
  });

  it("shows the last-generated timestamp when a self-report has already run", async () => {
    currentSelfReportStatus = { last_generated_at: "2026-07-15 09:00:00", narrate_enabled: false };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(
      await screen.findByText("Last generated: 2026-07-15 09:00:00"),
    ).toBeInTheDocument();
  });

  it("generates a report on demand and shows a confirmation", async () => {
    const user = userEvent.setup();
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    const button = await screen.findByRole("button", { name: "📊 Generate now" });
    await user.click(button);

    expect(await screen.findByText("Report generated — check your conversation list.")).toBeInTheDocument();
    expect(requests.some((r) => r.url.includes("/v1/self-report/generate") && r.method === "POST")).toBe(true);
    expect(await screen.findByText("Last generated: 2026-07-20 12:00:00")).toBeInTheDocument();
  });

  it("shows a failure message when generating the report fails", async () => {
    selfReportGenerateOk = false;
    const user = userEvent.setup();
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    const button = await screen.findByRole("button", { name: "📊 Generate now" });
    await user.click(button);

    expect(await screen.findByText("Failed to generate the report.")).toBeInTheDocument();
  });
});
