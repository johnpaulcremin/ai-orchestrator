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

const encoder = new TextEncoder();
const META_FRAME =
  'event: meta\ndata: {"request_id":"r","mode_used":"auto->fast","model":"gpt-5-mini","notes":"n"}\n\n';
const SSE_BODY =
  META_FRAME +
  'event: delta\ndata: {"text":"Hello "}\n\n' +
  'event: delta\ndata: {"text":"world"}\n\n' +
  'event: done\ndata: {"answer":"Hello world","mode_used":"auto->fast","notes":"n"}\n\n';

const REGEN_SSE_BODY =
  'event: meta\ndata: {"mode_used":"forced:gpt-5","model":"gpt-5","notes":"n"}\n\n' +
  'event: delta\ndata: {"text":"Regenerated answer"}\n\n' +
  'event: done\ndata: {"answer":"Regenerated answer","mode_used":"forced:gpt-5","notes":"n"}\n\n';

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

// Configurable stub state (reset each test).
let statusBody: { jwt_enabled: boolean; registration_allowed: boolean };
let streamMode: "ok" | "404" | "hang";
let messages: Msg[];
let capturedAuthHeader: string | null;
let capturedRegenBody: Record<string, unknown> | null;
let pinnedModel: string | null;

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
  pinnedModel = null;
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
          models: { router: "gpt-5-nano", fast: "gemini-fast", smart: "gpt-5", fallback: "gpt-5-mini" },
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
      if (url.endsWith("/v1/conversations") && method === "GET") {
        return Response.json([
          { id: 1, title: "First chat", owner: null, pinned_model: pinnedModel, created_at: "2026-07-18 10:00:00", updated_at: "2026-07-18 10:00:00" },
        ]);
      }
      if (/\/v1\/conversations\/\d+\/pin$/.test(url) && method === "PUT") {
        const body = init?.body ? (JSON.parse(String(init.body)) as { model?: string }) : {};
        pinnedModel = body.model ? body.model : null;
        return Response.json({ id: 1, title: "First chat", owner: null, pinned_model: pinnedModel, created_at: "2026-07-18 10:00:00", updated_at: "2026-07-18 10:00:00" });
      }
      if (/\/v1\/conversations\/\d+\/messages$/.test(url) && method === "GET") {
        return Response.json(messages);
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
      if (/\/ask\/stream$/.test(url) && method === "POST") {
        capturedAuthHeader = authed;
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
        messages = PERSISTED;
        return sseResponse(SSE_BODY);
      }
      throw new Error(`Unhandled request: ${method} ${url}`);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
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

  it("renders assistant markdown (bold) rather than raw text", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "this is **bold** now", created_at: "2026-07-18 10:00:00" },
    ];
    render(<App />);
    const bold = await screen.findByText("bold");
    expect(bold.tagName).toBe("STRONG");
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

  it("surfaces a 404 error and restores the question", async () => {
    streamMode = "404";
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const box = screen.getByLabelText(/Ask a question/i);
    await user.type(box, "will fail");
    await user.click(screen.getByRole("button", { name: /^Ask$/i }));

    expect(await screen.findByText(/Conversation not found/i)).toBeInTheDocument();
    expect(box).toHaveValue("will fail");
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
});
