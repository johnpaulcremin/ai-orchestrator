import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { MessageList as MessageListType } from "./MessageList";
import type { Message } from "./types";

// Regression for the crash reported from a real device: remark-gfm's
// autolink-literal extension hardcodes a regex lookbehind assertion, which
// throws "Invalid regular expression: invalid group specifier name" on any
// Safari older than 16.4 the FIRST time a message renders -- taking the
// whole app down to a blank screen. markdownSupport.ts feature-detects this
// once and MessageList drops remarkGfm entirely when unsupported. Each test
// re-imports MessageList fresh (vi.resetModules) with markdownSupport mocked,
// since the plugin list is computed once at module load.
async function freshMessageList(supportsRegexLookbehind: boolean): Promise<typeof MessageListType> {
  vi.resetModules();
  vi.doMock("./markdownSupport", () => ({ supportsRegexLookbehind }));
  const mod = await import("./MessageList");
  return mod.MessageList;
}

function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 1,
    conversation_id: 10,
    role: "assistant",
    content: "~~struck~~ text",
    created_at: "2026-07-20 10:00:00",
    ...overrides,
  };
}

function makeProps(
  MessageListComponent: typeof MessageListType,
  overrides: Partial<ComponentProps<typeof MessageListComponent>> = {},
) {
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
    ...overrides,
  } as unknown as ComponentProps<typeof MessageListComponent>;
}

afterEach(() => {
  vi.doUnmock("./markdownSupport");
});

describe("MessageList GFM fallback (remark-gfm lookbehind crash)", () => {
  it("renders GFM markdown (e.g. strikethrough) when lookbehind is supported", async () => {
    const MessageList = await freshMessageList(true);
    const message = makeMessage();
    render(<MessageList {...makeProps(MessageList, { messages: [message] })} />);
    expect(document.querySelector("del")).not.toBeNull();
  });

  it("falls back to plain CommonMark without throwing when lookbehind is unsupported", async () => {
    const MessageList = await freshMessageList(false);
    const message = makeMessage();
    expect(() =>
      render(<MessageList {...makeProps(MessageList, { messages: [message] })} />),
    ).not.toThrow();
    // GFM (strikethrough) is simply absent, not crashed -- CommonMark still
    // renders the raw text.
    expect(document.querySelector("del")).toBeNull();
    expect(screen.getByText(/struck/)).toBeInTheDocument();
  });
});
