import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Compare } from "./Compare";

type CompareResult = {
  model: string;
  answer: string;
  mode_used: string;
  notes: string;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  elapsed_ms: number;
};

let capturedBody: Record<string, unknown> | null;
let responseResults: CompareResult[];
let shouldFail: boolean;

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/v1/compare")) {
        capturedBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        if (shouldFail) {
          return new Response(JSON.stringify({ detail: "boom" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        return Response.json({ question: capturedBody?.question, results: responseResults });
      }
      throw new Error(`Unhandled request: ${url}`);
    }),
  );
}

const noop = () => {};
const headers = (extra: Record<string, string> = {}) => ({ ...extra });
const models = ["gpt-5", "claude-sonnet-5", "gemini/gemini-flash-latest"];

beforeEach(() => {
  capturedBody = null;
  shouldFail = false;
  responseResults = [
    {
      model: "gpt-5",
      answer: "Paris is the capital of France.",
      mode_used: "forced:gpt-5",
      notes: "n",
      input_tokens: 10,
      output_tokens: 20,
      cost_usd: 0.02,
      elapsed_ms: 850,
    },
    {
      model: "claude-sonnet-5",
      answer: "",
      mode_used: "forced:claude-sonnet-5",
      notes: "no API key",
      input_tokens: null,
      output_tokens: null,
      cost_usd: null,
      elapsed_ms: 5,
    },
  ];
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Compare", () => {
  it("pre-selects up to 4 of the available models", () => {
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} />,
    );
    for (const model of models) {
      expect(screen.getByRole("checkbox", { name: model })).toBeChecked();
    }
  });

  it("runs a comparison and renders each model's result", async () => {
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} />,
    );

    await user.type(screen.getByLabelText("Question"), "What is the capital of France?");
    await user.click(screen.getByRole("button", { name: "Compare" }));

    await waitFor(() => {
      expect(capturedBody?.question).toBe("What is the capital of France?");
    });
    expect(capturedBody?.models).toEqual(models);

    expect(await screen.findByText("Paris is the capital of France.")).toBeInTheDocument();
    expect(screen.getByText(/850 ms/)).toBeInTheDocument();
    expect(screen.getByText(/No answer — no API key/)).toBeInTheDocument();
  });

  it("shows an error instead of requesting when fewer than 2 models are selected", async () => {
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} />,
    );

    // Uncheck down to a single model.
    await user.click(screen.getByRole("checkbox", { name: "claude-sonnet-5" }));
    await user.click(screen.getByRole("checkbox", { name: "gemini/gemini-flash-latest" }));
    await user.type(screen.getByLabelText("Question"), "hi");
    await user.click(screen.getByRole("button", { name: "Compare" }));

    expect(await screen.findByText(/Pick at least 2 models/i)).toBeInTheDocument();
    expect(capturedBody).toBeNull();
  });

  it("shows an error instead of requesting when the question is empty", async () => {
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} />,
    );

    await user.click(screen.getByRole("button", { name: "Compare" }));

    expect(await screen.findByText(/Enter a question first\./i)).toBeInTheDocument();
    expect(capturedBody).toBeNull();
  });

  it("shows an error message when the request fails", async () => {
    shouldFail = true;
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} />,
    );

    await user.type(screen.getByLabelText("Question"), "hi");
    await user.click(screen.getByRole("button", { name: "Compare" }));

    expect(await screen.findByText("boom")).toBeInTheDocument();
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={onClose} />,
    );

    await user.click(screen.getByRole("button", { name: "Close compare" }));
    expect(onClose).toHaveBeenCalled();
  });
});
