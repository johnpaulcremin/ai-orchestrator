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
let capturedImportBody: Record<string, unknown> | null;
let importShouldFail: boolean;

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
      if (url.endsWith("/v1/conversations/import")) {
        capturedImportBody = init?.body
          ? (JSON.parse(String(init.body)) as Record<string, unknown>)
          : null;
        if (importShouldFail) {
          return new Response(JSON.stringify({ detail: "import boom" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        return Response.json({ id: 42, title: capturedImportBody?.title });
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
  capturedImportBody = null;
  importShouldFail = false;
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
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );
    for (const model of models) {
      expect(screen.getByRole("checkbox", { name: model })).toBeChecked();
    }
  });

  it("runs a comparison and renders each model's result", async () => {
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );

    await user.type(screen.getByLabelText("Question"), "What is the capital of France?");
    await user.click(screen.getByRole("button", { name: "$ Compare" }));

    await waitFor(() => {
      expect(capturedBody?.question).toBe("What is the capital of France?");
    });
    expect(capturedBody?.models).toEqual(models);

    expect(await screen.findByText("Paris is the capital of France.")).toBeInTheDocument();
    expect(screen.getByText(/850 ms/)).toBeInTheDocument();
    expect(screen.getByText(/No answer — no API key/)).toBeInTheDocument();
  });

  it("calls onCostIncurred after a successful compare, so a caller can refresh a spend indicator", async () => {
    const onCostIncurred = vi.fn();
    const user = userEvent.setup();
    render(
      <Compare
        apiBase="/api"
        getHeaders={headers}
        availableModels={models}
        onClose={noop}
        onOpenConversation={noop}
        onCostIncurred={onCostIncurred}
      />,
    );

    await user.type(screen.getByLabelText("Question"), "What is the capital of France?");
    await user.click(screen.getByRole("button", { name: "$ Compare" }));

    await screen.findByText("Paris is the capital of France.");
    expect(onCostIncurred).toHaveBeenCalledTimes(1);
  });

  it("does not call onCostIncurred when the compare request fails", async () => {
    shouldFail = true;
    const onCostIncurred = vi.fn();
    const user = userEvent.setup();
    render(
      <Compare
        apiBase="/api"
        getHeaders={headers}
        availableModels={models}
        onClose={noop}
        onOpenConversation={noop}
        onCostIncurred={onCostIncurred}
      />,
    );

    await user.type(screen.getByLabelText("Question"), "hi");
    await user.click(screen.getByRole("button", { name: "$ Compare" }));

    await screen.findByText(/boom/i);
    expect(onCostIncurred).not.toHaveBeenCalled();
  });

  it("shows an error instead of requesting when fewer than 2 models are selected", async () => {
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );

    // Uncheck down to a single model.
    await user.click(screen.getByRole("checkbox", { name: "claude-sonnet-5" }));
    await user.click(screen.getByRole("checkbox", { name: "gemini/gemini-flash-latest" }));
    await user.type(screen.getByLabelText("Question"), "hi");
    await user.click(screen.getByRole("button", { name: "$ Compare" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Pick at least 2 models/i);
    expect(capturedBody).toBeNull();
  });

  it("shows an error instead of requesting when the question is empty", async () => {
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );

    await user.click(screen.getByRole("button", { name: "$ Compare" }));

    expect(await screen.findByText(/Enter a question first\./i)).toBeInTheDocument();
    expect(capturedBody).toBeNull();
  });

  it("shows an error message when the request fails", async () => {
    shouldFail = true;
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );

    await user.type(screen.getByLabelText("Question"), "hi");
    await user.click(screen.getByRole("button", { name: "$ Compare" }));

    expect(await screen.findByText("boom")).toBeInTheDocument();
  });

  it("adds a custom model via the Add button and includes it in the request", async () => {
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );

    await user.type(
      screen.getByLabelText("Add a custom model"),
      "groq/llama-3.3-70b-versatile",
    );
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(screen.getByText("groq/llama-3.3-70b-versatile")).toBeInTheDocument();
    expect(screen.getByText("Models (4 selected)")).toBeInTheDocument();
    expect(screen.getByLabelText("Add a custom model")).toHaveValue("");

    await user.type(screen.getByLabelText("Question"), "hi");
    await user.click(screen.getByRole("button", { name: "$ Compare" }));

    await waitFor(() => {
      expect(capturedBody?.models).toEqual([...models, "groq/llama-3.3-70b-versatile"]);
    });
  });

  it("adds a custom model on Enter", async () => {
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );

    await user.type(screen.getByLabelText("Add a custom model"), "ollama/llama3.1:8b{Enter}");

    expect(screen.getByText("ollama/llama3.1:8b")).toBeInTheDocument();
    expect(screen.getByText("Models (4 selected)")).toBeInTheDocument();
  });

  it("shows an error when adding a model that's already selected", async () => {
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );

    await user.type(screen.getByLabelText("Add a custom model"), "gpt-5{Enter}");

    expect(await screen.findByText(/already selected/i)).toBeInTheDocument();
    expect(screen.getByText("Models (3 selected)")).toBeInTheDocument();
  });

  it("shows an error when adding a model past the 4-model cap", async () => {
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );

    await user.type(screen.getByLabelText("Add a custom model"), "custom-one{Enter}");
    expect(screen.getByText("Models (4 selected)")).toBeInTheDocument();

    // The input and Add button disable once at the cap.
    expect(screen.getByLabelText("Add a custom model")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
  });

  it("removes a custom model via its chip's remove button", async () => {
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );

    await user.type(screen.getByLabelText("Add a custom model"), "custom-model{Enter}");
    expect(screen.getByText("Models (4 selected)")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Remove custom-model" }));

    expect(screen.queryByText("custom-model")).not.toBeInTheDocument();
    expect(screen.getByText("Models (3 selected)")).toBeInTheDocument();
  });

  it("copies the results as Markdown to the clipboard", async () => {
    const user = userEvent.setup();
    const clipboardWriteText = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockResolvedValue(undefined);
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );

    await user.type(screen.getByLabelText("Question"), "What is the capital of France?");
    await user.click(screen.getByRole("button", { name: "$ Compare" }));
    await screen.findByText("Paris is the capital of France.");

    await user.click(screen.getByRole("button", { name: "📋 Copy as Markdown" }));

    expect(clipboardWriteText).toHaveBeenCalledWith(
      expect.stringContaining("# Compare: What is the capital of France?"),
    );
    expect(clipboardWriteText).toHaveBeenCalledWith(
      expect.stringContaining("Paris is the capital of France."),
    );
    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining("850 ms"));
    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining("No answer — no API key"));
    expect(await screen.findByRole("button", { name: "✓ Copied!" })).toBeInTheDocument();
  });

  it("shows an error when copying the results fails", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );

    await user.type(screen.getByLabelText("Question"), "hi");
    await user.click(screen.getByRole("button", { name: "$ Compare" }));
    await screen.findByText("Paris is the capital of France.");

    await user.click(screen.getByRole("button", { name: "📋 Copy as Markdown" }));

    expect(await screen.findByText(/Failed to copy to clipboard\./i)).toBeInTheDocument();
  });

  it("saves a result as a new conversation pinned to that model, then opens it", async () => {
    const onOpenConversation = vi.fn();
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Compare
        apiBase="/api"
        getHeaders={headers}
        availableModels={models}
        onClose={onClose}
        onOpenConversation={onOpenConversation}
      />,
    );

    await user.type(screen.getByLabelText("Question"), "What is the capital of France?");
    await user.click(screen.getByRole("button", { name: "$ Compare" }));
    await screen.findByText("Paris is the capital of France.");

    await user.click(
      screen.getAllByRole("button", { name: "💬 Continue in new conversation" })[0],
    );

    await waitFor(() => {
      expect(capturedImportBody?.pinned_model).toBe("gpt-5");
    });
    expect(capturedImportBody?.title).toBe("What is the capital of France?");
    expect(capturedImportBody?.messages).toEqual([
      { role: "user", content: "What is the capital of France?" },
      {
        role: "assistant",
        content: "Paris is the capital of France.",
        mode_used: "compare:gpt-5",
        input_tokens: 10,
        output_tokens: 20,
        cost_usd: 0.02,
      },
    ]);
    expect(onOpenConversation).toHaveBeenCalledWith(42);
    expect(onClose).toHaveBeenCalled();
  });

  it("does not show a Continue button for a model with no answer", async () => {
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );

    await user.type(screen.getByLabelText("Question"), "hi");
    await user.click(screen.getByRole("button", { name: "$ Compare" }));
    await screen.findByText("Paris is the capital of France.");

    expect(
      screen.getAllByRole("button", { name: "💬 Continue in new conversation" }),
    ).toHaveLength(1);
  });

  it("shows an error when saving a result as a conversation fails", async () => {
    importShouldFail = true;
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );

    await user.type(screen.getByLabelText("Question"), "hi");
    await user.click(screen.getByRole("button", { name: "$ Compare" }));
    await screen.findByText("Paris is the capital of France.");

    await user.click(screen.getByRole("button", { name: "💬 Continue in new conversation" }));

    expect(await screen.findByText("import boom")).toBeInTheDocument();
  });

  it("does not show the Copy as Markdown button before any comparison has run", () => {
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={noop} onOpenConversation={noop} />,
    );
    expect(screen.queryByRole("button", { name: "📋 Copy as Markdown" })).not.toBeInTheDocument();
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Compare apiBase="/api" getHeaders={headers} availableModels={models} onClose={onClose} onOpenConversation={noop} />,
    );

    await user.click(screen.getByRole("button", { name: "Close compare" }));
    expect(onClose).toHaveBeenCalled();
  });
});
