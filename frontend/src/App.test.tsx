import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";

type Msg = {
  id: number;
  conversation_id: number;
  role: string;
  content: string;
  mode_used?: string | null;
  notes?: string | null;
  sources?: { title: string; url: string }[] | null;
  pending_action?: { action: string; summary: string; payload: Record<string, unknown> } | null;
  action_status?: "pending" | "confirmed" | "declined" | "failed" | null;
  images?: string[] | null;
  files?: { filename: string; data: string }[] | null;
  created_at: string;
};

const encoder = new TextEncoder();
const META_FRAME =
  'event: meta\ndata: {"request_id":"r","mode_used":"auto->fast","model":"gpt-5-mini","notes":"n"}\n\n';
const SSE_BODY =
  META_FRAME +
  'event: delta\ndata: {"text":"Hello "}\n\n' +
  'event: delta\ndata: {"text":"world"}\n\n' +
  'event: done\ndata: {"answer":"Hello world","mode_used":"auto->fast","notes":"n"}\n\n';

const SSE_BODY_REFUSED =
  META_FRAME +
  'event: done\ndata: {"answer":"","mode_used":"auto->fast","notes":"Daily budget reached. Request refused; it resets at 00:00 UTC."}\n\n';

const SSE_BODY_WITH_SOURCES =
  META_FRAME +
  'event: delta\ndata: {"text":"It\'s sunny."}\n\n' +
  'event: done\ndata: {"answer":"It\'s sunny.","mode_used":"auto->fast","notes":"n","sources":[{"title":"Weather Site","url":"https://weather.example"}]}\n\n';

const SSE_BODY_WITH_ACTION =
  META_FRAME +
  'event: delta\ndata: {"text":"I\'ll draft that."}\n\n' +
  'event: done\ndata: {"answer":"I\'ll draft that.","mode_used":"auto->fast","notes":"n","pending_action":{"action":"send_email","summary":"Email Bob the report","payload":{"to":"b"}}}\n\n';

// Persisted version deliberately WITHOUT the pending action, so a card found
// before the post-stream refetch completes can only have come from the live
// streaming render, not the persisted message.
const PERSISTED_NO_ACTION: Msg[] = [
  { id: 1, conversation_id: 1, role: "user", content: "email bob", created_at: "2026-07-18 10:01:00" },
  {
    id: 2,
    conversation_id: 1,
    role: "assistant",
    content: "I'll draft that.",
    mode_used: "auto->fast",
    notes: "n | context_messages=0",
    created_at: "2026-07-18 10:01:04",
  },
];

const SSE_BODY_WITH_IMAGE =
  META_FRAME +
  'event: delta\ndata: {"text":"Here\'s the image you asked for."}\n\n' +
  'event: done\ndata: {"answer":"Here\'s the image you asked for.","mode_used":"auto->fast","notes":"n","images":["data:image/png;base64,aaa"]}\n\n';

// Persisted version deliberately WITHOUT the image, so an <img> found before
// the post-stream refetch completes can only have come from the live render.
const PERSISTED_NO_IMAGE: Msg[] = [
  { id: 1, conversation_id: 1, role: "user", content: "draw a cat", created_at: "2026-07-18 10:01:00" },
  {
    id: 2,
    conversation_id: 1,
    role: "assistant",
    content: "Here's the image you asked for.",
    mode_used: "auto->fast",
    notes: "n | context_messages=0",
    created_at: "2026-07-18 10:01:04",
  },
];

const REGEN_SSE_BODY =
  'event: meta\ndata: {"mode_used":"forced:gpt-5","model":"gpt-5","notes":"n"}\n\n' +
  'event: delta\ndata: {"text":"Regenerated answer"}\n\n' +
  'event: done\ndata: {"answer":"Regenerated answer","mode_used":"forced:gpt-5","notes":"n"}\n\n';

const EDIT_SSE_BODY =
  'event: meta\ndata: {"mode_used":"auto->fast","model":"gpt-5-mini","notes":"n"}\n\n' +
  'event: delta\ndata: {"text":"Edited answer"}\n\n' +
  'event: done\ndata: {"answer":"Edited answer","mode_used":"auto->fast","notes":"n"}\n\n';

