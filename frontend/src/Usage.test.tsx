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
  by_day: { date: string; cost_usd: number }[];
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
    })),
    ...overrides,
  };
}

type Captured = { url: string; method: string };
let requests: Captured[];
let currentSummary: UsageSummary;

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      requests.push({ url, method });

      if (url.includes("/v1/usage")) {
        return Response.json(currentSummary);
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
    expect(await screen.findByText(/Failed to load usage/i)).toBeInTheDocument();
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<Usage apiBase="/api" getHeaders={headers} onClose={onClose} />);
    await screen.findByText("gpt-5");

    await user.click(screen.getByRole("button", { name: "Close usage" }));
    expect(onClose).toHaveBeenCalled();
  });
});
