import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MessageList } from "./MessageList";
import type { Message } from "./types";

function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 1,
    conversation_id: 10,
    role: "user",
    content: "Hello there",
    created_at: "2026-07-20 10:00:00",
    ...overrides,
  };
}

function makeProps(overrides: Partial<ComponentProps<typeof MessageList>> = {}) {
  return {
    messages: [],
    streaming: false,
    streamState: null,
    conversations: [],
    selectedConversation: null,
    findMatchIds: [],
    findActiveIndex: 0,
    deepLinkHighlightId: null,
    copiedMessageId: null,
    copyMessage: vi.fn(async () => {}),
    copiedLinkMessageId: null,
    copyMessageLink: vi.fn(async () => {}),
    toggleMessageBookmark: vi.fn(async () => {}),
    rateMessage: vi.fn(async () => {}),
    synthesizingMessageId: null,
    speakingMessageId: null,
    toggleSpeak: vi.fn(async () => {}),
    freeSpeakingMessageId: null,
    toggleFreeSpeak: vi.fn(),
    editingMessageId: null,
    startEdit: vi.fn(),
    busy: false,
    branchingMessageId: null,
    branchFromMessage: vi.fn(async () => {}),
    deletingMessageId: null,
    deleteMessage: vi.fn(async () => {}),
    continuingMessageId: null,
    continueMessage: vi.fn(async () => {}),
    editDraft: "",
    setEditDraft: vi.fn(),
    saveEdit: vi.fn(async () => {}),
    cancelEdit: vi.fn(),
    resolveAction: vi.fn(async () => {}),
    unansweredNotice: null,
    selectedConversationId: 10,
    canRegenerate: false,
    regenerate: vi.fn(async () => {}),
    retryAsWorkflow: vi.fn(async () => {}),
    isPinned: false,
    regenChoice: "",
    setRegenChoice: vi.fn(),
    budgetTierEnabled: false,
    outputTokenCaps: { budget: 800, fast: 1500, smart: 4000 },
    composerMode: "auto",
    forcedModelOptions: [],
    messagesEndRef: { current: null },
    messagesContainerRef: { current: null },
    showJumpToBottom: false,
    fetchSpreadsheetPreview: vi.fn(async () => null),
    ...overrides,
  };
}

