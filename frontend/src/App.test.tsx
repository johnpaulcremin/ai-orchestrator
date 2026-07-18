import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";

type Msg = {
  id: number;
  conversation_id: number;
  role: string;
  content: string;
  mode_used?: string | null;
  notes?: string | null;
  created_at: string;
};

const SSE_BODY = [
  'event: meta\ndata: {"request_id":"r","mode_used":"auto->fast","model":"gpt-5-mini","notes":"n"}\n\n',
  'event: delta\ndata: {"text":"Hello "}\n\n',
  'event: delta\ndata: {"text":"world"}\n\n',
  'event: done\ndata: {"answer":"Hello world","mode_used":"auto->fast","notes":"n"}\n\n',
].join("");

const encoder = new TextEncoder();

function streamResponse(body: string): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(body));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

// A hand-rolled fetch stub: MSW's streamed bodies do not flow through
// undici+jsdom, so we drive the SSE endpoint with a real ReadableStream here.
let messages: Msg[];
let capturedAuthHeader: string | null;

beforeEach(() => {
  messages = [];
  capturedAuthHeader = null;
  window.localStorage.clear();

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      const headers = new Headers(init?.headers);

      if (url.endsWith("/v1/conversations") && method === "GET") {
        return Response.json([
          { id: 1, title: "First chat", created_at: "2026-07-18 10:00:00", updated_at: "2026-07-18 10:00:00" },
        ]);
      }
      if (/\/v1\/conversations\/\d+\/messages$/.test(url) && method === "GET") {
        return Response.json(messages);
      }
      if (/\/ask\/stream$/.test(url) && method === "POST") {
        capturedAuthHeader = headers.get("authorization");
        // Simulate the backend persisting the pair before the refetch.
        messages = [
          { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
          {
            id: 2,
            conversation_id: 1,
            role: "assistant",
            content: "Hello world",
            mode_used: "auto->fast",
            notes: "n | context_messages=0",
            created_at: "2026-07-18 10:01:04",
          },
        ];
        return streamResponse(SSE_BODY);
      }
      throw new Error(`Unhandled request: ${method} ${url}`);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("loads and renders the conversation list from the API", async () => {
    render(<App />);
    expect(await screen.findByRole("heading", { name: "First chat" })).toBeInTheDocument();
  });

  it("streams an answer and shows the persisted assistant message", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByPlaceholderText(/Ask inside this saved conversation/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    // The final assistant text comes from the post-stream refetch (server truth).
    expect(await screen.findByText("Hello world")).toBeInTheDocument();
    expect(screen.getByText("auto->fast")).toBeInTheDocument();
  });

  it("attaches the bearer token to requests when one is set", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/API token/i), "secret-token");
    await user.type(screen.getByPlaceholderText(/Ask inside this saved conversation/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    await screen.findByText("Hello world");
    expect(capturedAuthHeader).toBe("Bearer secret-token");
  });
});