const PERSISTED: Msg[] = [
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

// Only the user turn persists when the answer is refused (e.g. daily budget
// cap) — no assistant reply is written.
const PERSISTED_UNANSWERED: Msg[] = [
  { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
];

// Persisted version of the sources-bearing answer deliberately WITHOUT
// sources, so a link found before the post-stream refresh completes can only
// have come from the live-streaming render, not the persisted message.
const PERSISTED_NO_SOURCES: Msg[] = [
  { id: 1, conversation_id: 1, role: "user", content: "weather", created_at: "2026-07-18 10:01:00" },
  {
    id: 2,
    conversation_id: 1,
    role: "assistant",
    content: "It's sunny.",
    mode_used: "auto->fast",
    notes: "n | context_messages=0",
    created_at: "2026-07-18 10:01:04",
  },
];

// Configurable stub state (reset each test).
let statusBody: { jwt_enabled: boolean; registration_allowed: boolean };
let streamMode: "ok" | "404" | "hang" | "sources" | "action" | "image" | "refused";
let messages: Msg[];
let capturedAuthHeader: string | null;
let capturedRegenBody: Record<string, unknown> | null;
let capturedEditBody: Record<string, unknown> | null;
let pinnedModel: string | null;
let systemPrompt: string | null;
let favoriteState: boolean;
let capturedFavoriteBody: Record<string, unknown> | null;
let archivedState: boolean;
let capturedArchiveBody: Record<string, unknown> | null;
let budgetModel: string | null;
let capturedActionBody: Record<string, unknown> | null;
let actionResponse: { action_status: string; detail?: string | null };
let capturedAskBody: Record<string, unknown> | null;
let capturedTranscribeBody: Record<string, unknown> | null;
let transcribeResponse: { text: string } | { status: number; detail: string };
let capturedSpeakBody: Record<string, unknown> | null;
let speakShouldFail: boolean;
let searchResultsResponse: {
  id: number;
  title: string;
  owner: string | null;
  pinned_model: string | null;
  created_at: string;
  updated_at: string;
  snippet: string;
}[];
let capturedSearchQuery: string | null;
let clipboardWriteText: ReturnType<typeof vi.fn>;
let capturedDeleteMessageUrl: string | null;
let deleteMessageShouldFail: boolean;
let importedConversation: {
  id: number;
  title: string;
  owner: string | null;
  pinned_model: string | null;
  system_prompt: string | null;
  created_at: string;
  updated_at: string;
} | null;
let capturedImportBody: Record<string, unknown> | null;
let importShouldFail: boolean;
let duplicatedConversation: typeof importedConversation;
let capturedDuplicateUrl: string | null;
let duplicateShouldFail: boolean;

function sseResponse(body: string): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(body));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

beforeEach(() => {
  statusBody = { jwt_enabled: false, registration_allowed: true };
  streamMode = "ok";
  messages = [];
  capturedAuthHeader = null;
  capturedRegenBody = null;
  capturedEditBody = null;
  pinnedModel = null;
  systemPrompt = null;
  favoriteState = false;
  capturedFavoriteBody = null;
  archivedState = false;
  capturedArchiveBody = null;
  budgetModel = null;
  capturedActionBody = null;
  actionResponse = { action_status: "confirmed", detail: "Webhook responded 200." };
  capturedAskBody = null;
  capturedTranscribeBody = null;
  transcribeResponse = { text: "hello from the mic" };
  capturedSpeakBody = null;
  speakShouldFail = false;
  searchResultsResponse = [];
  capturedSearchQuery = null;
  capturedDeleteMessageUrl = null;
  deleteMessageShouldFail = false;
  importedConversation = null;
  capturedImportBody = null;
  importShouldFail = false;
  duplicatedConversation = null;
  capturedDuplicateUrl = null;
  duplicateShouldFail = false;
  window.localStorage.clear();

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      const headers = new Headers(init?.headers);
      const authed = headers.get("authorization");

      if (url.endsWith("/v1/status"))
        return Response.json({
          ...statusBody,
          models: {
            router: "gpt-5-nano",
            fast: "gemini-fast",
            smart: "gpt-5",
            fallback: "gpt-5-mini",
            ...(budgetModel ? { budget: budgetModel } : {}),
          },
        });
      if (url.endsWith("/v1/settings") && method === "GET") {
        return Response.json({
          editable: true,
          tiers: [
            {
              key: "OPENAI_MODEL_SMART",
              label: "Smart tier",
              effective_model: "gpt-5",
              source: "default",
              override: null,
              env: null,
              default: "",
              provider: "openai",
              key_env: "OPENAI_API_KEY",
              key_present: true,
            },
          ],
          categories: [],
        });
      }
      if (url.endsWith("/v1/cache") && method === "GET") {
        return Response.json({ enabled: true, entries: 0, ttl_seconds: 0, max_entries: 1000 });
      }
      if (url.endsWith("/v1/auth/me")) return Response.json({ username: authed ? "alice" : null });
      if (url.endsWith("/v1/auth/register") && method === "POST") {
        return new Response(
          JSON.stringify({ id: 1, username: "alice", created_at: "2026-07-18 10:00:00" }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.endsWith("/v1/auth/login") && method === "POST") {
        return Response.json({ access_token: "jwt-token", token_type: "bearer" });
      }
      if (url.endsWith("/v1/auth/logout") && method === "POST") {
        return Response.json({ status: "logged_out" });
      }
      if (url.includes("/v1/search") && method === "GET") {
        capturedSearchQuery = new URL(url, "http://localhost").searchParams.get("q");
        return Response.json(searchResultsResponse);
      }
      if (url.includes("/v1/usage") && method === "GET") {
        return Response.json({ today_usd: 0, days: 14, by_model: [], by_day: [] });
      }
      if ((url.endsWith("/v1/conversations") || url.includes("/v1/conversations?")) && method === "GET") {
        const includeArchived = url.includes("include_archived=true");
        return Response.json([
          ...(archivedState && !includeArchived
            ? []
            : [{ id: 1, title: "First chat", owner: null, pinned_model: pinnedModel, system_prompt: systemPrompt, favorite: favoriteState, archived: archivedState, created_at: "2026-07-18 10:00:00", updated_at: "2026-07-18 10:00:00" }]),
          ...(importedConversation ? [importedConversation] : []),
          ...(duplicatedConversation ? [duplicatedConversation] : []),
        ]);
      }
      if (/\/v1\/conversations\/\d+\/favorite$/.test(url) && method === "PUT") {
        capturedFavoriteBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        favoriteState = Boolean(capturedFavoriteBody?.favorite);
        return Response.json({ id: 1, title: "First chat", owner: null, pinned_model: pinnedModel, system_prompt: systemPrompt, favorite: favoriteState, archived: archivedState, created_at: "2026-07-18 10:00:00", updated_at: "2026-07-18 10:00:00" });
      }
      if (/\/v1\/conversations\/\d+\/archive$/.test(url) && method === "PUT") {
        capturedArchiveBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        archivedState = Boolean(capturedArchiveBody?.archived);
        return Response.json({ id: 1, title: "First chat", owner: null, pinned_model: pinnedModel, system_prompt: systemPrompt, favorite: favoriteState, archived: archivedState, created_at: "2026-07-18 10:00:00", updated_at: "2026-07-18 10:00:00" });
      }
      if (/\/v1\/conversations\/\d+\/duplicate$/.test(url) && method === "POST") {
        capturedDuplicateUrl = url;
        if (duplicateShouldFail) {
          return new Response(JSON.stringify({ detail: "boom" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        messages = messages.map((message, index) => ({ ...message, id: 200 + index, conversation_id: 3 }));
        duplicatedConversation = {
          id: 3,
          title: "First chat (copy)",
          owner: null,
          pinned_model: pinnedModel,
          system_prompt: systemPrompt,
          created_at: "2026-07-19 09:00:00",
          updated_at: "2026-07-19 09:00:00",
        };
        return Response.json(duplicatedConversation);
      }
      if (url.endsWith("/v1/conversations/import") && method === "POST") {
        capturedImportBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        if (importShouldFail) {
          return new Response(JSON.stringify({ detail: "Import failed: bad data" }), {
            status: 422,
            headers: { "Content-Type": "application/json" },
          });
        }
        const importMessages = (capturedImportBody?.messages as { role: string; content: string }[]) ?? [];
        messages = importMessages.map((message, index) => ({
          id: 100 + index,
          conversation_id: 2,
          role: message.role,
          content: message.content,
          created_at: "2026-07-19 09:00:00",
        }));
        importedConversation = {
          id: 2,
          title: (capturedImportBody?.title as string) || "Imported conversation",
          owner: null,
          pinned_model: null,
          system_prompt: null,
          created_at: "2026-07-19 09:00:00",
          updated_at: "2026-07-19 09:00:00",
        };
        return Response.json(importedConversation);
      }
      if (/\/v1\/conversations\/\d+\/pin$/.test(url) && method === "PUT") {
        const body = init?.body ? (JSON.parse(String(init.body)) as { model?: string }) : {};
        pinnedModel = body.model ? body.model : null;
        return Response.json({ id: 1, title: "First chat", owner: null, pinned_model: pinnedModel, system_prompt: systemPrompt, created_at: "2026-07-18 10:00:00", updated_at: "2026-07-18 10:00:00" });
      }
      if (/\/v1\/conversations\/\d+\/system_prompt$/.test(url) && method === "PUT") {
        const body = init?.body
          ? (JSON.parse(String(init.body)) as { system_prompt?: string })
          : {};
        systemPrompt = body.system_prompt ? body.system_prompt : null;
        return Response.json({ id: 1, title: "First chat", owner: null, pinned_model: pinnedModel, system_prompt: systemPrompt, created_at: "2026-07-18 10:00:00", updated_at: "2026-07-18 10:00:00" });
      }
      if (/\/v1\/conversations\/\d+\/messages$/.test(url) && method === "GET") {
        if (streamMode === "sources" || streamMode === "action" || streamMode === "image") {
          // A small real delay so a test can observe the live-streaming render
          // (sources/pending_action arrive on the SSE "done" frame) before this
          // refetch swaps it for the persisted message list.
          await new Promise((resolve) => setTimeout(resolve, 30));
        }
        return Response.json(messages);
      }
      if (/\/v1\/conversations\/\d+\/messages\/\d+$/.test(url) && method === "DELETE") {
        capturedDeleteMessageUrl = url;
        if (deleteMessageShouldFail) {
          return new Response(JSON.stringify({ detail: "boom" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        const deletedId = Number(url.split("/").pop());
        messages = messages.filter((m) => m.id !== deletedId);
        return Response.json({ status: "deleted", message_id: deletedId });
      }
      if (/\/regenerate\/stream$/.test(url) && method === "POST") {
        capturedRegenBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        messages = [
          { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
          {
            id: 3,
            conversation_id: 1,
            role: "assistant",
            content: "Regenerated answer",
            mode_used: "forced:gpt-5",
            notes: "n | regenerated | context_messages=0",
            created_at: "2026-07-18 10:02:00",
          },
        ];
        return sseResponse(REGEN_SSE_BODY);
      }
      if (/\/messages\/\d+\/edit\/stream$/.test(url) && method === "POST") {
        capturedEditBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        messages = [
          { id: 1, conversation_id: 1, role: "user", content: "hi there, edited", created_at: "2026-07-18 10:01:00" },
          {
            id: 3,
            conversation_id: 1,
            role: "assistant",
            content: "Edited answer",
            mode_used: "auto->fast",
            notes: "n | edited | context_messages=0",
            created_at: "2026-07-18 10:02:00",
          },
        ];
        return sseResponse(EDIT_SSE_BODY);
      }
      if (/\/ask\/stream$/.test(url) && method === "POST") {
        capturedAuthHeader = authed;
        capturedAskBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        if (streamMode === "404") {
          return new Response(JSON.stringify({ detail: "Conversation not found" }), {
            status: 404,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (streamMode === "hang") {
          // Send meta then hang until the request is aborted.
          const stream = new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(encoder.encode(META_FRAME));
              init?.signal?.addEventListener("abort", () => {
                try {
                  controller.error(new DOMException("aborted", "AbortError"));
                } catch {
                  /* already closed */
                }
              });
            },
          });
          return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
        }
        if (streamMode === "sources") {
          messages = PERSISTED_NO_SOURCES;
          return sseResponse(SSE_BODY_WITH_SOURCES);
        }
        if (streamMode === "action") {
          messages = PERSISTED_NO_ACTION;
          return sseResponse(SSE_BODY_WITH_ACTION);
        }
        if (streamMode === "image") {
          messages = PERSISTED_NO_IMAGE;
          return sseResponse(SSE_BODY_WITH_IMAGE);
        }
        if (streamMode === "refused") {
          messages = PERSISTED_UNANSWERED;
          return sseResponse(SSE_BODY_REFUSED);
        }
        messages = PERSISTED;
        return sseResponse(SSE_BODY);
      }
      if (/\/messages\/\d+\/action$/.test(url) && method === "POST") {
        capturedActionBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        return Response.json(actionResponse);
      }
      if (url.endsWith("/v1/transcribe") && method === "POST") {
        capturedTranscribeBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        if ("status" in transcribeResponse) {
          return new Response(JSON.stringify({ detail: transcribeResponse.detail }), {
            status: transcribeResponse.status,
            headers: { "Content-Type": "application/json" },
          });
        }
        return Response.json(transcribeResponse);
      }
      if (url.endsWith("/v1/speak") && method === "POST") {
        capturedSpeakBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        if (speakShouldFail) {
          return new Response(JSON.stringify({ detail: "upstream boom" }), {
            status: 502,
            headers: { "Content-Type": "application/json" },
          });
        }
        // A plain byte body, not a Blob: constructing a Response directly from
        // a Blob hits a platform-specific path in Node's fetch/undici that
        // fails on Linux CI runners but not locally on Windows. res.blob() on
        // the receiving end still yields a real Blob either way.
        return new Response(new TextEncoder().encode("fake mp3 bytes"), {
          headers: { "Content-Type": "audio/mpeg" },
        });
      }
      throw new Error(`Unhandled request: ${method} ${url}`);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.documentElement.removeAttribute("data-theme");
});

describe("App", () => {
  it("loads and renders the conversation list", async () => {
    render(<App />);
    expect(await screen.findByRole("heading", { name: "First chat" })).toBeInTheDocument();
  });

  it("streams an answer and shows the persisted assistant message", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    expect(await screen.findByText("Hello world")).toBeInTheDocument();
    expect(screen.getByText("auto->fast")).toBeInTheDocument();
  });

  it("announces the completed answer in an aria-live region for screen readers", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    // "Hello world" appears in the persisted message bubble AND in the
    // hidden live-region announcement — the announcement is prefixed so it
    // doesn't collide with getByText("Hello world") matching two nodes.
    expect(await screen.findByText(/Answer received: Hello world/i)).toBeInTheDocument();
  });

  it("renders assistant markdown (bold) rather than raw text", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "this is **bold** now", created_at: "2026-07-18 10:00:00" },
    ];
    render(<App />);
    const bold = await screen.findByText("bold");
    expect(bold.tagName).toBe("STRONG");
  });

  it("copies a message's text to the clipboard", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "Hello world", created_at: "2026-07-18 10:00:00" },
    ];
    const user = userEvent.setup();
    clipboardWriteText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    render(<App />);
    await screen.findByText("Hello world");

    await user.click(screen.getByRole("button", { name: "Copy message text" }));

    expect(clipboardWriteText).toHaveBeenCalledWith("Hello world");
    expect(await screen.findByRole("button", { name: "Copied!" })).toBeInTheDocument();
  });

  it("shows a copy button on rendered code blocks and copies the code", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "```js\nconsole.log('hi')\n```",
        created_at: "2026-07-18 10:00:00",
      },
    ];
    const user = userEvent.setup();
    clipboardWriteText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    render(<App />);
    await screen.findByText(/console\.log/);

    await user.click(screen.getByRole("button", { name: "Copy code" }));

    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining("console.log('hi')"));
    expect(await screen.findByRole("button", { name: "Copied!" })).toBeInTheDocument();
  });

  it("shows a status message when copying fails", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "Hello world", created_at: "2026-07-18 10:00:00" },
    ];
    const user = userEvent.setup();
    clipboardWriteText = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockRejectedValue(new Error("denied"));
    render(<App />);
    await screen.findByText("Hello world");

    await user.click(screen.getByRole("button", { name: "Copy message text" }));

    expect(await screen.findByText(/Failed to copy to clipboard\./i)).toBeInTheDocument();
  });

  it("deletes a message after confirming", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
      { id: 2, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello!");

    await user.click(screen.getByRole("button", { name: /Delete assistant message/i }));

    await waitFor(() => {
      expect(capturedDeleteMessageUrl).toMatch(/\/v1\/conversations\/1\/messages\/2$/);
    });
    expect(screen.queryByText("hello!")).not.toBeInTheDocument();
    expect(screen.getByText("hi there")).toBeInTheDocument();
    expect(await screen.findByText(/Message deleted\./i)).toBeInTheDocument();
  });

  it("does not delete a message when confirmation is cancelled", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello!");

    await user.click(screen.getByRole("button", { name: /Delete assistant message/i }));

    expect(capturedDeleteMessageUrl).toBeNull();
    expect(screen.getByText("hello!")).toBeInTheDocument();
  });

  it("shows a status message when deleting a message fails", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    deleteMessageShouldFail = true;
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello!");

    await user.click(screen.getByRole("button", { name: /Delete assistant message/i }));

    expect(await screen.findByText(/Failed to delete message/i)).toBeInTheDocument();
    expect(screen.getByText("hello!")).toBeInTheDocument();
  });

  it("renders sources as clickable links under the assistant message", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "It's sunny.",
        sources: [
          { title: "Weather Site", url: "https://weather.example" },
          { title: "", url: "https://fallback.example" },
        ],
        created_at: "2026-07-18 10:00:00",
      },
    ];
    render(<App />);

    const link = await screen.findByRole("link", { name: "Weather Site" });
    expect(link).toHaveAttribute("href", "https://weather.example");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));

    // An empty title falls back to showing the URL itself as the link text.
    expect(screen.getByRole("link", { name: "https://fallback.example" })).toBeInTheDocument();
  });

  it("renders sources in the live streaming bubble from the done frame, before the post-stream refresh", async () => {
    // Review follow-up: sources previously only appeared after refetching
    // persisted messages, with a silent gap during the live stream itself.
    streamMode = "sources";
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "weather");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    // The persisted refetch (PERSISTED_NO_SOURCES) carries no sources, so this
    // link can only have come from streamState during the live render.
    const link = await screen.findByRole("link", { name: "Weather Site" });
    expect(link).toHaveAttribute("href", "https://weather.example");
  });

  it("shows no sources list when the assistant message has none", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "hi", created_at: "2026-07-18 10:00:00" },
    ];
    render(<App />);
    await screen.findByText("hi");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("renders a pending action card with Confirm/Decline buttons", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "I've drafted the email.",
        pending_action: { action: "send_email", summary: "Email Bob the report", payload: { to: "b" } },
        action_status: "pending",
        created_at: "2026-07-18 10:00:00",
      },
    ];
    render(<App />);
    expect(await screen.findByText("Email Bob the report")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Decline" })).toBeInTheDocument();
  });

  it("shows the actual webhook payload, not just the summary, before confirming", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "I've drafted the email.",
        pending_action: {
          action: "send_email",
          summary: "Email Bob the report",
          payload: { to: "b", amount: 500 },
        },
        action_status: "pending",
        created_at: "2026-07-18 10:00:00",
      },
    ];
    render(<App />);
    await screen.findByText("Email Bob the report");
    expect(screen.getByText(/"to": "b"/)).toBeInTheDocument();
    expect(screen.getByText(/"amount": 500/)).toBeInTheDocument();
  });

  it("confirms a pending action and shows the resolved status", async () => {
    actionResponse = { action_status: "confirmed", detail: "Webhook responded 200." };
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "I've drafted the email.",
        pending_action: { action: "send_email", summary: "Email Bob the report", payload: { to: "b" } },
        action_status: "pending",
        created_at: "2026-07-18 10:00:00",
      },
    ];
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Confirm" }));

    expect(await screen.findByText("✓ Confirmed")).toBeInTheDocument();
    expect(capturedActionBody).toEqual({ confirm: true });
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });

  it("declines a pending action without confirming", async () => {
    actionResponse = { action_status: "declined" };
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "I've drafted the email.",
        pending_action: { action: "send_email", summary: "Email Bob the report", payload: { to: "b" } },
        action_status: "pending",
        created_at: "2026-07-18 10:00:00",
      },
    ];
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Decline" }));

    expect(await screen.findByText("Declined")).toBeInTheDocument();
    expect(capturedActionBody).toEqual({ confirm: false });
  });

  it("shows a pending action in the live streaming bubble from the done frame, before the post-stream refresh", async () => {
    streamMode = "action";
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "email bob");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    // The persisted refetch (PERSISTED_NO_ACTION) carries no pending_action, so
    // this can only have come from streamState during the live render.
    expect(await screen.findByText("Email Bob the report")).toBeInTheDocument();
    expect(screen.getByText("Confirm below once sent")).toBeInTheDocument();
  });

  it("renders a generated image under the assistant message", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "Here's the image you asked for.",
        images: ["data:image/png;base64,aaa"],
        created_at: "2026-07-18 10:00:00",
      },
    ];
    render(<App />);
    const img = await screen.findByRole("img", { name: "Generated" });
    expect(img).toHaveAttribute("src", "data:image/png;base64,aaa");
  });

  it("shows a generated image in the live streaming bubble from the done frame, before the post-stream refresh", async () => {
    streamMode = "image";
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "draw a cat");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    // The persisted refetch (PERSISTED_NO_IMAGE) carries no images, so this can
    // only have come from streamState during the live render.
    const img = await screen.findByRole("img", { name: "Generated" });
    expect(img).toHaveAttribute("src", "data:image/png;base64,aaa");
  });

  it("renders a user-attached image under the user message", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "user",
        content: "what is this",
        images: ["data:image/png;base64,aaa"],
        created_at: "2026-07-18 10:00:00",
      },
    ];
    render(<App />);
    const img = await screen.findByRole("img", { name: "Attached" });
    expect(img).toHaveAttribute("src", "data:image/png;base64,aaa");
  });

  it("attaches an image, previews it, and sends it with the question", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const file = new File(["fake-bytes"], "cat.png", { type: "image/png" });
    const fileInput = screen.getByLabelText(/Attach image/i) as HTMLInputElement;
    await user.upload(fileInput, file);

    // A thumbnail preview appears before sending.
    await screen.findByAltText("Attachment 1");

    await user.type(screen.getByLabelText(/Ask a question/i), "what is this");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    await screen.findByText("Hello world");
    expect(capturedAskBody?.images).toEqual([expect.stringMatching(/^data:image\/png;base64,/)]);

    // The composer's preview clears after sending.
    expect(screen.queryByAltText("Attachment 1")).not.toBeInTheDocument();
  });

  it("removes an attached image from the preview before sending", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const file = new File(["fake-bytes"], "cat.png", { type: "image/png" });
    const fileInput = screen.getByLabelText(/Attach image/i) as HTMLInputElement;
    await user.upload(fileInput, file);
    await screen.findByAltText("Attachment 1");

    await user.click(screen.getByRole("button", { name: /Remove attachment 1/i }));
    expect(screen.queryByAltText("Attachment 1")).not.toBeInTheDocument();

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));
    await screen.findByText("Hello world");
    expect(capturedAskBody?.images).toBeUndefined();
  });

  it("shows the attached image in the live streaming user bubble", async () => {
    // Use a streamMode with a delayed persisted refetch (see the sources test
    // above) so the live bubble is observable before it's swapped out.
    streamMode = "sources";
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const file = new File(["fake-bytes"], "cat.png", { type: "image/png" });
    const fileInput = screen.getByLabelText(/Attach image/i) as HTMLInputElement;
    await user.upload(fileInput, file);
    await screen.findByAltText("Attachment 1");

    await user.type(screen.getByLabelText(/Ask a question/i), "what is this");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    // While streaming, the live user bubble shows the attached image too.
    const img = await screen.findByRole("img", { name: "Attached" });
    expect(img).toHaveAttribute("src", expect.stringMatching(/^data:image\/png;base64,/));
  });

  it("renders an attached document under the user message", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "user",
        content: "summarize this",
        files: [{ filename: "report.pdf", data: "data:application/pdf;base64,aaa" }],
        created_at: "2026-07-18 10:00:00",
      },
    ];
    render(<App />);
    await screen.findByText("📄 report.pdf");
  });

  it("attaches a PDF, previews it as a chip, and sends it with the question", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const file = new File(["%PDF-1.4 fake"], "report.pdf", { type: "application/pdf" });
    const fileInput = screen.getByLabelText(/Attach image or document/i) as HTMLInputElement;
    await user.upload(fileInput, file);

    // A filename chip preview appears before sending.
    await screen.findByText("📄 report.pdf");

    await user.type(screen.getByLabelText(/Ask a question/i), "summarize this");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    await screen.findByText("Hello world");
    expect(capturedAskBody?.files).toEqual([
      { filename: "report.pdf", data: expect.stringMatching(/^data:application\/pdf;base64,/) },
    ]);

    // The composer's preview clears after sending.
    expect(screen.queryByText("📄 report.pdf")).not.toBeInTheDocument();
  });

  it("removes an attached file from the preview before sending", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const file = new File(["%PDF-1.4 fake"], "report.pdf", { type: "application/pdf" });
    const fileInput = screen.getByLabelText(/Attach image or document/i) as HTMLInputElement;
    await user.upload(fileInput, file);
    await screen.findByText("📄 report.pdf");

    await user.click(screen.getByRole("button", { name: /Remove attachment report.pdf/i }));
    expect(screen.queryByText("📄 report.pdf")).not.toBeInTheDocument();

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));
    await screen.findByText("Hello world");
    expect(capturedAskBody?.files).toBeUndefined();
  });

  it("shows the attached file in the live streaming user bubble", async () => {
    streamMode = "sources";
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const file = new File(["%PDF-1.4 fake"], "report.pdf", { type: "application/pdf" });
    const fileInput = screen.getByLabelText(/Attach image or document/i) as HTMLInputElement;
    await user.upload(fileInput, file);
    await screen.findByText("📄 report.pdf");

    await user.type(screen.getByLabelText(/Ask a question/i), "summarize this");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    // While streaming, the live user bubble shows the attached file chip too.
    const chips = await screen.findAllByText("📄 report.pdf");
    expect(chips.length).toBeGreaterThan(0);
  });

  it("shows an unsupported-browser message when there is no microphone API", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: /Record a voice question/i }));
    await screen.findByText(/isn't supported in this browser/i);
  });

  it("records, stops, transcribes, and inserts the text into the question box", async () => {
    class FakeMediaRecorder {
      static isTypeSupported = () => true;
      mimeType: string;
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(_stream: unknown, options?: { mimeType?: string }) {
        this.mimeType = options?.mimeType ?? "audio/webm";
      }
      start() {}
      stop() {
        this.ondataavailable?.({ data: new Blob(["fake audio"], { type: this.mimeType }) });
        this.onstop?.();
      }
    }

    const originalMediaDevices = navigator.mediaDevices;
    const fakeStream = { getTracks: () => [{ stop: () => {} }] } as unknown as MediaStream;
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: vi.fn().mockResolvedValue(fakeStream) },
      configurable: true,
    });
    vi.stubGlobal("MediaRecorder", FakeMediaRecorder);

    try {
      const user = userEvent.setup();
      render(<App />);
      await screen.findByRole("heading", { name: "First chat" });

      await user.click(screen.getByRole("button", { name: /Record a voice question/i }));
      await screen.findByRole("button", { name: /Stop recording/i });

      await user.click(screen.getByRole("button", { name: /Stop recording/i }));

      const textarea = (await screen.findByLabelText(/Ask a question/i)) as HTMLTextAreaElement;
      await waitFor(() => expect(textarea.value).toBe("hello from the mic"));

      expect(capturedTranscribeBody?.audio).toEqual(
        expect.stringMatching(/^data:audio\/webm;base64,/),
      );
    } finally {
      Object.defineProperty(navigator, "mediaDevices", {
        value: originalMediaDevices,
        configurable: true,
      });
    }
  });

  it("speaks and stops an assistant message via the speaker button", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "Hello world",
        created_at: "2026-07-18 10:00:00",
      },
    ];

    class FakeAudio {
      onended: (() => void) | null = null;
      play = vi.fn().mockResolvedValue(undefined);
      pause = vi.fn();
    }
    vi.stubGlobal("Audio", FakeAudio);
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    URL.createObjectURL = vi.fn(() => "blob:fake-url");
    URL.revokeObjectURL = vi.fn();

    try {
      const user = userEvent.setup();
      render(<App />);

      const speakButton = await screen.findByRole("button", { name: /Read this answer aloud/i });
      await user.click(speakButton);

      await screen.findByRole("button", { name: /Stop speaking/i });
      expect(capturedSpeakBody).toEqual({ text: "Hello world" });

      await user.click(screen.getByRole("button", { name: /Stop speaking/i }));
      await screen.findByRole("button", { name: /Read this answer aloud/i });
    } finally {
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
    }
  });

  it("shows a status message when speech synthesis fails", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "Hello world",
        created_at: "2026-07-18 10:00:00",
      },
    ];
    speakShouldFail = true;

    const user = userEvent.setup();
    render(<App />);
    const speakButton = await screen.findByRole("button", { name: /Read this answer aloud/i });
    await user.click(speakButton);

    await screen.findByText(/upstream boom/i);
  });

  it("attaches the bearer token when one is set", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem("ai_workbench_token", "static-tok");
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));
    await screen.findByText("Hello world");
    expect(capturedAuthHeader).toBe("Bearer static-tok");
  });

  it("shows a login form and signs in / out when JWT is enabled", async () => {
    statusBody = { jwt_enabled: true, registration_allowed: true };
    const user = userEvent.setup();
    render(<App />);

    await user.type(await screen.findByLabelText(/Username/i), "alice");
    await user.type(screen.getByLabelText(/Password/i), "password123");
    await user.click(screen.getByRole("button", { name: /^Log in$/i }));

    expect(await screen.findByText("alice")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Log out/i }));
    expect(await screen.findByRole("button", { name: /^Log in$/i })).toBeInTheDocument();
  });

  it("hides the Register button when registration is disabled", async () => {
    statusBody = { jwt_enabled: true, registration_allowed: false };
    render(<App />);
    expect(await screen.findByRole("button", { name: /^Log in$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Register$/i })).toBeNull();
  });

  it("opens the settings modal from the header", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: /^Settings$/i }));

    expect(await screen.findByRole("dialog", { name: /Model settings/i })).toBeInTheDocument();
    expect(await screen.findByText("Smart tier")).toBeInTheDocument();
  });

  it("pins a model to the conversation and disables the mode dropdown", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const modeSelect = screen.getByLabelText(/Routing mode/i);
    expect(modeSelect).toBeEnabled();

    await user.selectOptions(screen.getByLabelText(/Pinned model/i), "gpt-5");

    // The pin persisted (reload reflects it) and the mode dropdown is now locked.
    await screen.findByText(/Pinned this conversation to gpt-5/i);
    expect(screen.getByLabelText(/Routing mode/i)).toBeDisabled();
    expect((screen.getByLabelText(/Pinned model/i) as HTMLSelectElement).value).toBe("gpt-5");
  });

  it("pinning a conversation also disables the regenerate-with dropdown", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
      { id: 2, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello!");

    const regenSelect = screen.getByLabelText(/Regenerate with/i);
    expect(regenSelect).toBeEnabled();

    await user.selectOptions(screen.getByLabelText(/Pinned model/i), "gpt-5");

    await screen.findByText(/Pinned this conversation to gpt-5/i);
    expect(screen.getByLabelText(/Regenerate with/i)).toBeDisabled();
  });

  it("hides the budget tier from mode/pin/regenerate options when it isn't configured", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(
      within(screen.getByLabelText(/Routing mode/i)).queryByRole("option", { name: "budget" }),
    ).not.toBeInTheDocument();
    expect(
      within(screen.getByLabelText(/Pinned model/i)).queryByRole("option", { name: /budget tier/i }),
    ).not.toBeInTheDocument();
  });

  it("offers the budget tier in mode, pin, and regenerate selectors when the server has it configured", async () => {
    budgetModel = "groq/llama-3.3-70b-versatile";
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
      {
        id: 2,
        conversation_id: 1,
        role: "assistant",
        content: "old answer",
        mode_used: "auto->fast",
        created_at: "2026-07-18 10:01:04",
      },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("old answer");

    const modeSelect = screen.getByLabelText(/Routing mode/i);
    expect(within(modeSelect).getByRole("option", { name: "budget" })).toBeInTheDocument();
    await user.selectOptions(modeSelect, "budget");
    expect((modeSelect as HTMLSelectElement).value).toBe("budget");

    expect(
      within(screen.getByLabelText(/Pinned model/i)).getByRole("option", { name: /budget tier/i }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByLabelText(/Regenerate with/i)).getByRole("option", { name: /budget tier/i }),
    ).toBeInTheDocument();
  });

  it("regenerates the last answer with a forced model", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
      {
        id: 2,
        conversation_id: 1,
        role: "assistant",
        content: "old answer",
        mode_used: "auto->fast",
        created_at: "2026-07-18 10:01:04",
      },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("old answer");

    // Pick a specific model, then regenerate.
    await user.selectOptions(screen.getByLabelText(/Regenerate with/i), "model:gpt-5");
    await user.click(screen.getByRole("button", { name: /Regenerate/i }));

    expect(await screen.findByText("Regenerated answer")).toBeInTheDocument();
    expect(capturedRegenBody).toEqual({ model: "gpt-5", mode: "auto" });
  });

  it("edits a user message and resends it", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
      {
        id: 2,
        conversation_id: 1,
        role: "assistant",
        content: "old answer",
        mode_used: "auto->fast",
        created_at: "2026-07-18 10:01:04",
      },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("old answer");

    await user.click(screen.getByRole("button", { name: /Edit message from/i }));
    const editBox = screen.getByLabelText(/Edit question/i);
    expect(editBox).toHaveValue("hi there");

    await user.clear(editBox);
    await user.type(editBox, "hi there, edited");
    await user.click(screen.getByRole("button", { name: /Save & resend/i }));

    expect(await screen.findByText("Edited answer")).toBeInTheDocument();
    expect(screen.getByText("hi there, edited")).toBeInTheDocument();
    expect(screen.queryByText("old answer")).not.toBeInTheDocument();
    expect(capturedEditBody).toEqual({ question: "hi there, edited", mode: "auto" });
  });

  it("cancels an edit without sending anything", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
      {
        id: 2,
        conversation_id: 1,
        role: "assistant",
        content: "old answer",
        mode_used: "auto->fast",
        created_at: "2026-07-18 10:01:04",
      },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("old answer");

    await user.click(screen.getByRole("button", { name: /Edit message from/i }));
    const editBox = screen.getByLabelText(/Edit question/i);
    await user.clear(editBox);
    await user.type(editBox, "this should not be sent");

    await user.click(screen.getByRole("button", { name: /^Cancel$/i }));

    expect(screen.queryByLabelText(/Edit question/i)).not.toBeInTheDocument();
    expect(screen.getByText("hi there")).toBeInTheDocument();
    expect(screen.getByText("old answer")).toBeInTheDocument();
    expect(capturedEditBody).toBeNull();
  });

  it("exports a conversation as markdown", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
      {
        id: 2,
        conversation_id: 1,
        role: "assistant",
        content: "hello!",
        mode_used: "auto->fast",
        created_at: "2026-07-18 10:01:04",
      },
    ];

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
      render(<App />);
      await screen.findByText("hello!");

      await user.selectOptions(screen.getByLabelText(/Export conversation/i), "markdown");

      expect(clickSpy).toHaveBeenCalled();
      expect(capturedBlob).not.toBeNull();
      expect(capturedBlob?.type).toBe("text/markdown");
      const text = await capturedBlob?.text();
      expect(text).toContain("# First chat");
      expect(text).toContain("hi there");
      expect(text).toContain("hello!");
    } finally {
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
      clickSpy.mockRestore();
    }
  });

  it("exports a conversation as json", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
      {
        id: 2,
        conversation_id: 1,
        role: "assistant",
        content: "hello!",
        mode_used: "auto->fast",
        created_at: "2026-07-18 10:01:04",
      },
    ];

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
      render(<App />);
      await screen.findByText("hello!");

      await user.selectOptions(screen.getByLabelText(/Export conversation/i), "json");

      expect(capturedBlob).not.toBeNull();
      expect(capturedBlob?.type).toBe("application/json");
      const text = await capturedBlob?.text();
      const parsed = JSON.parse(text ?? "{}") as {
        conversation: { title: string };
        messages: { content: string }[];
      };
      expect(parsed.conversation.title).toBe("First chat");
      expect(parsed.messages.map((m) => m.content)).toEqual(["hi there", "hello!"]);
    } finally {
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
      clickSpy.mockRestore();
    }
  });

  it("sets custom instructions for a conversation", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(screen.getByRole("button", { name: "Instructions" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Instructions" }));
    const textarea = screen.getByLabelText(/Custom instructions for this conversation/i);
    await user.type(textarea, "Always answer in French.");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.queryByLabelText(/Custom instructions for this conversation/i)).not.toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "Instructions ●" })).toBeInTheDocument();
  });

  it("cancels editing instructions without saving", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Instructions" }));
    await user.type(
      screen.getByLabelText(/Custom instructions for this conversation/i),
      "discarded text",
    );
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByLabelText(/Custom instructions for this conversation/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Instructions" })).toBeInTheDocument();
  });

  it("preloads the instructions draft with any already-saved text", async () => {
    systemPrompt = "Be extremely terse.";
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(screen.getByRole("button", { name: "Instructions ●" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Instructions ●" }));

    expect(screen.getByLabelText(/Custom instructions for this conversation/i)).toHaveValue(
      "Be extremely terse.",
    );
  });

  it("duplicates the current conversation and selects the copy", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hi there");

    await user.click(screen.getByRole("button", { name: "Duplicate" }));

    await screen.findByRole("heading", { name: "First chat (copy)" });
    expect(capturedDuplicateUrl).toMatch(/\/v1\/conversations\/1\/duplicate$/);
    expect(screen.getByText("hi there")).toBeInTheDocument();
    expect(await screen.findByText(/Duplicated as "First chat \(copy\)"\./i)).toBeInTheDocument();
  });

  it("jumps to the latest message when switching conversations, even if scrolled away from the tail", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
    ];
    const user = userEvent.setup();
    const { container } = render(<App />);
    await screen.findByText("hi there");

    // Get a second sidebar entry to switch back and forth between.
    await user.click(screen.getByRole("button", { name: "Duplicate" }));
    await screen.findByRole("heading", { name: "First chat (copy)" });

    const messagesPane = container.querySelector(".messages");
    if (!messagesPane) throw new Error("messages pane not found");
    // jsdom performs no real layout (scrollHeight/clientHeight always read 0),
    // so fake a "scrolled well away from the bottom" state directly on this
    // node — that's the exact state that used to leave the pane stuck on an
    // old message after switching conversations.
    Object.defineProperty(messagesPane, "scrollHeight", { configurable: true, value: 2000 });
    Object.defineProperty(messagesPane, "clientHeight", { configurable: true, value: 500 });
    Object.defineProperty(messagesPane, "scrollTop", { configurable: true, value: 0 });
    const scrollSpy = vi
      .spyOn(window.HTMLElement.prototype, "scrollIntoView")
      .mockImplementation(() => {});

    const firstChatButton = screen.getByText("First chat", { selector: "span" }).closest("button");
    if (!firstChatButton) throw new Error("First chat sidebar button not found");
    await user.click(firstChatButton);
    await screen.findByRole("heading", { name: "First chat" });

    expect(scrollSpy).toHaveBeenCalled();
    scrollSpy.mockRestore();
  });

  it("scopes the Ask/Stop button and busy state to the conversation actually streaming", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hi there");

    // Get a second conversation while nothing is streaming yet.
    await user.click(screen.getByRole("button", { name: "Duplicate" }));
    await screen.findByRole("heading", { name: "First chat (copy)" });

    const firstChatButton = screen.getByText("First chat", { selector: "span" }).closest("button");
    const copyButton = screen.getByText("First chat (copy)", { selector: "span" }).closest("button");
    if (!firstChatButton || !copyButton) throw new Error("sidebar buttons not found");

    // Switch back and start a long-running stream in the first conversation.
    await user.click(firstChatButton);
    await screen.findByRole("heading", { name: "First chat" });
    streamMode = "hang";
    await user.type(screen.getByLabelText(/Ask a question/i), "long question");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));
    await screen.findByRole("button", { name: /^Stop$/i });

    // Switch to the second (idle) conversation — its composer must show Ask,
    // not the first conversation's Stop button.
    await user.click(copyButton);
    await screen.findByRole("heading", { name: "First chat (copy)" });
    expect(screen.getByRole("button", { name: /^Ask$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Stop$/i })).not.toBeInTheDocument();

    // Asking here must not silently no-op — a single shared stream slot
    // backs the whole app, so this should explain why rather than pretend
    // the click did nothing.
    await user.type(screen.getByLabelText(/Ask a question/i), "another question");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));
    expect(
      await screen.findByText(/Another conversation is still answering/i),
    ).toBeInTheDocument();

    // The first conversation's stream must be untouched by any of this.
    await user.click(firstChatButton);
    await screen.findByRole("heading", { name: "First chat" });
    expect(screen.getByRole("button", { name: /^Stop$/i })).toBeInTheDocument();
  });

  it("shows a status message when duplicating fails", async () => {
    duplicateShouldFail = true;
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Duplicate" }));

    expect(await screen.findByText(/Failed to duplicate conversation/i)).toBeInTheDocument();
  });

  it("opens the usage panel from the header button", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Usage" }));

    expect(await screen.findByRole("dialog", { name: "Usage" })).toBeInTheDocument();
  });

  it("opens the compare panel from the header button", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Compare" }));

    expect(await screen.findByRole("dialog", { name: "Compare models" })).toBeInTheDocument();
  });

  it("disables export when the conversation has no messages", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    expect(screen.getByLabelText(/Export conversation/i)).toBeDisabled();
  });

  it("imports a conversation from an exported JSON file", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const exportJson = JSON.stringify({
      conversation: { id: 9, title: "Trip to Japan" },
      messages: [
        { id: 1, role: "user", content: "any good ramen spots?" },
        { id: 2, role: "assistant", content: "Try Ichiran.", mode_used: "auto->fast" },
      ],
    });
    const file = new File([exportJson], "trip-to-japan.json", { type: "application/json" });
    const input = screen.getByLabelText(/Import a conversation from a JSON file/i);
    await user.upload(input, file);

    await screen.findByRole("heading", { name: "Trip to Japan" });
    expect(capturedImportBody?.title).toBe("Trip to Japan");
    expect(capturedImportBody?.messages).toEqual([
      {
        role: "user",
        content: "any good ramen spots?",
        mode_used: null,
        notes: null,
        input_tokens: null,
        output_tokens: null,
        cost_usd: null,
        cached: false,
        sources: null,
      },
      {
        role: "assistant",
        content: "Try Ichiran.",
        mode_used: "auto->fast",
        notes: null,
        input_tokens: null,
        output_tokens: null,
        cost_usd: null,
        cached: false,
        sources: null,
      },
    ]);
    expect(screen.getByText("any good ramen spots?")).toBeInTheDocument();
    expect(screen.getByText("Try Ichiran.")).toBeInTheDocument();
    expect(await screen.findByText(/Imported "Trip to Japan"\./i)).toBeInTheDocument();
  });

  it("forwards pin, instructions, tokens, cost, cached, and sources from the export file", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const exportJson = JSON.stringify({
      conversation: {
        id: 9,
        title: "Trip to Japan",
        pinned_model: "claude-sonnet-5",
        system_prompt: "Be terse.",
      },
      messages: [
        {
          id: 2,
          role: "assistant",
          content: "Try Ichiran.",
          mode_used: "auto->fast",
          input_tokens: 120,
          output_tokens: 45,
          cost_usd: 0.0031,
          cached: true,
          sources: [{ title: "Ichiran", url: "https://example.com/ichiran" }],
        },
      ],
    });
    const file = new File([exportJson], "trip-to-japan.json", { type: "application/json" });
    const input = screen.getByLabelText(/Import a conversation from a JSON file/i);
    await user.upload(input, file);

    await screen.findByRole("heading", { name: "Trip to Japan" });
    expect(capturedImportBody?.pinned_model).toBe("claude-sonnet-5");
    expect(capturedImportBody?.system_prompt).toBe("Be terse.");
    const forwardedMessages = capturedImportBody?.messages as Record<string, unknown>[];
    expect(forwardedMessages[0]).toMatchObject({
      input_tokens: 120,
      output_tokens: 45,
      cost_usd: 0.0031,
      cached: true,
      sources: [{ title: "Ichiran", url: "https://example.com/ichiran" }],
    });
  });

  it("shows an error for a file that isn't an exported conversation", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const file = new File(["{}"], "not-an-export.json", { type: "application/json" });
    const input = screen.getByLabelText(/Import a conversation from a JSON file/i);
    await user.upload(input, file);

    expect(
      await screen.findByText(/doesn't look like an exported conversation/i),
    ).toBeInTheDocument();
    expect(capturedImportBody).toBeNull();
  });

  it("shows a status message when the import request fails", async () => {
    importShouldFail = true;
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const exportJson = JSON.stringify({ messages: [{ role: "user", content: "hi" }] });
    const file = new File([exportJson], "export.json", { type: "application/json" });
    const input = screen.getByLabelText(/Import a conversation from a JSON file/i);
    await user.upload(input, file);

    expect(await screen.findByText(/Import failed: bad data/i)).toBeInTheDocument();
  });

  it("searches conversations and shows a matching result with its snippet", async () => {
    searchResultsResponse = [
      {
        id: 1,
        title: "First chat",
        owner: null,
        pinned_model: null,
        created_at: "2026-07-18 10:00:00",
        updated_at: "2026-07-18 10:00:00",
        snippet: "...volcanoes in Iceland...",
      },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Search conversations/i), "volcano");

    expect(await screen.findByText("...volcanoes in Iceland...")).toBeInTheDocument();
    expect(capturedSearchQuery).toBe("volcano");
  });

  it("shows a no-matches message for a query with no hits", async () => {
    searchResultsResponse = [];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Search conversations/i), "nothing matches this");

    expect(await screen.findByText(/No matches\./i)).toBeInTheDocument();
  });

  it("selecting a search result clears the query and shows the conversation list again", async () => {
    searchResultsResponse = [
      {
        id: 1,
        title: "First chat",
        owner: null,
        pinned_model: null,
        created_at: "2026-07-18 10:00:00",
        updated_at: "2026-07-18 10:00:00",
        snippet: "...volcanoes in Iceland...",
      },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const searchBox = screen.getByLabelText(/Search conversations/i);
    await user.type(searchBox, "volcano");
    const result = await screen.findByText("...volcanoes in Iceland...");

    await user.click(result);

    expect(searchBox).toHaveValue("");
    expect(screen.queryByText("...volcanoes in Iceland...")).not.toBeInTheDocument();
    expect(screen.getByText("#1")).toBeInTheDocument();
  });

  it("Ctrl+K focuses the search input", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });

    expect(screen.getByLabelText(/Search conversations/i)).toHaveFocus();
  });

  it("Cmd+K (metaKey) also focuses the search input", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    fireEvent.keyDown(window, { key: "k", metaKey: true });

    expect(screen.getByLabelText(/Search conversations/i)).toHaveFocus();
  });

  it("Escape clears an active search query", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const searchBox = screen.getByLabelText(/Search conversations/i);
    await user.type(searchBox, "volcano");
    expect(searchBox).toHaveValue("volcano");

    fireEvent.keyDown(window, { key: "Escape" });

    expect(searchBox).toHaveValue("");
  });

  it("Escape cancels an in-progress edit without sending anything", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hi there");

    await user.click(screen.getByRole("button", { name: /Edit message from/i }));
    await user.type(screen.getByLabelText(/Edit question/i), " more text");

    fireEvent.keyDown(window, { key: "Escape" });

    expect(screen.queryByLabelText(/Edit question/i)).not.toBeInTheDocument();
    expect(capturedEditBody).toBeNull();
  });

  it("Escape closes the instructions panel without saving", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Instructions" }));
    await user.type(
      screen.getByLabelText(/Custom instructions for this conversation/i),
      "discarded",
    );

    fireEvent.keyDown(window, { key: "Escape" });

    expect(screen.queryByLabelText(/Custom instructions for this conversation/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Instructions" })).toBeInTheDocument();
  });

  it("surfaces a 404 error and restores the question", async () => {
    streamMode = "404";
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const box = screen.getByLabelText(/Ask a question/i);
    await user.type(box, "will fail");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    const errorStatus = await screen.findByText(/Conversation not found/i);
    expect(errorStatus).toBeInTheDocument();
    expect(errorStatus).toHaveClass("chat-status-error");
    expect(box).toHaveValue("will fail");
  });

  it("does not mark a successful answer's status as an error", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "say hi");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    const routineStatus = await screen.findByText(/auto->fast \| n/);
    expect(routineStatus).toBeInTheDocument();
    expect(routineStatus).not.toHaveClass("chat-status-error");
  });

  it("stops a stream on Stop and restores the question", async () => {
    streamMode = "hang";
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const box = screen.getByLabelText(/Ask a question/i);
    await user.type(box, "please stop");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    await user.click(await screen.findByRole("button", { name: /^Stop$/i }));

    expect(await screen.findByText(/Stopped\./i)).toBeInTheDocument();
    expect(box).toHaveValue("please stop");
  });

  it("shows an inline notice under a question that got no answer (e.g. budget refusal)", async () => {
    streamMode = "refused";
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent(/didn't get an answer/i);
    expect(notice).toHaveTextContent(/Daily budget reached/i);
  });

  it("favorites a conversation from the sidebar and reflects the starred state", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const starButton = screen.getByRole("button", { name: 'Favorite "First chat"' });
    expect(starButton).toHaveAttribute("aria-pressed", "false");
    expect(starButton).toHaveTextContent("☆");

    await user.click(starButton);

    expect(capturedFavoriteBody).toEqual({ favorite: true });
    const unfavoriteButton = await screen.findByRole("button", {
      name: 'Unfavorite "First chat"',
    });
    expect(unfavoriteButton).toHaveAttribute("aria-pressed", "true");
    expect(unfavoriteButton).toHaveTextContent("★");
  });

  it("unfavorites a starred conversation back to unstarred", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: 'Favorite "First chat"' }));
    await screen.findByRole("button", { name: 'Unfavorite "First chat"' });

    await user.click(screen.getByRole("button", { name: 'Unfavorite "First chat"' }));

    expect(capturedFavoriteBody).toEqual({ favorite: false });
    const favoriteButton = await screen.findByRole("button", {
      name: 'Favorite "First chat"',
    });
    expect(favoriteButton).toHaveAttribute("aria-pressed", "false");
  });

  it("defaults to the system theme with no data-theme attribute set", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(document.documentElement).not.toHaveAttribute("data-theme");
    expect(screen.getByRole("button", { name: /Theme: 🖥️ System/ })).toBeInTheDocument();
    expect(window.localStorage.getItem("ai_workbench_theme")).toBeNull();
  });

  it("cycles system -> light -> dark -> system on repeated clicks, persisting each choice", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const findToggle = () => screen.getByRole("button", { name: /^Theme:/ });

    await user.click(findToggle());
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    expect(window.localStorage.getItem("ai_workbench_theme")).toBe("light");
    expect(screen.getByRole("button", { name: /Theme: ☀️ Light/ })).toBeInTheDocument();

    await user.click(findToggle());
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(window.localStorage.getItem("ai_workbench_theme")).toBe("dark");
    expect(screen.getByRole("button", { name: /Theme: 🌙 Dark/ })).toBeInTheDocument();

    await user.click(findToggle());
    expect(document.documentElement).not.toHaveAttribute("data-theme");
    expect(window.localStorage.getItem("ai_workbench_theme")).toBeNull();
    expect(screen.getByRole("button", { name: /Theme: 🖥️ System/ })).toBeInTheDocument();
  });

  it("restores a previously saved theme preference on load", async () => {
    window.localStorage.setItem("ai_workbench_theme", "dark");
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(screen.getByRole("button", { name: /Theme: 🌙 Dark/ })).toBeInTheDocument();
  });

  it("ignores a corrupted stored theme value and falls back to system", async () => {
    window.localStorage.setItem("ai_workbench_theme", "purple");
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(document.documentElement).not.toHaveAttribute("data-theme");
    expect(screen.getByRole("button", { name: /Theme: 🖥️ System/ })).toBeInTheDocument();
  });

  it("clicking the favorite star doesn't also select the conversation", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    // Selecting is a no-op here (it's already selected) — this guards against
    // a future second conversation where a bubbled click would wrongly switch
    // the active conversation just from starring a different one.
    await user.click(screen.getByRole("button", { name: 'Favorite "First chat"' }));

    expect(await screen.findByRole("heading", { name: "First chat" })).toBeInTheDocument();
  });

  it("archives the selected conversation, hiding it and clearing the selection", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: /^Archive$/ }));

    expect(capturedArchiveBody).toEqual({ archived: true });
    expect(await screen.findByRole("heading", { name: "No conversation selected" })).toBeInTheDocument();
    expect(screen.queryByText("First chat")).not.toBeInTheDocument();
  });

  it("reveals an archived conversation via Show archived, tagged and selectable", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: /^Archive$/ }));
    await screen.findByRole("heading", { name: "No conversation selected" });

    await user.click(screen.getByRole("checkbox", { name: "Show archived" }));

    expect(await screen.findByText("(archived)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^First chat/ })).toBeInTheDocument();
  });

  it("unarchiving a conversation restores it to the default list", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: /^Archive$/ }));
    await user.click(screen.getByRole("checkbox", { name: "Show archived" }));
    await screen.findByText("(archived)");

    await user.click(screen.getByRole("button", { name: /^First chat/ }));
    await user.click(screen.getByRole("button", { name: /^Unarchive$/ }));

    expect(capturedArchiveBody).toEqual({ archived: false });
    await user.click(screen.getByRole("checkbox", { name: "Show archived" }));
    expect(await screen.findByRole("heading", { name: "First chat" })).toBeInTheDocument();
    expect(screen.queryByText("(archived)")).not.toBeInTheDocument();
  });
});