describe("MessageList", () => {
  it("renders user and assistant messages with their content", () => {
    render(
      <MessageList
        {...makeProps({
          messages: [
            makeMessage({ id: 1, role: "user", content: "What is the capital of France?" }),
            makeMessage({ id: 2, role: "assistant", content: "It's Paris." }),
          ],
        })}
      />,
    );

    expect(screen.getByText("What is the capital of France?")).toBeInTheDocument();
    expect(screen.getByText("It's Paris.")).toBeInTheDocument();
  });

  it("shows the onboarding hint when there are no conversations at all", () => {
    render(<MessageList {...makeProps({ conversations: [], selectedConversation: null })} />);
    expect(screen.getByText(/Welcome to AI Workbench/)).toBeInTheDocument();
  });

  it("calls deleteMessage when a message's delete button is clicked", async () => {
    const user = userEvent.setup();
    const deleteMessage = vi.fn(async () => {});
    const message = makeMessage({ id: 3, content: "Delete me" });
    render(<MessageList {...makeProps({ messages: [message], deleteMessage })} />);

    await user.click(screen.getByRole("button", { name: /Delete user message/ }));
    expect(deleteMessage).toHaveBeenCalledWith(message);
  });

  it("shows the jump-to-bottom button and calls scrollIntoView on click", async () => {
    const user = userEvent.setup();
    const scrollIntoView = vi.spyOn(window.HTMLElement.prototype, "scrollIntoView").mockImplementation(() => {});
    render(<MessageList {...makeProps({ showJumpToBottom: true })} />);

    await user.click(screen.getByRole("button", { name: /Jump to latest/ }));
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "end", behavior: "smooth" });
    scrollIntoView.mockRestore();
  });

  it("shows the streaming bubble with the live question and answer", () => {
    render(
      <MessageList
        {...makeProps({
          streaming: true,
          streamState: {
            conversationId: 10,
            question: "Live question",
            answer: "Live answer so far",
          },
        })}
      />,
    );

    expect(screen.getByText("Live question")).toBeInTheDocument();
    expect(screen.getByText(/Live answer so far/)).toBeInTheDocument();
  });

  it("renders the answering model as a badge on the message", () => {
    const message = makeMessage({
      id: 7,
      role: "assistant",
      content: "Answered.",
      mode_used: "auto->fast",
      model: "gemini/gemini-flash-latest",
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    // The provider path is stripped for the label; the full id stays on the
    // title so nothing is lost to the truncation.
    const badge = screen.getByTitle("gemini/gemini-flash-latest");
    expect(badge).toHaveTextContent("gemini-flash-latest");
  });

  it("renders no model badge on a message with no recorded model", () => {
    // A message persisted before the model column existed — nothing at all,
    // not an "unknown" placeholder.
    const message = makeMessage({
      id: 8,
      role: "assistant",
      content: "Older answer.",
      mode_used: "auto->fast",
      model: null,
    });
    const { container } = render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(container.querySelector(".model-badge")).toBeNull();
    // ...while the mode badge it sits beside is unaffected.
    expect(screen.getByText("auto->fast")).toBeInTheDocument();
  });

  it("omits the model badge when the mode badge already names that model", () => {
    const message = makeMessage({
      id: 9,
      role: "assistant",
      content: "Pinned answer.",
      mode_used: "forced:gpt-5",
      model: "gpt-5",
    });
    const { container } = render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.getByText("forced:gpt-5")).toBeInTheDocument();
    expect(container.querySelector(".model-badge")).toBeNull();
  });

  it("renders a download link for a code-execution-generated file", () => {
    const message = makeMessage({
      id: 5,
      role: "assistant",
      content: "Here's your spreadsheet.",
      code_results: [
        {
          code: "df.to_excel('out.xlsx')",
          logs: "saved",
          images: [],
          files: [
            {
              filename: "out.xlsx",
              mime_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
              data: "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,ZmFrZQ==",
            },
          ],
        },
      ],
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    const link = screen.getByRole("link", { name: /out\.xlsx/ });
    expect(link).toHaveAttribute("download", "out.xlsx");
    expect(link).toHaveAttribute(
      "href",
      "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,ZmFrZQ==",
    );
  });

  it("offers no Continue or Retry-as-workflow on a truncated WORKFLOW answer", () => {
    // A workflow answer carries `truncated` for a step that hit its ceiling,
    // not for this text — the synthesis finished. Continue would append a
    // resumption to a complete answer (billed, recovering nothing), and
    // "Retry as workflow" would offer a workflow for something that is one.
    const message = makeMessage({
      id: 90,
      role: "assistant",
      content: "The combined answer.",
      truncated: true,
      max_output_tokens: 1500,
      workflow_steps: [
        {
          category: "summarization",
          instruction: "build the sheet",
          model: "gpt-5",
          status: "ok",
        },
      ],
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    // The notice itself stays — it is the only thing explaining a short file.
    expect(screen.getByText(/cut off at the 1,500-token/)).toBeInTheDocument();
    expect(screen.getByText(/One step of this workflow hit that ceiling/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Continue/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Retry as workflow/ })).toBeNull();
  });

  it("still offers Continue on a truncated single-shot answer", () => {
    // The gate is on being a workflow, not on being truncated — an ordinary
    // cut-off answer must keep the affordance it has always had.
    const message = makeMessage({
      id: 91,
      role: "assistant",
      content: "cut off mid",
      truncated: true,
      max_output_tokens: 1500,
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.getByRole("button", { name: /Continue/ })).toBeInTheDocument();
  });

  it("offers no Continue when the answer was cut off before it wrote anything", () => {
    // The whole ceiling went on a tool call's arguments, so this row's content
    // is the app's explanation, not a partial answer — Continue would bill a
    // call to resume an apology. The notice and "Retry as workflow" still
    // apply, because a re-run in separately capped steps is the real remedy.
    const message = makeMessage({
      id: 92,
      role: "assistant",
      content: "I ran out of output space before writing any of the answer",
      truncated: true,
      no_output: true,
      max_output_tokens: 4000,
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.getByText(/cut off at the 4,000-token/)).toBeInTheDocument();
    expect(screen.getByText(/nothing to continue/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Continue/ })).toBeNull();
    expect(screen.getByRole("button", { name: /Retry as workflow/ })).toBeInTheDocument();
  });

  it("makes a file the answer NAMES in its prose download the real attachment", () => {
    // The reported bug: the answer's own "Download Spreadsheet: <name>" link
    // went nowhere, because a generated file has no address a model could
    // write (see generatedFileLinks.tsx). The real file was reachable only
    // by opening the collapsed "Ran code" card.
    const message = makeMessage({
      id: 6,
      role: "assistant",
      content: "📊 **Download Spreadsheet:** [out.xlsx](sandbox:/mnt/data/out.xlsx)",
      code_results: [
        {
          code: "df.to_excel('out.xlsx')",
          logs: "saved",
          images: [],
          files: [
            {
              filename: "out.xlsx",
              mime_type:
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
              data: "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,ZmFrZQ==",
            },
          ],
        },
      ],
    });
    const { container } = render(<MessageList {...makeProps({ messages: [message] })} />);

    const inline = container.querySelector(".markdown-body .generated-file-link");
    expect(inline).not.toBeNull();
    expect(inline).toHaveAttribute("download", "out.xlsx");
    expect(inline).toHaveAttribute(
      "href",
      "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,ZmFrZQ==",
    );
  });

  it("never renders a prose download link for a file the answer does not carry", () => {
    // With CODE_EXECUTION off an artefact step degrades to prose and no file
    // exists at all — the live case behind the report. A dead anchor here
    // reads as a download and is not one.
    const message = makeMessage({
      id: 7,
      role: "assistant",
      content: "📊 **Download Spreadsheet:** [items_14_onwards.xlsx](sandbox:/mnt/data/x)",
    });
    const { container } = render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(container.querySelector(".markdown-body a")).toBeNull();
    expect(screen.getByText(/items_14_onwards\.xlsx/)).toBeInTheDocument();
  });

  it("shows an inline preview for a generated .xlsx file once its disclosure is opened", async () => {
    const user = userEvent.setup();
    const fetchSpreadsheetPreview = vi.fn(async () => ({
      rows: [
        ["name", "score"],
        ["alice", "10"],
      ],
      total_rows: 2,
      total_cols: 2,
      truncated: false,
      sheet_name: "Results",
    }));
    const message = makeMessage({
      id: 5,
      role: "assistant",
      content: "Here's your spreadsheet.",
      code_results: [
        {
          code: "df.to_excel('out.xlsx')",
          logs: "saved",
          images: [],
          files: [
            {
              filename: "out.xlsx",
              mime_type:
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
              data: "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,ZmFrZQ==",
            },
          ],
        },
      ],
    });
    render(
      <MessageList {...makeProps({ messages: [message], fetchSpreadsheetPreview })} />,
    );

    expect(fetchSpreadsheetPreview).not.toHaveBeenCalled();
    await user.click(screen.getByText("Preview: out.xlsx"));

    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.getByText("score")).toBeInTheDocument();
    expect(fetchSpreadsheetPreview).toHaveBeenCalledTimes(1);
  });

  it("heads the preview with the sheet name and the file's real dimensions", async () => {
    const user = userEvent.setup();
    const fetchSpreadsheetPreview = vi.fn(async () => ({
      rows: [["name", "score"]],
      total_rows: 312,
      total_cols: 8,
      truncated: true,
      sheet_name: "Q3 results",
    }));
    const message = makeMessage({
      id: 5,
      role: "assistant",
      code_results: [
        {
          code: "...",
          logs: null,
          images: [],
          files: [
            {
              filename: "out.xlsx",
              mime_type:
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
              data: "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,ZmFrZQ==",
            },
          ],
        },
      ],
    });
    render(
      <MessageList {...makeProps({ messages: [message], fetchSpreadsheetPreview })} />,
    );

    await user.click(screen.getByText("Preview: out.xlsx"));

    expect(await screen.findByText("Q3 results")).toBeInTheDocument();
    // The WHOLE file's shape, not the preview grid's.
    expect(screen.getByText("312 rows × 8 columns")).toBeInTheDocument();
  });

  it("falls back to the filename as the heading for a sheet-less .csv", async () => {
    const user = userEvent.setup();
    const fetchSpreadsheetPreview = vi.fn(async () => ({
      rows: [["a", "b"]],
      total_rows: 1,
      total_cols: 2,
      truncated: false,
      sheet_name: null,
    }));
    const message = makeMessage({
      id: 5,
      role: "assistant",
      code_results: [
        {
          code: "...",
          logs: null,
          images: [],
          files: [
            { filename: "rows.csv", mime_type: "text/csv", data: "data:text/csv;base64,ZmFrZQ==" },
          ],
        },
      ],
    });
    render(
      <MessageList {...makeProps({ messages: [message], fetchSpreadsheetPreview })} />,
    );

    await user.click(screen.getByText("Preview: rows.csv"));

    // Scoped to the meta line's own heading — "rows.csv" also appears as the
    // download link's text and in the disclosure summary.
    expect(
      await screen.findByText("rows.csv", { selector: ".spreadsheet-preview-sheet" }),
    ).toBeInTheDocument();
    expect(screen.getByText("1 row × 2 columns")).toBeInTheDocument();
  });

  it("renders the first row as a sticky-able header row, not a body row", async () => {
    const user = userEvent.setup();
    const fetchSpreadsheetPreview = vi.fn(async () => ({
      rows: [
        ["name", "score"],
        ["alice", "10"],
      ],
      total_rows: 2,
      total_cols: 2,
      truncated: false,
      sheet_name: "Sheet1",
    }));
    const message = makeMessage({
      id: 5,
      role: "assistant",
      code_results: [
        {
          code: "...",
          logs: null,
          images: [],
          files: [
            { filename: "t.csv", mime_type: "text/csv", data: "data:text/csv;base64,ZmFrZQ==" },
          ],
        },
      ],
    });
    render(
      <MessageList {...makeProps({ messages: [message], fetchSpreadsheetPreview })} />,
    );

    await user.click(screen.getByText("Preview: t.csv"));

    expect(await screen.findByRole("columnheader", { name: "name" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "score" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "alice" })).toBeInTheDocument();
  });

  it("says outright how many rows are missing when the preview is row-capped", async () => {
    const user = userEvent.setup();
    const fetchSpreadsheetPreview = vi.fn(async () => ({
      rows: [["a", "b"]],
      total_rows: 60,
      total_cols: 2,
      truncated: true,
      sheet_name: null,
    }));
    const message = makeMessage({
      id: 5,
      role: "assistant",
      code_results: [
        {
          code: "...",
          logs: null,
          images: [],
          files: [
            { filename: "big.csv", mime_type: "text/csv", data: "data:text/csv;base64,ZmFrZQ==" },
          ],
        },
      ],
    });
    render(
      <MessageList {...makeProps({ messages: [message], fetchSpreadsheetPreview })} />,
    );

    await user.click(screen.getByText("Preview: big.csv"));
    expect(
      await screen.findByText(/Showing first 1 of 60 rows — download the file for all of it\./),
    ).toBeInTheDocument();
  });

  it("names both axes when the preview is capped on rows and columns", async () => {
    const user = userEvent.setup();
    const fetchSpreadsheetPreview = vi.fn(async () => ({
      rows: [["a", "b"]],
      total_rows: 312,
      total_cols: 45,
      truncated: true,
      sheet_name: "Wide",
    }));
    const message = makeMessage({
      id: 5,
      role: "assistant",
      code_results: [
        {
          code: "...",
          logs: null,
          images: [],
          files: [
            { filename: "wide.csv", mime_type: "text/csv", data: "data:text/csv;base64,ZmFrZQ==" },
          ],
        },
      ],
    });
    render(
      <MessageList {...makeProps({ messages: [message], fetchSpreadsheetPreview })} />,
    );

    await user.click(screen.getByText("Preview: wide.csv"));
    expect(
      await screen.findByText(/first 1 of 312 rows and first 2 of 45 columns/),
    ).toBeInTheDocument();
  });

  it("shows no cap notice when the grid is the whole file", async () => {
    const user = userEvent.setup();
    const fetchSpreadsheetPreview = vi.fn(async () => ({
      rows: [
        ["a", "b"],
        ["1", "2"],
      ],
      total_rows: 2,
      total_cols: 2,
      truncated: false,
      sheet_name: "Sheet1",
    }));
    const message = makeMessage({
      id: 5,
      role: "assistant",
      code_results: [
        {
          code: "...",
          logs: null,
          images: [],
          files: [
            { filename: "small.csv", mime_type: "text/csv", data: "data:text/csv;base64,ZmFrZQ==" },
          ],
        },
      ],
    });
    render(
      <MessageList {...makeProps({ messages: [message], fetchSpreadsheetPreview })} />,
    );

    await user.click(screen.getByText("Preview: small.csv"));
    await screen.findByRole("columnheader", { name: "a" });
    expect(screen.queryByText(/Showing first/)).not.toBeInTheDocument();
  });

  it("says so rather than rendering an empty grid for a file with no rows", async () => {
    const user = userEvent.setup();
    const fetchSpreadsheetPreview = vi.fn(async () => ({
      rows: [],
      total_rows: 0,
      total_cols: 0,
      truncated: false,
      sheet_name: "Empty",
    }));
    const message = makeMessage({
      id: 5,
      role: "assistant",
      code_results: [
        {
          code: "...",
          logs: null,
          images: [],
          files: [
            { filename: "empty.csv", mime_type: "text/csv", data: "data:text/csv;base64,ZmFrZQ==" },
          ],
        },
      ],
    });
    render(
      <MessageList {...makeProps({ messages: [message], fetchSpreadsheetPreview })} />,
    );

    await user.click(screen.getByText("Preview: empty.csv"));
    expect(
      await screen.findByText("This file has no rows to preview."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("degrades to just the download link when the preview endpoint fails", async () => {
    const user = userEvent.setup();
    const fetchSpreadsheetPreview = vi.fn(async () => null);
    const message = makeMessage({
      id: 5,
      role: "assistant",
      code_results: [
        {
          code: "...",
          logs: null,
          images: [],
          files: [
            {
              filename: "corrupt.xlsx",
              mime_type:
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
              data: "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,ZmFrZQ==",
            },
          ],
        },
      ],
    });
    render(
      <MessageList {...makeProps({ messages: [message], fetchSpreadsheetPreview })} />,
    );

    const link = screen.getByRole("link", { name: /corrupt\.xlsx/ });
    await user.click(screen.getByText("Preview: corrupt.xlsx"));

    expect(
      await screen.findByText(/Preview unavailable — use the download link above\./),
    ).toBeInTheDocument();
    // The plain download link is untouched by the failed preview attempt.
    expect(link).toHaveAttribute("download", "corrupt.xlsx");
  });

  it("does not offer a preview for a non-spreadsheet generated file", () => {
    const fetchSpreadsheetPreview = vi.fn(async () => null);
    const message = makeMessage({
      id: 5,
      role: "assistant",
      code_results: [
        {
          code: "...",
          logs: null,
          images: [],
          files: [
            { filename: "report.pdf", mime_type: "application/pdf", data: "data:application/pdf;base64,ZmFrZQ==" },
          ],
        },
      ],
    });
    render(
      <MessageList {...makeProps({ messages: [message], fetchSpreadsheetPreview })} />,
    );

    expect(screen.queryByText(/Preview:/)).not.toBeInTheDocument();
  });

  it("renders no download section when a code result has no files", () => {
    const message = makeMessage({
      id: 6,
      role: "assistant",
      content: "Just logs, no files.",
      code_results: [{ code: "print(1)", logs: "1", images: [], files: null }],
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows a 'served free via <model>' note for a free-lane answer", () => {
    const message = makeMessage({
      id: 7,
      role: "assistant",
      content: "Answered via the free lane.",
      mode_used: "auto->free:groq/llama-3.3-70b-versatile",
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(
      screen.getByText("served free via groq/llama-3.3-70b-versatile"),
    ).toBeInTheDocument();
  });

  it("renders academic search results on an assistant message", () => {
    const message = makeMessage({
      id: 9,
      role: "assistant",
      content: "Here's what I found.",
      academic_results: [
        {
          title: "Climate Adaptation Strategies",
          authors: "A. Researcher",
          year: 2022,
          venue: "Nature",
          citation_count: 42,
          url: "https://example.com/paper",
          abstract_snippet: "This paper examines...",
        },
      ],
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.getByText("Climate Adaptation Strategies")).toBeInTheDocument();
    expect(screen.getByText("A. Researcher · 2022")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Nature" })).toHaveAttribute(
      "href",
      "https://example.com/paper",
    );
  });

  it("shows academic search results behind a collapsible 'Academic search' card", () => {
    const message = makeMessage({
      id: 9,
      role: "assistant",
      content: "Here's what I found.",
      academic_results: [{ title: "A paper" }],
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.getByText("Academic search")).toBeInTheDocument();
    expect(screen.getByText("A paper")).toBeInTheDocument();
  });

  it("shows fact-checks behind a collapsible 'Fact-checked' card", () => {
    const message = makeMessage({
      id: 20,
      role: "assistant",
      content: "Checking that claim.",
      fact_checks: [{ claim: "The sky is green", rating: "False" }],
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.getByText("Fact-checked")).toBeInTheDocument();
    expect(screen.getByText("The sky is green")).toBeInTheDocument();
  });

  it("shows math_solve results behind a collapsible 'Computed (math_solve)' card, with the engine used", () => {
    const message = makeMessage({
      id: 21,
      role: "assistant",
      content: "Computed exactly.",
      math_results: [
        {
          operation: "solve",
          expression: "x**2 - 4",
          variable: "x",
          result: "[-2, 2]",
          source: "sympy",
        },
      ],
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.getByText("Computed (math_solve)")).toBeInTheDocument();
    expect(screen.getByText(/via SymPy/)).toBeInTheDocument();
  });

  it("omits the engine attribution for a math_solve result with no known source", () => {
    const message = makeMessage({
      id: 22,
      role: "assistant",
      content: "Computed exactly.",
      math_results: [
        { operation: "solve", expression: "x - 1", variable: "x", result: "1" },
      ],
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.getByText("= 1")).toBeInTheDocument();
    expect(screen.queryByText(/via SymPy/)).not.toBeInTheDocument();
    expect(screen.queryByText(/via Wolfram Alpha/)).not.toBeInTheDocument();
  });

  it("shows web search queries and sources behind a collapsible 'Web search' card", () => {
    const message = makeMessage({
      id: 23,
      role: "assistant",
      content: "Here's the latest.",
      search_queries: ["current weather in Paris"],
      sources: [{ title: "Weather site", url: "https://example.com/weather" }],
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.getByText("Web search")).toBeInTheDocument();
    expect(screen.getByText("current weather in Paris")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Weather site" })).toHaveAttribute(
      "href",
      "https://example.com/weather",
    );
  });

  it("shows the 'Web search' card for queries alone, with no sources yet", () => {
    const message = makeMessage({
      id: 24,
      role: "assistant",
      content: "Searching...",
      search_queries: ["latest news on X"],
      sources: null,
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.getByText("Web search")).toBeInTheDocument();
    expect(screen.getByText("latest news on X")).toBeInTheDocument();
  });

  it("shows a memory-use indicator, expandable to the recalled conversation's title and date", () => {
    const message = makeMessage({
      id: 25,
      role: "assistant",
      content: "50000.",
      memory_sources: [
        { conversation_title: "Budget planning", created_at: "2026-03-05 12:00:00" },
      ],
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.getByText(/Used memory from 1 past conversation/)).toBeInTheDocument();
    // The disclosure's content -- the recalled conversation's own title and
    // date, never the recalled question/answer text itself.
    const source = screen.getByText("Budget planning").closest("li");
    expect(source).toHaveTextContent("Budget planning");
    expect(source).toHaveTextContent("2026");
  });

  it("pluralizes the memory indicator for more than one recalled conversation", () => {
    const message = makeMessage({
      id: 26,
      role: "assistant",
      content: "answer",
      memory_sources: [
        { conversation_title: "Budget planning", created_at: "2026-03-05 12:00:00" },
        { conversation_title: "Trip itinerary", created_at: "2026-02-01 09:00:00" },
      ],
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.getByText(/Used memory from 2 past conversations/)).toBeInTheDocument();
  });

  it("shows no memory indicator when memory contributed nothing", () => {
    const message = makeMessage({
      id: 27,
      role: "assistant",
      content: "answer",
      memory_sources: null,
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.queryByText(/Used memory/)).not.toBeInTheDocument();
  });

  it("does not show the free-lane note for a normally routed answer", () => {
    const message = makeMessage({
      id: 8,
      role: "assistant",
      content: "Answered normally.",
      mode_used: "auto->smart",
    });
    render(<MessageList {...makeProps({ messages: [message] })} />);

    expect(screen.queryByText(/served free via/)).not.toBeInTheDocument();
  });

  describe("message feedback", () => {
    it("shows thumbs up/down only on assistant messages", () => {
      const assistant = makeMessage({ id: 10, role: "assistant", content: "answer" });
      const user = makeMessage({ id: 11, role: "user", content: "question" });
      render(<MessageList {...makeProps({ messages: [user, assistant] })} />);

      expect(screen.getAllByLabelText("Rate this answer good")).toHaveLength(1);
      expect(screen.getAllByLabelText("Rate this answer bad")).toHaveLength(1);
    });

    it("marks thumbs up active when the message is rated up", () => {
      const message = makeMessage({ id: 12, role: "assistant", content: "answer", feedback: 1 });
      render(<MessageList {...makeProps({ messages: [message] })} />);

      expect(screen.getByLabelText("Remove rating from this answer")).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      expect(screen.getByLabelText("Rate this answer bad")).toBeInTheDocument();
    });

    it("clicking thumbs up calls rateMessage with 'up'", async () => {
      const rateMessage = vi.fn(async () => {});
      const user = userEvent.setup();
      const message = makeMessage({ id: 13, role: "assistant", content: "answer" });
      render(<MessageList {...makeProps({ messages: [message], rateMessage })} />);

      await user.click(screen.getByLabelText("Rate this answer good"));
      expect(rateMessage).toHaveBeenCalledWith(message, "up");
    });

    it("clicking thumbs down when unrated opens a reason popover instead of rating immediately", async () => {
      const rateMessage = vi.fn(async () => {});
      const user = userEvent.setup();
      const message = makeMessage({ id: 14, role: "assistant", content: "answer" });
      render(<MessageList {...makeProps({ messages: [message], rateMessage })} />);

      await user.click(screen.getByLabelText("Rate this answer bad"));
      expect(rateMessage).not.toHaveBeenCalled();
      expect(screen.getByRole("menu")).toBeInTheDocument();
      expect(screen.getByRole("menuitem", { name: "Wrong" })).toBeInTheDocument();
      expect(screen.getByRole("menuitem", { name: "Incomplete" })).toBeInTheDocument();
      expect(screen.getByRole("menuitem", { name: "Style/format" })).toBeInTheDocument();
      expect(screen.getByRole("menuitem", { name: "Other" })).toBeInTheDocument();
    });

    it("clicking a reason option rates down with that reason", async () => {
      const rateMessage = vi.fn(async () => {});
      const user = userEvent.setup();
      const message = makeMessage({ id: 15, role: "assistant", content: "answer" });
      render(<MessageList {...makeProps({ messages: [message], rateMessage })} />);

      await user.click(screen.getByLabelText("Rate this answer bad"));
      await user.click(screen.getByRole("menuitem", { name: "Incomplete" }));

      expect(rateMessage).toHaveBeenCalledWith(message, "down", "Incomplete");
      expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    });

    it("clicking Skip rates down with no reason", async () => {
      const rateMessage = vi.fn(async () => {});
      const user = userEvent.setup();
      const message = makeMessage({ id: 16, role: "assistant", content: "answer" });
      render(<MessageList {...makeProps({ messages: [message], rateMessage })} />);

      await user.click(screen.getByLabelText("Rate this answer bad"));
      await user.click(screen.getByRole("button", { name: "Skip" }));

      expect(rateMessage).toHaveBeenCalledWith(message, "down", undefined);
    });

    it("pressing Escape closes the popover and rates down with no reason", async () => {
      const rateMessage = vi.fn(async () => {});
      const user = userEvent.setup();
      const message = makeMessage({ id: 17, role: "assistant", content: "answer" });
      render(<MessageList {...makeProps({ messages: [message], rateMessage })} />);

      await user.click(screen.getByLabelText("Rate this answer bad"));
      await user.keyboard("{Escape}");

      expect(rateMessage).toHaveBeenCalledWith(message, "down", undefined);
      expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    });

    it("clicking away closes the popover and rates down with no reason", async () => {
      const rateMessage = vi.fn(async () => {});
      const user = userEvent.setup();
      const message = makeMessage({ id: 19, role: "assistant", content: "answer" });
      render(<MessageList {...makeProps({ messages: [message], rateMessage })} />);

      await user.click(screen.getByLabelText("Rate this answer bad"));
      expect(screen.getByRole("menu")).toBeInTheDocument();

      await user.click(document.body);

      expect(rateMessage).toHaveBeenCalledWith(message, "down", undefined);
      expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    });

    it("clicking thumbs down again when already down clears it without opening the popover", async () => {
      const rateMessage = vi.fn(async () => {});
      const user = userEvent.setup();
      const message = makeMessage({
        id: 18,
        role: "assistant",
        content: "answer",
        feedback: -1,
      });
      render(<MessageList {...makeProps({ messages: [message], rateMessage })} />);

      await user.click(screen.getByLabelText("Remove rating from this answer"));
      expect(rateMessage).toHaveBeenCalledWith(message, "down");
      expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    });
  });
  describe("a truncated answer names its ceiling and offers a remedy", () => {
    // Before this, the notice said only THAT the answer was cut off, and the
    // re-route dropdown listed every tier as though any of them were the fix.
    // For a smart-tier answer they are all capped at or below the ceiling that
    // just failed, so the control was advising a re-run of the same failure.
    const truncated = (overrides: Partial<Message> = {}) =>
      makeMessage({
        id: 90,
        role: "assistant",
        content: "cut off mid",
        truncated: true,
        max_output_tokens: 4000,
        ...overrides,
      });

    it("names the ceiling and the tier it belongs to", () => {
      render(<MessageList {...makeProps({ messages: [truncated()] })} />);

      expect(
        screen.getByText("⚠️ Response was cut off at the 4,000-token smart-tier ceiling."),
      ).toBeInTheDocument();
    });

    it("states the number without a tier when no single tier owns it", () => {
      render(
        <MessageList
          {...makeProps({
            messages: [truncated({ max_output_tokens: 2222 })],
          })}
        />,
      );

      expect(
        screen.getByText("⚠️ Response was cut off at the 2,222-token ceiling."),
      ).toBeInTheDocument();
    });

    it("falls back to the old wording when the ceiling was never recorded", () => {
      // A workflow answer, or a message written before the column existed. The
      // number is omitted rather than guessed from the current configuration,
      // which would describe a different attempt.
      render(
        <MessageList
          {...makeProps({ messages: [truncated({ max_output_tokens: null })] })}
        />,
      );

      expect(
        screen.getByText("⚠️ Response was cut off before it finished."),
      ).toBeInTheDocument();
    });

    it("offers a workflow retry, which is the one remedy with no single ceiling", async () => {
      const retryAsWorkflow = vi.fn(async () => {});
      const user = userEvent.setup();
      render(
        <MessageList {...makeProps({ messages: [truncated()], retryAsWorkflow })} />,
      );

      await user.click(screen.getByRole("button", { name: "$ Retry as workflow" }));

      expect(retryAsWorkflow).toHaveBeenCalledTimes(1);
    });

    it("withholds the workflow retry on an older truncated answer", () => {
      // Regenerate can only ever re-answer the LAST turn, so offering it here
      // would silently re-answer a different question than the one the notice
      // is attached to. Continue has no such limitation and stays.
      render(
        <MessageList
          {...makeProps({
            messages: [
              truncated(),
              makeMessage({ id: 91, role: "user", content: "a later question" }),
            ],
          })}
        />,
      );

      expect(
        screen.queryByRole("button", { name: "$ Retry as workflow" }),
      ).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "$ Continue" })).toBeInTheDocument();
    });
  });

  describe("the re-route control marks options that cannot help", () => {
    const withTruncatedLast = (overrides: Partial<Message> = {}) => [
      makeMessage({ id: 100, role: "user", content: "q" }),
      makeMessage({
        id: 101,
        role: "assistant",
        content: "cut off mid",
        truncated: true,
        max_output_tokens: 4000,
        ...overrides,
      }),
    ];

    it("annotates every tier whose ceiling is no higher than the one that failed", () => {
      render(
        <MessageList
          {...makeProps({
            messages: withTruncatedLast(),
            canRegenerate: true,
            budgetTierEnabled: true,
          })}
        />,
      );

      expect(screen.getByRole("option", { name: "budget tier — 800 cap, no more room" })).toBeInTheDocument();
      expect(screen.getByRole("option", { name: "fast tier — 1,500 cap, no more room" })).toBeInTheDocument();
      // Equal, not lower, and still no help: re-running at 4,000 hits 4,000.
      expect(screen.getByRole("option", { name: "smart tier — 4,000 cap, no more room" })).toBeInTheDocument();
    });

    it("leaves an option alone when it does have more room", () => {
      render(
        <MessageList
          {...makeProps({
            messages: withTruncatedLast({ max_output_tokens: 800 }),
            canRegenerate: true,
            budgetTierEnabled: true,
          })}
        />,
      );

      expect(screen.getByRole("option", { name: "fast tier" })).toBeInTheDocument();
      expect(screen.getByRole("option", { name: "smart tier" })).toBeInTheDocument();
      expect(screen.getByRole("option", { name: "budget tier — 800 cap, no more room" })).toBeInTheDocument();
    });

    it("never annotates re-route (auto), whose ceiling isn't known in advance", () => {
      render(
        <MessageList
          {...makeProps({ messages: withTruncatedLast(), canRegenerate: true })}
        />,
      );

      expect(screen.getByRole("option", { name: "re-route (auto)" })).toBeInTheDocument();
    });

    it("annotates a forced model using the ceiling its composer mode gives it", () => {
      // app/routing.py's forced_model branch: auto and smart take the smart
      // ceiling, fast and budget their own. So the SAME model is a remedy under
      // one composer mode and not under another.
      const props = {
        messages: withTruncatedLast({ max_output_tokens: 1500 }),
        canRegenerate: true,
        forcedModelOptions: ["gpt-5"],
      };
      const { unmount } = render(<MessageList {...makeProps({ ...props, composerMode: "auto" })} />);
      expect(screen.getByRole("option", { name: "gpt-5" })).toBeInTheDocument();
      unmount();

      render(<MessageList {...makeProps({ ...props, composerMode: "fast" })} />);
      expect(
        screen.getByRole("option", { name: "gpt-5 — 1,500 cap, no more room" }),
      ).toBeInTheDocument();
    });

    it("says nothing about ceilings when the last answer was not truncated", () => {
      render(
        <MessageList
          {...makeProps({
            messages: withTruncatedLast({ truncated: false }),
            canRegenerate: true,
            budgetTierEnabled: true,
          })}
        />,
      );

      expect(screen.getByRole("option", { name: "budget tier" })).toBeInTheDocument();
      expect(screen.getByRole("option", { name: "fast tier" })).toBeInTheDocument();
      expect(screen.getByRole("option", { name: "smart tier" })).toBeInTheDocument();
    });

    it("says nothing about ceilings before /v1/status has reported them", () => {
      render(
        <MessageList
          {...makeProps({
            messages: withTruncatedLast(),
            canRegenerate: true,
            budgetTierEnabled: true,
            outputTokenCaps: {},
          })}
        />,
      );

      expect(screen.getByRole("option", { name: "fast tier" })).toBeInTheDocument();
      expect(
        screen.getByText("⚠️ Response was cut off at the 4,000-token ceiling."),
      ).toBeInTheDocument();
    });
  });
});
