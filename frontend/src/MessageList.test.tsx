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
});
