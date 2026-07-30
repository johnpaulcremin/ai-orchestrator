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

type Captured = { url: string; method: string };
let requests: Captured[];
let currentSummary: UsageSummary;
let currentFreeTier: FreeTierStatus;
let currentFeedback: FeedbackSummary;

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
      if (url.includes("/v1/usage")) {
        return Response.json(currentSummary);
      }
      if (url.includes("/v1/free-tier")) {
        return Response.json(currentFreeTier);
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

  it("requests the selected window when changed", async () => {
    const user = userEvent.setup();
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("gpt-5");

    await user.selectOptions(screen.getByLabelText("Usage window"), "30");

    await waitFor(() => {
      expect(requests.some((r) => r.url.includes("days=30"))).toBe(true);
    });
  });

  it("shows a message when no spend is recorded in the window", async () => {
    currentSummary = makeSummary({ by_model: [] });
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByText(/No spend recorded in this window\./i)).toBeInTheDocument();
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

  it("shows the Quality section with by-model stats", async () => {
    currentFeedback = {
      by_model: {
        "gpt-5": { answers_rated: 10, up: 9, down: 1, down_rate: 0.1 },
      },
      by_category: {},
      by_lane: {},
    };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText("Quality")).toBeInTheDocument();
    expect(screen.getByText("10%")).toBeInTheDocument();
  });

  it("hides the Quality section when nothing has been rated", async () => {
    currentFeedback = { by_model: {}, by_category: {}, by_lane: {} };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("gpt-5");
    expect(screen.queryByText("Quality")).not.toBeInTheDocument();
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

    await screen.findByText("Quality");
    const row = screen.getAllByText("gpt-5-mini")[0].closest("tr");
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

    await screen.findByText("Quality");
    const row = screen.getAllByText("gpt-5-mini")[0].closest("tr");
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

  it("shows by-category stats alongside by-model stats", async () => {
    currentFeedback = {
      by_model: {},
      by_category: {
        coding: { answers_rated: 6, up: 5, down: 1, down_rate: 1 / 6 },
      },
      by_lane: {},
    };
    render(<Usage apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Quality");
    expect(screen.getByText("coding")).toBeInTheDocument();
  });
});
