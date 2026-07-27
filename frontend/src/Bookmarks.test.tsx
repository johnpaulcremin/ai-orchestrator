import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Bookmarks } from "./Bookmarks";

type BookmarkedMessage = {
  id: number;
  conversation_id: number;
  conversation_title: string;
  role: string;
  content: string;
  created_at: string;
};

function makeBookmark(overrides: Partial<BookmarkedMessage> = {}): BookmarkedMessage {
  return {
    id: 1,
    conversation_id: 10,
    conversation_title: "Trip planning",
    role: "assistant",
    content: "Here are some destinations to consider.",
    created_at: "2026-07-20 10:00:00",
    ...overrides,
  };
}

let currentItems: BookmarkedMessage[];
let putRequests: { url: string; body: unknown }[];
let putShouldFail: boolean;

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      if (url.includes("/v1/bookmarks") && method === "GET") {
        return Response.json(currentItems);
      }
      if (/\/messages\/\d+\/bookmark$/.test(url) && method === "PUT") {
        putRequests.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
        if (putShouldFail) {
          return new Response("boom", { status: 500 });
        }
        return Response.json({ status: "ok" });
      }
      throw new Error(`Unhandled request: ${method} ${url}`);
    }),
  );
}

const noop = () => {};
const headers = (extra: Record<string, string> = {}) => ({ ...extra });

beforeEach(() => {
  currentItems = [makeBookmark()];
  putRequests = [];
  putShouldFail = false;
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Bookmarks", () => {
  it("loads and renders each bookmarked message with its conversation title", async () => {
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectConversation={noop} />,
    );
    expect(await screen.findByText("Trip planning")).toBeInTheDocument();
    expect(screen.getByText("Here are some destinations to consider.")).toBeInTheDocument();
  });

  it("shows a message when there are no bookmarks", async () => {
    currentItems = [];
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectConversation={noop} />,
    );
    expect(await screen.findByText(/No bookmarks yet/i)).toBeInTheDocument();
  });

  it("shows an error message when the request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("boom", { status: 500 })),
    );
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectConversation={noop} />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to load bookmarks/i);
  });

  it("selects the conversation and closes when a bookmark row is clicked", async () => {
    const onSelectConversation = vi.fn();
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Bookmarks
        apiBase="/api"
        getHeaders={headers}
        onClose={onClose}
        onSelectConversation={onSelectConversation}
      />,
    );
    await user.click(await screen.findByText("Trip planning"));

    expect(onSelectConversation).toHaveBeenCalledWith(10);
    expect(onClose).toHaveBeenCalled();
  });

  it("truncates a long message to a snippet", async () => {
    currentItems = [makeBookmark({ content: "a".repeat(250) })];
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectConversation={noop} />,
    );
    const snippet = await screen.findByText(/^a+…$/);
    expect(snippet.textContent?.length).toBe(201);
  });

  it("removes a bookmark via PUT and drops it from the list", async () => {
    const user = userEvent.setup();
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectConversation={noop} />,
    );
    await screen.findByText("Trip planning");

    await user.click(screen.getByRole("button", { name: "Remove bookmark from Trip planning" }));

    expect(await screen.findByText(/No bookmarks yet/i)).toBeInTheDocument();
    expect(putRequests).toEqual([
      { url: "/api/v1/conversations/10/messages/1/bookmark", body: { bookmarked: false } },
    ]);
  });

  it("shows an error and keeps the row when removing a bookmark fails", async () => {
    putShouldFail = true;
    const user = userEvent.setup();
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectConversation={noop} />,
    );
    await screen.findByText("Trip planning");

    await user.click(screen.getByRole("button", { name: "Remove bookmark from Trip planning" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to remove bookmark/i);
    expect(screen.getByText("Trip planning")).toBeInTheDocument();
  });

  it("does not select the conversation when the remove button is clicked", async () => {
    const onSelectConversation = vi.fn();
    const user = userEvent.setup();
    render(
      <Bookmarks
        apiBase="/api"
        getHeaders={headers}
        onClose={noop}
        onSelectConversation={onSelectConversation}
      />,
    );
    await screen.findByText("Trip planning");

    await user.click(screen.getByRole("button", { name: "Remove bookmark from Trip planning" }));

    expect(onSelectConversation).not.toHaveBeenCalled();
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={onClose} onSelectConversation={noop} />,
    );
    await screen.findByText("Trip planning");

    await user.click(screen.getByRole("button", { name: "Close bookmarks" }));
    expect(onClose).toHaveBeenCalled();
  });
});
