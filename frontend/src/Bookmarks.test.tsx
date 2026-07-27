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
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectMessage={noop} />,
    );
    expect(await screen.findByText("Trip planning")).toBeInTheDocument();
    expect(screen.getByText("Here are some destinations to consider.")).toBeInTheDocument();
  });

  it("shows a message when there are no bookmarks", async () => {
    currentItems = [];
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectMessage={noop} />,
    );
    expect(await screen.findByText(/No bookmarks yet/i)).toBeInTheDocument();
  });

  it("shows an error message when the request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("boom", { status: 500 })),
    );
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectMessage={noop} />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to load bookmarks/i);
  });

  it("jumps to the message and closes when a bookmark row is clicked", async () => {
    const onSelectMessage = vi.fn();
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Bookmarks
        apiBase="/api"
        getHeaders={headers}
        onClose={onClose}
        onSelectMessage={onSelectMessage}
      />,
    );
    await user.click(await screen.findByText("Trip planning"));

    expect(onSelectMessage).toHaveBeenCalledWith(10, 1);
    expect(onClose).toHaveBeenCalled();
  });

  it("truncates a long message to a snippet", async () => {
    currentItems = [makeBookmark({ content: "a".repeat(250) })];
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectMessage={noop} />,
    );
    const snippet = await screen.findByText(/^a+…$/);
    expect(snippet.textContent?.length).toBe(201);
  });

  it("removes a bookmark via PUT and drops it from the list", async () => {
    const user = userEvent.setup();
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectMessage={noop} />,
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
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectMessage={noop} />,
    );
    await screen.findByText("Trip planning");

    await user.click(screen.getByRole("button", { name: "Remove bookmark from Trip planning" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to remove bookmark/i);
    expect(screen.getByText("Trip planning")).toBeInTheDocument();
  });

  it("does not jump to the message when the remove button is clicked", async () => {
    const onSelectMessage = vi.fn();
    const user = userEvent.setup();
    render(
      <Bookmarks
        apiBase="/api"
        getHeaders={headers}
        onClose={noop}
        onSelectMessage={onSelectMessage}
      />,
    );
    await screen.findByText("Trip planning");

    await user.click(screen.getByRole("button", { name: "Remove bookmark from Trip planning" }));

    expect(onSelectMessage).not.toHaveBeenCalled();
  });

  it("exports the bookmark list as a Markdown file", async () => {
    currentItems = [
      makeBookmark({ id: 1, conversation_title: "Trip planning", content: "Try Ichiran." }),
      makeBookmark({
        id: 2,
        conversation_id: 20,
        conversation_title: "Recipe ideas",
        content: "Add more garlic.",
        created_at: "2026-07-21 11:00:00",
      }),
    ];
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
      render(
        <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectMessage={noop} />,
      );
      await screen.findByText("Trip planning");

      await user.click(screen.getByRole("button", { name: "⬇️ Export" }));

      expect(capturedBlob).not.toBeNull();
      expect(capturedBlob?.type).toBe("text/markdown");
      expect(capturedFilename).toBe("ai-workbench-bookmarks.md");
      const text = await capturedBlob?.text();
      expect(text).toContain("# Bookmarked messages");
      expect(text).toContain("## Trip planning");
      expect(text).toContain("Try Ichiran.");
      expect(text).toContain("## Recipe ideas");
      expect(text).toContain("Add more garlic.");
    } finally {
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
      clickSpy.mockRestore();
    }
  });

  it("disables the Export button when there are no bookmarks", async () => {
    currentItems = [];
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectMessage={noop} />,
    );
    expect(await screen.findByRole("button", { name: "⬇️ Export" })).toBeDisabled();
  });

  it("filters bookmarks by conversation title or content as you type", async () => {
    currentItems = [
      makeBookmark({ id: 1, conversation_title: "Trip planning", content: "Try Ichiran." }),
      makeBookmark({
        id: 2,
        conversation_id: 20,
        conversation_title: "Recipe ideas",
        content: "Add more garlic.",
      }),
    ];
    const user = userEvent.setup();
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectMessage={noop} />,
    );
    await screen.findByText("Trip planning");
    expect(screen.getByText("Recipe ideas")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Search bookmarks"), "garlic");

    expect(screen.queryByText("Trip planning")).not.toBeInTheDocument();
    expect(screen.getByText("Recipe ideas")).toBeInTheDocument();
  });

  it("matches on conversation title too, not just message content", async () => {
    currentItems = [
      makeBookmark({ id: 1, conversation_title: "Trip planning", content: "Try Ichiran." }),
      makeBookmark({
        id: 2,
        conversation_id: 20,
        conversation_title: "Recipe ideas",
        content: "Add more garlic.",
      }),
    ];
    const user = userEvent.setup();
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectMessage={noop} />,
    );
    await screen.findByText("Trip planning");

    await user.type(screen.getByLabelText("Search bookmarks"), "trip");

    expect(screen.getByText("Trip planning")).toBeInTheDocument();
    expect(screen.queryByText("Recipe ideas")).not.toBeInTheDocument();
  });

  it("shows a no-matches message when the search query matches nothing", async () => {
    const user = userEvent.setup();
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={noop} onSelectMessage={noop} />,
    );
    await screen.findByText("Trip planning");

    await user.type(screen.getByLabelText("Search bookmarks"), "nonexistent term xyz");

    expect(await screen.findByText(/No bookmarks match "nonexistent term xyz"/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "⬇️ Export" })).toBeDisabled();
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Bookmarks apiBase="/api" getHeaders={headers} onClose={onClose} onSelectMessage={noop} />,
    );
    await screen.findByText("Trip planning");

    await user.click(screen.getByRole("button", { name: "Close bookmarks" }));
    expect(onClose).toHaveBeenCalled();
  });
});
