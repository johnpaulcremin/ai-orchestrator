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
    isPinned: false,
    regenChoice: "",
    setRegenChoice: vi.fn(),
    budgetTierEnabled: false,
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

  it("shows a truncation note when the preview is capped", async () => {
    const user = userEvent.setup();
    const fetchSpreadsheetPreview = vi.fn(async () => ({
      rows: [["a", "b"]],
      total_rows: 60,
      total_cols: 2,
      truncated: true,
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
    expect(await screen.findByText(/Showing 1 of 60 rows/)).toBeInTheDocument();
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
});
