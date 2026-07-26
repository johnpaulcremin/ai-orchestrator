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
  daily_budget_usd: number | null;
  daily_budget_per_owner_usd: number | null;
  owner_remaining_usd: number | null;
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
    daily_budget_usd: null,
    daily_budget_per_owner_usd: null,
    owner_remaining_usd: null,
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
      expect(text).toContain("date,cost_usd");
      expect(text).toContain("2026-07-14,0.5");
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
});
