import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  bookmarked?: boolean;
  truncated?: boolean;
  code_results?: { code: string; logs?: string | null; images?: string[] | null }[] | null;
  fact_checks?: { claim: string; rating?: string | null; publisher?: string | null; url?: string | null }[] | null;
  math_results?: { operation: string; expression: string; variable: string; result?: string | null; error?: string | null; source?: string | null }[] | null;
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

const SSE_BODY_WITH_CODE =
  META_FRAME +
  'event: delta\ndata: {"text":"The answer is 4."}\n\n' +
  'event: done\ndata: {"answer":"The answer is 4.","mode_used":"auto->fast","notes":"n","code_results":[{"code":"print(2 + 2)","logs":"4","images":[]}]}\n\n';

// Persisted version deliberately WITHOUT the code result, so a <details> found
// before the post-stream refetch completes can only have come from the live
// streaming render, not the persisted message.
const PERSISTED_NO_CODE: Msg[] = [
  { id: 1, conversation_id: 1, role: "user", content: "what is 2+2", created_at: "2026-07-18 10:01:00" },
  {
    id: 2,
    conversation_id: 1,
    role: "assistant",
    content: "The answer is 4.",
    mode_used: "auto->fast",
    notes: "n | context_messages=0",
    created_at: "2026-07-18 10:01:04",
  },
];

const SSE_BODY_WITH_FACT_CHECK =
  META_FRAME +
  'event: delta\ndata: {"text":"False."}\n\n' +
  'event: done\ndata: {"answer":"False.","mode_used":"auto->fast","notes":"n","fact_checks":[{"claim":"the moon landing was faked","rating":"False","publisher":"Snopes","url":"https://snopes.com/x"}]}\n\n';

// Persisted version deliberately WITHOUT the fact-check, so a result found
// before the post-stream refetch completes can only have come from the live
// streaming render, not the persisted message.
const PERSISTED_NO_FACT_CHECK: Msg[] = [
  { id: 1, conversation_id: 1, role: "user", content: "fact check: the moon landing", created_at: "2026-07-18 10:01:00" },
  {
    id: 2,
    conversation_id: 1,
    role: "assistant",
    content: "False.",
    mode_used: "auto->fast",
    notes: "n | context_messages=0",
    created_at: "2026-07-18 10:01:04",
  },
];

const SSE_BODY_WITH_MATH_RESULT =
  META_FRAME +
  'event: delta\ndata: {"text":"Computed exactly: **[-2, 2]**"}\n\n' +
  'event: done\ndata: {"answer":"Computed exactly: **[-2, 2]**","mode_used":"auto->fast","notes":"n","math_results":[{"operation":"solve","expression":"x**2 - 4","variable":"x","result":"[-2, 2]"}]}\n\n';

// Persisted version deliberately WITHOUT the math result, so a value found
// before the post-stream refetch completes can only have come from the live
// streaming render, not the persisted message.
const PERSISTED_NO_MATH_RESULT: Msg[] = [
  { id: 1, conversation_id: 1, role: "user", content: "solve x^2 = 4", created_at: "2026-07-18 10:01:00" },
  {
    id: 2,
    conversation_id: 1,
    role: "assistant",
    content: "Computed exactly: **[-2, 2]**",
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
let statusBody: { jwt_enabled: boolean; registration_allowed: boolean; auth_enabled?: boolean };
let streamMode:
  | "ok"
  | "404"
  | "hang"
  | "sources"
  | "action"
  | "image"
  | "code"
  | "factcheck"
  | "mathsolve"
  | "refused"
  | "rate_limited";
// Deterministic replacement for a real setTimeout delay: several tests
// assert on the LIVE streaming bubble's content (sources/pending_action/
// image/code_results arrive on the SSE "done" frame) before the app's
// post-stream GET .../messages refetch swaps it for the persisted message
// list, which deliberately omits that field to prove the assertion could
// only have come from the live render. A real-time delay here raced the
// app's own timing under load (this is what made
// "shows a pending action in the live streaming bubble..." flake in CI) —
// holding the refetch open until the test explicitly releases it removes
// the race entirely.
let pendingMessagesRefetchPromise: Promise<void> | null = null;
let pendingMessagesRefetchResolve: (() => void) | null = null;
function holdNextMessagesRefetch() {
  pendingMessagesRefetchPromise = new Promise<void>((resolve) => {
    pendingMessagesRefetchResolve = resolve;
  });
}
async function releaseMessagesRefetch() {
  const resolve = pendingMessagesRefetchResolve;
  pendingMessagesRefetchPromise = null;
  pendingMessagesRefetchResolve = null;
  if (!resolve) {
    return;
  }
  // The resolved fetch's .then handlers (setMessages) run as a microtask
  // after this — flushing inside act() keeps that state update from
  // bleeding past the end of the test that released it.
  await act(async () => {
    resolve();
    await Promise.resolve();
  });
}
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
let capturedEstimateBody: Record<string, unknown> | null;
let estimateShouldFail: boolean;
let estimateResponse: {
  model: string;
  mode_used: string;
  input_tokens_estimate: number;
  output_tokens_estimate: number;
  cost_usd_estimate: number | null;
};
let clipboardWriteText: ReturnType<typeof vi.fn>;
let capturedDeleteMessageUrl: string | null;
let deleteMessageShouldFail: boolean;
let capturedRestoreMessageBody: Record<string, unknown> | null;
let restoreMessageShouldFail: boolean;
let nextRestoredMessageId: number;
let capturedBookmarkBody: { bookmarked?: boolean } | null;
let bookmarkShouldFail: boolean;
let continueShouldFail: boolean;
let continueCallCount: number;
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
let capturedImportBodies: Record<string, unknown>[];
let importShouldFail: boolean;
let importedConversationsList: NonNullable<typeof importedConversation>[];
let importFailAfterCount: number | null;
let duplicatedConversation: typeof importedConversation;
let capturedDuplicateUrl: string | null;
let duplicateShouldFail: boolean;
let branchedConversation: typeof importedConversation;
let capturedBranchUrl: string | null;
let branchShouldFail: boolean;
let newlyCreatedConversation: typeof importedConversation;
let capturedCreateBody: Record<string, unknown> | null;
let bookmarksResponse: Record<string, unknown>[];
let createConversationShouldReturn401: boolean;
let bulkExtraConversations: { id: number; title: string; owner: null; pinned_model: null; system_prompt: null; favorite: boolean; archived: boolean; created_at: string; updated_at: string }[];
let conversationArchivedOverrides: Record<number, boolean>;
let deletedConversationIds: Set<number>;
let archiveShouldFailForId: number | null;
let deleteShouldFailForId: number | null;
let tagsState: string[];
let capturedTagsBody: Record<string, unknown> | null;
let tagsShouldFail: boolean;
let tagsShouldFailForId: number | null;
let loginShouldFail: boolean;
let usageBudgetOverride: { daily_budget_per_owner_usd: number | null; owner_remaining_usd: number | null } | null;
let usageTodayOverride: {
  today_usd: number;
  daily_budget_usd: number | null;
  avoided_cost_today_usd?: number;
} | null;
let conversationTagsOverrides: Record<number, string[]>;
let conversationFavoriteOverrides: Record<number, boolean>;

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
  pendingMessagesRefetchPromise = null;
  pendingMessagesRefetchResolve = null;
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
  capturedEstimateBody = null;
  estimateShouldFail = false;
  estimateResponse = {
    model: "gpt-5",
    mode_used: "auto->fast",
    input_tokens_estimate: 3,
    output_tokens_estimate: 1500,
    cost_usd_estimate: 0.0123,
  };
  capturedDeleteMessageUrl = null;
  deleteMessageShouldFail = false;
  capturedRestoreMessageBody = null;
  restoreMessageShouldFail = false;
  nextRestoredMessageId = 900;
  capturedBookmarkBody = null;
  bookmarkShouldFail = false;
  continueShouldFail = false;
  continueCallCount = 0;
  importedConversation = null;
  capturedImportBody = null;
  capturedImportBodies = [];
  importShouldFail = false;
  importedConversationsList = [];
  importFailAfterCount = null;
  duplicatedConversation = null;
  capturedDuplicateUrl = null;
  duplicateShouldFail = false;
  branchedConversation = null;
  capturedBranchUrl = null;
  branchShouldFail = false;
  newlyCreatedConversation = null;
  capturedCreateBody = null;
  bookmarksResponse = [];
  createConversationShouldReturn401 = false;
  bulkExtraConversations = [];
  conversationArchivedOverrides = {};
  deletedConversationIds = new Set();
  archiveShouldFailForId = null;
  deleteShouldFailForId = null;
  tagsState = [];
  capturedTagsBody = null;
  tagsShouldFail = false;
  tagsShouldFailForId = null;
  loginShouldFail = false;
  usageBudgetOverride = null;
  usageTodayOverride = null;
  conversationTagsOverrides = {};
  conversationFavoriteOverrides = {};
  createdNotifications = [];
  MockNotification.permission = "granted";
  MockNotification.requestPermission.mockClear();
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
          features: [],
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
        if (loginShouldFail) {
          return new Response(JSON.stringify({ detail: "Invalid username or password" }), {
            status: 401,
            headers: { "Content-Type": "application/json" },
          });
        }
        return Response.json({ access_token: "jwt-token", token_type: "bearer" });
      }
      if (url.endsWith("/v1/auth/logout") && method === "POST") {
        return Response.json({ status: "logged_out" });
      }
      if (url.includes("/v1/search") && method === "GET") {
        capturedSearchQuery = new URL(url, "http://localhost").searchParams.get("q");
        return Response.json(searchResultsResponse);
      }
      if (url.endsWith("/v1/estimate") && method === "POST") {
        capturedEstimateBody = init?.body
          ? (JSON.parse(String(init.body)) as Record<string, unknown>)
          : null;
        if (estimateShouldFail) {
          return new Response(JSON.stringify({ detail: "boom" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        return Response.json(estimateResponse);
      }
      if (url.includes("/v1/usage") && method === "GET") {
        return Response.json({
          today_usd: usageTodayOverride?.today_usd ?? 0,
          days: 14,
          by_model: [],
          by_day: [],
          daily_budget_usd: usageTodayOverride?.daily_budget_usd ?? null,
          daily_budget_per_owner_usd: usageBudgetOverride?.daily_budget_per_owner_usd ?? null,
          owner_remaining_usd: usageBudgetOverride?.owner_remaining_usd ?? null,
          avoided_cost_today_usd: usageTodayOverride?.avoided_cost_today_usd ?? 0,
        });
      }
      if (url.includes("/v1/bookmarks") && method === "GET") {
        return Response.json(bookmarksResponse);
      }
      if (/\/v1\/conversations\/\d+\/summarize$/.test(url) && method === "POST") {
        return Response.json({ summary: "A short recap of the conversation." });
      }
      if (url.endsWith("/v1/conversations") && method === "POST") {
        capturedCreateBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        if (createConversationShouldReturn401) {
          return new Response(JSON.stringify({ detail: "Invalid or missing API token" }), {
            status: 401,
            headers: { "Content-Type": "application/json" },
          });
        }
        newlyCreatedConversation = {
          id: 20,
          title: (capturedCreateBody?.title as string) || "New AI Workbench Conversation",
          owner: null,
          pinned_model: null,
          system_prompt: null,
          created_at: "2026-07-20 09:00:00",
          updated_at: "2026-07-20 09:00:00",
        };
        return Response.json(newlyCreatedConversation);
      }
      if ((url.endsWith("/v1/conversations") || url.includes("/v1/conversations?")) && method === "GET") {
        const includeArchived = url.includes("include_archived=true");
        const extra = bulkExtraConversations.map((c) => ({
          ...c,
          archived: conversationArchivedOverrides[c.id] ?? c.archived,
          tags: conversationTagsOverrides[c.id] ?? c.tags,
          favorite: conversationFavoriteOverrides[c.id] ?? c.favorite,
        }));
        return Response.json(
          [
            ...(newlyCreatedConversation ? [newlyCreatedConversation] : []),
            ...(archivedState && !includeArchived
              ? []
              : [{ id: 1, title: "First chat", owner: null, pinned_model: pinnedModel, system_prompt: systemPrompt, favorite: favoriteState, archived: archivedState, tags: tagsState, created_at: "2026-07-18 10:00:00", updated_at: "2026-07-18 10:00:00" }]),
            ...importedConversationsList,
            ...(duplicatedConversation ? [duplicatedConversation] : []),
            ...(branchedConversation ? [branchedConversation] : []),
            ...extra,
          ].filter((c) => !deletedConversationIds.has(c.id) && (includeArchived || !c.archived)),
        );
      }
      if (/\/v1\/conversations\/\d+\/favorite$/.test(url) && method === "PUT") {
        const id = Number(url.match(/\/conversations\/(\d+)\/favorite$/)?.[1]);
        capturedFavoriteBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        const nextFavorite = Boolean(capturedFavoriteBody?.favorite);
        conversationFavoriteOverrides[id] = nextFavorite;
        if (id === 1) favoriteState = nextFavorite;
        const title = bulkExtraConversations.find((c) => c.id === id)?.title ?? "First chat";
        return Response.json({ id, title, owner: null, pinned_model: pinnedModel, system_prompt: systemPrompt, favorite: nextFavorite, archived: archivedState, tags: tagsState, created_at: "2026-07-18 10:00:00", updated_at: "2026-07-18 10:00:00" });
      }
      if (/\/v1\/conversations\/\d+\/tags$/.test(url) && method === "PUT") {
        const id = Number(url.match(/\/conversations\/(\d+)\/tags$/)?.[1]);
        capturedTagsBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        if (tagsShouldFail || id === tagsShouldFailForId) {
          return new Response(JSON.stringify({ detail: "boom" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        const nextTags = Array.isArray(capturedTagsBody?.tags) ? (capturedTagsBody.tags as string[]) : [];
        conversationTagsOverrides[id] = nextTags;
        if (id === 1) tagsState = nextTags;
        const title = bulkExtraConversations.find((c) => c.id === id)?.title ?? "First chat";
        return Response.json({ id, title, owner: null, pinned_model: pinnedModel, system_prompt: systemPrompt, favorite: favoriteState, archived: archivedState, tags: nextTags, created_at: "2026-07-18 10:00:00", updated_at: "2026-07-18 10:00:00" });
      }
      if (/\/v1\/conversations\/\d+\/archive$/.test(url) && method === "PUT") {
        const id = Number(url.match(/\/conversations\/(\d+)\/archive$/)?.[1]);
        if (id === archiveShouldFailForId) {
          return new Response(JSON.stringify({ detail: "boom" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        capturedArchiveBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        const nextArchived = Boolean(capturedArchiveBody?.archived);
        conversationArchivedOverrides[id] = nextArchived;
        if (id === 1) archivedState = nextArchived;
        const title = bulkExtraConversations.find((c) => c.id === id)?.title ?? "First chat";
        return Response.json({ id, title, owner: null, pinned_model: pinnedModel, system_prompt: systemPrompt, favorite: favoriteState, archived: nextArchived, created_at: "2026-07-18 10:00:00", updated_at: "2026-07-18 10:00:00" });
      }
      if (/\/v1\/conversations\/\d+$/.test(url) && method === "DELETE") {
        const id = Number(url.split("/").pop());
        if (id === deleteShouldFailForId) {
          return new Response(JSON.stringify({ detail: "boom" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        deletedConversationIds.add(id);
        return Response.json({ status: "deleted", conversation_id: id });
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
      if (/\/v1\/conversations\/\d+\/messages\/\d+\/branch$/.test(url) && method === "POST") {
        capturedBranchUrl = url;
        if (branchShouldFail) {
          return new Response(JSON.stringify({ detail: "boom" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        messages = messages.map((message, index) => ({ ...message, id: 300 + index, conversation_id: 4 }));
        branchedConversation = {
          id: 4,
          title: "First chat (branch)",
          owner: null,
          pinned_model: pinnedModel,
          system_prompt: systemPrompt,
          created_at: "2026-07-19 09:30:00",
          updated_at: "2026-07-19 09:30:00",
        };
        return Response.json(branchedConversation);
      }
      if (url.endsWith("/v1/conversations/import") && method === "POST") {
        capturedImportBody = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
        if (capturedImportBody) capturedImportBodies.push(capturedImportBody);
        if (
          importShouldFail ||
          (importFailAfterCount !== null && capturedImportBodies.length > importFailAfterCount)
        ) {
          return new Response(JSON.stringify({ detail: "Import failed: bad data" }), {
            status: 422,
            headers: { "Content-Type": "application/json" },
          });
        }
        const importMessages = (capturedImportBody?.messages as { role: string; content: string }[]) ?? [];
        const newId = 100 + importedConversationsList.length;
        messages = importMessages.map((message, index) => ({
          id: newId * 100 + index,
          conversation_id: newId,
          role: message.role,
          content: message.content,
          created_at: "2026-07-19 09:00:00",
        }));
        importedConversation = {
          id: newId,
          title: (capturedImportBody?.title as string) || "Imported conversation",
          owner: null,
          pinned_model: null,
          system_prompt: null,
          created_at: "2026-07-19 09:00:00",
          updated_at: "2026-07-19 09:00:00",
        };
        importedConversationsList.push(importedConversation);
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
        if (pendingMessagesRefetchPromise) {
          await pendingMessagesRefetchPromise;
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
      if (/\/v1\/conversations\/\d+\/messages\/restore$/.test(url) && method === "POST") {
        const body = init?.body
          ? (JSON.parse(String(init.body)) as Record<string, unknown>)
          : {};
        capturedRestoreMessageBody = body;
        if (restoreMessageShouldFail) {
          return new Response(JSON.stringify({ detail: "boom" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        const conversationId = Number(url.match(/\/conversations\/(\d+)\/messages\/restore$/)?.[1]);
        const restored: Msg = {
          id: nextRestoredMessageId++,
          conversation_id: conversationId,
          role: String(body.role ?? "assistant"),
          content: String(body.content ?? ""),
          mode_used: (body.mode_used as string | null | undefined) ?? null,
          created_at: "2026-07-20 09:00:00",
        };
        messages = [...messages, restored];
        return Response.json(restored);
      }
      if (/\/v1\/conversations\/\d+\/messages\/\d+\/bookmark$/.test(url) && method === "PUT") {
        const messageId = Number(url.match(/\/messages\/(\d+)\/bookmark$/)?.[1]);
        const body = init?.body
          ? (JSON.parse(String(init.body)) as { bookmarked?: boolean })
          : {};
        capturedBookmarkBody = body;
        if (bookmarkShouldFail) {
          return new Response(JSON.stringify({ detail: "boom" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        messages = messages.map((m) =>
          m.id === messageId ? { ...m, bookmarked: Boolean(body.bookmarked) } : m,
        );
        const updated = messages.find((m) => m.id === messageId);
        if (!updated) {
          return new Response(JSON.stringify({ detail: "Message not found" }), {
            status: 404,
            headers: { "Content-Type": "application/json" },
          });
        }
        return Response.json(updated);
      }
      if (/\/v1\/conversations\/\d+\/messages\/\d+\/continue$/.test(url) && method === "POST") {
        continueCallCount += 1;
        const messageId = Number(url.match(/\/messages\/(\d+)\/continue$/)?.[1]);
        if (continueShouldFail) {
          return new Response(JSON.stringify({ detail: "Continuation failed" }), {
            status: 502,
            headers: { "Content-Type": "application/json" },
          });
        }
        messages = messages.map((m) =>
          m.id === messageId
            ? { ...m, content: `${m.content}-continued`, truncated: false }
            : m,
        );
        const updated = messages.find((m) => m.id === messageId);
        if (!updated) {
          return new Response(JSON.stringify({ detail: "Message not found" }), {
            status: 404,
            headers: { "Content-Type": "application/json" },
          });
        }
        return Response.json(updated);
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
        if (streamMode === "rate_limited") {
          return new Response(
            JSON.stringify({ error: "Rate limit exceeded: 60 per 1 minute" }),
            { status: 429, headers: { "Content-Type": "application/json" } },
          );
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
        if (streamMode === "code") {
          messages = PERSISTED_NO_CODE;
          return sseResponse(SSE_BODY_WITH_CODE);
        }
        if (streamMode === "factcheck") {
          messages = PERSISTED_NO_FACT_CHECK;
          return sseResponse(SSE_BODY_WITH_FACT_CHECK);
        }
        if (streamMode === "mathsolve") {
          messages = PERSISTED_NO_MATH_RESULT;
          return sseResponse(SSE_BODY_WITH_MATH_RESULT);
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

afterEach(async () => {
  vi.unstubAllGlobals();
  document.documentElement.removeAttribute("data-theme");
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
  document.title = "AI Workbench";
  window.history.replaceState(null, "", "/");
  // Safety net: release a gate a test forgot to (or failed before reaching
  // its release call), so a dangling unresolved fetch never bleeds into the
  // next test.
  await releaseMessagesRefetch();
});

function setDocumentHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", { configurable: true, value: hidden });
}

class MockNotification {
  static permission: NotificationPermission = "granted";
  static requestPermission = vi.fn(async (): Promise<NotificationPermission> => "granted");
  title: string;
  body?: string;
  onclick: (() => void) | null = null;
  close = vi.fn();

  constructor(title: string, options?: NotificationOptions) {
    this.title = title;
    this.body = options?.body;
    createdNotifications.push(this);
  }
}
let createdNotifications: MockNotification[];

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
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    expect(await screen.findByText("Hello world")).toBeInTheDocument();
    expect(screen.getByText("auto->fast")).toBeInTheDocument();
  });

  it("announces the completed answer in an aria-live region for screen readers", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    // "Hello world" appears in the persisted message bubble AND in the
    // hidden live-region announcement — the announcement is prefixed so it
    // doesn't collide with getByText("Hello world") matching two nodes.
    const announcement = await screen.findByText(/Answer received: Hello world/i);
    expect(announcement).toBeInTheDocument();
    // Regression guard: this element must use the "sr-only" class (clipped,
    // still in the accessibility tree), NOT "visually-hidden" (display:
    // none) — the latter would silently prevent the aria-live announcement
    // from ever reaching a screen reader. See App.css for both classes'
    // very different accessibility semantics.
    expect(announcement).toHaveClass("sr-only");
    expect(announcement).not.toHaveClass("visually-hidden");
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

  it("copies a shareable link to a specific message, including its conversation id", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "Hello world", created_at: "2026-07-18 10:00:00" },
    ];
    const user = userEvent.setup();
    clipboardWriteText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    render(<App />);
    await screen.findByText("Hello world");

    await user.click(screen.getByRole("button", { name: "Copy link to this message" }));

    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining("c=1"));
    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining("m=1"));
    expect(await screen.findByRole("button", { name: "Link copied!" })).toBeInTheDocument();
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

  it("shows a copy button on a code_results block and copies the code", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "The answer is 4.",
        code_results: [{ code: "print(2 + 2)", logs: "4", images: [] }],
        created_at: "2026-07-18 10:00:00",
      },
    ];
    const user = userEvent.setup();
    clipboardWriteText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    render(<App />);
    await screen.findByText("print(2 + 2)");

    await user.click(screen.getByRole("button", { name: "Copy code" }));

    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining("print(2 + 2)"));
    expect(await screen.findByRole("button", { name: "Copied!" })).toBeInTheDocument();
  });

  it("shows fact-check results with rating, claim, and a source link", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "False.",
        fact_checks: [
          {
            claim: "The moon landing was faked",
            rating: "False",
            publisher: "Snopes",
            url: "https://snopes.com/fact-check/moon-landing",
          },
        ],
        created_at: "2026-07-18 10:00:00",
      },
    ];
    render(<App />);

    expect(await screen.findByText("False")).toBeInTheDocument();
    expect(screen.getByText("The moon landing was faked")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Snopes" });
    expect(link).toHaveAttribute("href", "https://snopes.com/fact-check/moon-landing");
    // Accessible: the list has a name, and the rating carries enough
    // context on its own (not just a bare "False") for a screen reader.
    expect(screen.getByRole("list", { name: "Fact checks" })).toBeInTheDocument();
    expect(screen.getByText("False")).toHaveAttribute("aria-label", "Rating: False");
  });

  it("shows a math_solve result with its expression and value", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "Computed exactly: **[-2, 2]**",
        math_results: [
          {
            operation: "solve",
            expression: "x**2 - 4",
            variable: "x",
            result: "[-2, 2]",
          },
        ],
        created_at: "2026-07-18 10:00:00",
      },
    ];
    render(<App />);

    expect(await screen.findByText("x**2 - 4")).toBeInTheDocument();
    expect(screen.getByText("= [-2, 2]")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Computed results" })).toBeInTheDocument();
  });

  it("shows a Wolfram Alpha attribution when SymPy fell back to it", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "Computed exactly: **42**",
        math_results: [
          {
            operation: "evaluate",
            expression: "some transcendental thing",
            variable: "x",
            result: "42",
            source: "wolfram_alpha",
          },
        ],
        created_at: "2026-07-18 10:00:00",
      },
    ];
    render(<App />);

    expect(await screen.findByText(/via Wolfram Alpha/i)).toHaveClass(
      "math-result-source",
    );
  });

  it("shows a math_solve error when the expression couldn't be computed", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "Couldn't compute that.",
        math_results: [
          {
            operation: "solve",
            expression: "bad expr",
            variable: "x",
            error: "unknown operation",
          },
        ],
        created_at: "2026-07-18 10:00:00",
      },
    ];
    render(<App />);

    expect(await screen.findByText("bad expr")).toBeInTheDocument();
    expect(screen.getByText("unknown operation")).toBeInTheDocument();
    // A bare error string floating in the message would read ambiguously
    // to a screen reader without this context.
    expect(screen.getByText("unknown operation")).toHaveAttribute(
      "aria-label",
      "Error: unknown operation",
    );
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

  it("offers Undo after deleting a message, and restores it via the restore endpoint", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
      { id: 2, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello!");

    await user.click(screen.getByRole("button", { name: /Delete assistant message/i }));
    expect(await screen.findByText(/Deleted this message\./i)).toBeInTheDocument();
    expect(screen.queryByText("hello!")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Undo" }));

    await waitFor(() => {
      expect(capturedRestoreMessageBody?.content).toBe("hello!");
    });
    expect(capturedRestoreMessageBody?.role).toBe("assistant");
    expect(await screen.findByText("hello!")).toBeInTheDocument();
    expect(await screen.findByText(/Message restored\./i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
  });

  it("shows a status message when restoring a deleted message fails", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    restoreMessageShouldFail = true;
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello!");

    await user.click(screen.getByRole("button", { name: /Delete assistant message/i }));
    await user.click(await screen.findByRole("button", { name: "Undo" }));

    expect(await screen.findByText(/boom/i)).toBeInTheDocument();
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

  it("bookmarks a message", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello!");

    const bookmarkButton = screen.getByRole("button", { name: /^Bookmark assistant message/i });
    expect(bookmarkButton).toHaveAttribute("aria-pressed", "false");

    await user.click(bookmarkButton);

    expect(capturedBookmarkBody).toEqual({ bookmarked: true });
    const removeButton = await screen.findByRole("button", {
      name: /Remove bookmark from assistant message/i,
    });
    expect(removeButton).toHaveAttribute("aria-pressed", "true");
    expect(removeButton).toHaveTextContent("🔖");
  });

  it("removes a bookmark from a message", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "hello!",
        bookmarked: true,
        created_at: "2026-07-18 10:01:04",
      },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello!");

    await user.click(screen.getByRole("button", { name: /Remove bookmark from assistant message/i }));

    expect(capturedBookmarkBody).toEqual({ bookmarked: false });
    const bookmarkButton = await screen.findByRole("button", {
      name: /^Bookmark assistant message/i,
    });
    expect(bookmarkButton).toHaveAttribute("aria-pressed", "false");
  });

  it("shows a status message when bookmarking fails", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    bookmarkShouldFail = true;
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello!");

    await user.click(screen.getByRole("button", { name: /^Bookmark assistant message/i }));

    expect(await screen.findByText(/Failed to update bookmark/i)).toBeInTheDocument();
  });

  it("bookmarking one message doesn't affect another", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
      { id: 2, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello!");

    await user.click(screen.getByRole("button", { name: /^Bookmark user message/i }));

    expect(
      await screen.findByRole("button", { name: /Remove bookmark from user message/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^Bookmark assistant message/i }),
    ).toHaveAttribute("aria-pressed", "false");
  });

  it("shows no truncation notice for a normal, complete answer", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi", created_at: "2026-07-18 10:01:00" },
      { id: 2, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    render(<App />);
    await screen.findByText("hello!");

    expect(screen.queryByText(/Response was cut off/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^\$ Continue$/i })).not.toBeInTheDocument();
  });

  it("shows a truncation notice with a Continue button for a truncated answer", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi", created_at: "2026-07-18 10:01:00" },
      {
        id: 2,
        conversation_id: 1,
        role: "assistant",
        content: "cut off mid",
        truncated: true,
        created_at: "2026-07-18 10:01:04",
      },
    ];
    render(<App />);
    await screen.findByText("cut off mid");

    expect(screen.getByText(/Response was cut off/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^\$ Continue$/i })).toBeInTheDocument();
  });

  it("continuing a truncated message appends the response and clears the notice", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi", created_at: "2026-07-18 10:01:00" },
      {
        id: 2,
        conversation_id: 1,
        role: "assistant",
        content: "cut off mid",
        truncated: true,
        created_at: "2026-07-18 10:01:04",
      },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("cut off mid");

    await user.click(screen.getByRole("button", { name: /^\$ Continue$/i }));

    expect(await screen.findByText("cut off mid-continued")).toBeInTheDocument();
    expect(screen.queryByText(/Response was cut off/i)).not.toBeInTheDocument();
    expect(continueCallCount).toBe(1);
  });

  it("shows a status message when continuing a truncated message fails", async () => {
    continueShouldFail = true;
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi", created_at: "2026-07-18 10:01:00" },
      {
        id: 2,
        conversation_id: 1,
        role: "assistant",
        content: "cut off mid",
        truncated: true,
        created_at: "2026-07-18 10:01:04",
      },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("cut off mid");

    await user.click(screen.getByRole("button", { name: /^\$ Continue$/i }));

    expect(await screen.findByText("Continuation failed")).toBeInTheDocument();
    // The notice is still there — the failed continuation left it truncated.
    expect(screen.getByText(/Response was cut off/i)).toBeInTheDocument();
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
    holdNextMessagesRefetch();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "weather");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    // The persisted refetch (PERSISTED_NO_SOURCES) carries no sources, so this
    // link can only have come from streamState during the live render. The
    // refetch is held open (see holdNextMessagesRefetch) until released below,
    // so there's no race against it to win.
    const link = await screen.findByRole("link", { name: "Weather Site" });
    expect(link).toHaveAttribute("href", "https://weather.example");
    await releaseMessagesRefetch();
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
    holdNextMessagesRefetch();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "email bob");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    // The persisted refetch (PERSISTED_NO_ACTION) carries no pending_action, so
    // this can only have come from streamState during the live render. The
    // refetch is held open (see holdNextMessagesRefetch) until released below,
    // so there's no race against it to win.
    expect(await screen.findByText("Email Bob the report")).toBeInTheDocument();
    expect(screen.getByText("Confirm below once sent")).toBeInTheDocument();
    await releaseMessagesRefetch();
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
    holdNextMessagesRefetch();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "draw a cat");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    // The persisted refetch (PERSISTED_NO_IMAGE) carries no images, so this can
    // only have come from streamState during the live render. The refetch is
    // held open (see holdNextMessagesRefetch) until released below, so
    // there's no race against it to win.
    const img = await screen.findByRole("img", { name: "Generated" });
    expect(img).toHaveAttribute("src", "data:image/png;base64,aaa");
    await releaseMessagesRefetch();
  });

  it("renders code the model ran under the assistant message", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "The answer is 4.",
        code_results: [{ code: "print(2 + 2)", logs: "4", images: [] }],
        created_at: "2026-07-18 10:00:00",
      },
    ];
    render(<App />);
    await screen.findByText("The answer is 4.");

    expect(screen.getByText("print(2 + 2)")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
    expect(screen.getByText("Ran code")).toBeInTheDocument();
  });

  it("shows code results in the live streaming bubble from the done frame, before the post-stream refresh", async () => {
    streamMode = "code";
    holdNextMessagesRefetch();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "what is 2+2");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    // The persisted refetch (PERSISTED_NO_CODE) carries no code_results, so
    // this can only have come from streamState during the live render. The
    // refetch is held open (see holdNextMessagesRefetch) until released
    // below, so there's no race against it to win.
    expect(await screen.findByText("print(2 + 2)")).toBeInTheDocument();
    await releaseMessagesRefetch();
  });

  it("shows fact-check results in the live streaming bubble from the done frame, before the post-stream refresh", async () => {
    streamMode = "factcheck";
    holdNextMessagesRefetch();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "fact check: the moon landing");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    // The persisted refetch (PERSISTED_NO_FACT_CHECK) carries no fact_checks,
    // so this can only have come from streamState during the live render.
    expect(await screen.findByText("the moon landing was faked")).toBeInTheDocument();
    expect(screen.getByText("False")).toBeInTheDocument();
    await releaseMessagesRefetch();
  });

  it("shows a math_solve result in the live streaming bubble from the done frame, before the post-stream refresh", async () => {
    streamMode = "mathsolve";
    holdNextMessagesRefetch();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "solve x^2 = 4");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    // The persisted refetch (PERSISTED_NO_MATH_RESULT) carries no
    // math_results, so this can only have come from streamState during the
    // live render.
    expect(await screen.findByText("x**2 - 4")).toBeInTheDocument();
    expect(screen.getByText("= [-2, 2]")).toBeInTheDocument();
    await releaseMessagesRefetch();
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
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    await screen.findByText("Hello world");
    expect(capturedAskBody?.images).toEqual([expect.stringMatching(/^data:image\/png;base64,/)]);

    // The composer's preview clears after sending.
    expect(screen.queryByAltText("Attachment 1")).not.toBeInTheDocument();
  });

  it("sends research:true when research mode is toggled on", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Toggle research mode" }));
    await user.type(screen.getByLabelText(/Ask a question/i), "what is 2+2");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    await screen.findByText("Hello world");
    expect(capturedAskBody?.research).toBe(true);
  });

  it("does not send a research field when research mode is off", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "what is 2+2");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    await screen.findByText("Hello world");
    expect(capturedAskBody?.research).toBeUndefined();
  });

  it("attaches a screenshot pasted into the composer from the clipboard", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const file = new File(["fake-bytes"], "screenshot.png", { type: "image/png" });
    const textarea = screen.getByLabelText(/Ask a question/i);
    fireEvent.paste(textarea, { clipboardData: { files: [file] } });

    // A thumbnail preview appears, same as attaching via the 📎 picker.
    await screen.findByAltText("Attachment 1");
  });

  it("does not attach anything when pasting plain text (no clipboard files)", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const textarea = screen.getByLabelText(/Ask a question/i);
    fireEvent.paste(textarea, { clipboardData: { files: [] } });
    await user.type(textarea, "just some text");

    expect(screen.queryByAltText("Attachment 1")).not.toBeInTheDocument();
    expect(textarea).toHaveValue("just some text");
  });

  it("attaches an image dropped onto the composer", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const file = new File(["fake-bytes"], "screenshot.png", { type: "image/png" });
    const composer = document.querySelector(".composer");
    if (!composer) throw new Error("composer not found");
    fireEvent.drop(composer, { dataTransfer: { files: [file] } });

    await screen.findByAltText("Attachment 1");
  });

  it("shows a drag-active highlight while dragging over the composer, cleared on drop", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const composer = document.querySelector(".composer");
    if (!composer) throw new Error("composer not found");
    fireEvent.dragOver(composer, { dataTransfer: { files: [] } });
    expect(composer).toHaveClass("drag-active");

    fireEvent.drop(composer, { dataTransfer: { files: [] } });
    expect(composer).not.toHaveClass("drag-active");
  });

  it("clears the drag-active highlight once the pointer actually leaves the composer", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const composer = document.querySelector(".composer");
    if (!composer) throw new Error("composer not found");
    fireEvent.dragOver(composer, { dataTransfer: { files: [] } });
    expect(composer).toHaveClass("drag-active");

    fireEvent.dragLeave(composer, { relatedTarget: document.body });
    expect(composer).not.toHaveClass("drag-active");
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
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));
    await screen.findByText("Hello world");
    expect(capturedAskBody?.images).toBeUndefined();
  });

  it("shows the attached image in the live streaming user bubble", async () => {
    // Use a streamMode with a delayed persisted refetch (see the sources test
    // above) so the live bubble is observable before it's swapped out.
    streamMode = "sources";
    holdNextMessagesRefetch();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const file = new File(["fake-bytes"], "cat.png", { type: "image/png" });
    const fileInput = screen.getByLabelText(/Attach image/i) as HTMLInputElement;
    await user.upload(fileInput, file);
    await screen.findByAltText("Attachment 1");

    await user.type(screen.getByLabelText(/Ask a question/i), "what is this");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    // While streaming, the live user bubble shows the attached image too.
    const img = await screen.findByRole("img", { name: "Attached" });
    expect(img).toHaveAttribute("src", expect.stringMatching(/^data:image\/png;base64,/));
    await releaseMessagesRefetch();
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
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

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
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));
    await screen.findByText("Hello world");
    expect(capturedAskBody?.files).toBeUndefined();
  });

  it("shows the attached file in the live streaming user bubble", async () => {
    streamMode = "sources";
    holdNextMessagesRefetch();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const file = new File(["%PDF-1.4 fake"], "report.pdf", { type: "application/pdf" });
    const fileInput = screen.getByLabelText(/Attach image or document/i) as HTMLInputElement;
    await user.upload(fileInput, file);
    await screen.findByText("📄 report.pdf");

    await user.type(screen.getByLabelText(/Ask a question/i), "summarize this");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    // While streaming, the live user bubble shows the attached file chip too.
    const chips = await screen.findAllByText("📄 report.pdf");
    expect(chips.length).toBeGreaterThan(0);
    await releaseMessagesRefetch();
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

  it("reads a message aloud with the free browser voice, at zero API cost", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "Hello world",
        created_at: "2026-07-18 10:00:00",
      },
    ];

    class FakeUtterance {
      text: string;
      onend: (() => void) | null = null;
      onerror: (() => void) | null = null;
      constructor(text: string) {
        this.text = text;
      }
    }
    const cancel = vi.fn();
    const speak = vi.fn();
    vi.stubGlobal("SpeechSynthesisUtterance", FakeUtterance);
    vi.stubGlobal("speechSynthesis", { cancel, speak });

    const user = userEvent.setup();
    render(<App />);

    const freeSpeakButton = await screen.findByRole("button", { name: "Free text-to-speech for this message" });
    await user.click(freeSpeakButton);

    expect(speak).toHaveBeenCalledTimes(1);
    const utterance = speak.mock.calls[0][0] as FakeUtterance;
    expect(utterance.text).toBe("Hello world");

    await screen.findByRole("button", { name: "Stop the free text-to-speech" });
  });

  it("shows an unsupported-browser message when there is no SpeechSynthesis API", async () => {
    messages = [
      {
        id: 1,
        conversation_id: 1,
        role: "assistant",
        content: "Hello world",
        created_at: "2026-07-18 10:00:00",
      },
    ];
    const originalSpeechSynthesis = (window as { speechSynthesis?: unknown }).speechSynthesis;
    // @ts-expect-error -- deliberately removing the API to test the fallback message
    delete window.speechSynthesis;

    try {
      const user = userEvent.setup();
      render(<App />);

      await user.click(
        await screen.findByRole("button", { name: "Free text-to-speech for this message" }),
      );

      await screen.findByText(/doesn't support built-in text-to-speech/i);
    } finally {
      (window as { speechSynthesis?: unknown }).speechSynthesis = originalSpeechSynthesis;
    }
  });

  it("records free voice input via the browser's SpeechRecognition and inserts the text", async () => {
    class FakeRecognition {
      lang = "";
      interimResults = false;
      maxAlternatives = 1;
      onresult: ((event: { results: { transcript: string }[][] }) => void) | null = null;
      onerror: (() => void) | null = null;
      onend: (() => void) | null = null;
      start = vi.fn();
      stop = vi.fn(() => {
        this.onend?.();
      });
    }
    let lastRecognition: FakeRecognition | null = null;
    vi.stubGlobal(
      "webkitSpeechRecognition",
      vi.fn().mockImplementation(() => {
        lastRecognition = new FakeRecognition();
        return lastRecognition;
      }),
    );

    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(await screen.findByRole("button", { name: "Free voice input" }));
    expect(lastRecognition).not.toBeNull();

    act(() => {
      lastRecognition?.onresult?.({ results: [[{ transcript: "hello from free voice" }]] });
    });

    const textarea = (await screen.findByLabelText(/Ask a question/i)) as HTMLTextAreaElement;
    await waitFor(() => expect(textarea.value).toBe("hello from free voice"));
  });

  it("shows an unsupported-browser message when there is no SpeechRecognition API", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(await screen.findByRole("button", { name: "Free voice input" }));

    await screen.findByText(/doesn't support built-in voice input/i);
  });

  it("attaches the bearer token when one is set", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem("ai_workbench_token", "static-tok");
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));
    await screen.findByText("Hello world");
    expect(capturedAuthHeader).toBe("Bearer static-tok");
  });

  it("shows a login form and signs in / out when JWT is enabled", async () => {
    statusBody = { jwt_enabled: true, registration_allowed: true };
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    await user.type(await screen.findByLabelText(/Username/i), "alice");
    await user.type(screen.getByLabelText(/Password/i), "password123");
    await user.click(screen.getByRole("button", { name: /^Log in$/i }));

    expect(await screen.findByText("alice")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Log out/i }));
    expect(await screen.findByRole("button", { name: /^Log in$/i })).toBeInTheDocument();
  });

  it("signs the user out and shows a session-expired message when an authenticated action gets a 401", async () => {
    statusBody = { jwt_enabled: true, registration_allowed: true };
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    await user.type(await screen.findByLabelText(/Username/i), "alice");
    await user.type(screen.getByLabelText(/Password/i), "password123");
    await user.click(screen.getByRole("button", { name: /^Log in$/i }));
    expect(await screen.findByText("alice")).toBeInTheDocument();

    createConversationShouldReturn401 = true;
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByText(/Session expired — please sign in again\./i)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /^Log in$/i })).toBeInTheDocument();
    expect(screen.queryByText("alice")).not.toBeInTheDocument();
  });

  it("tells a not-logged-in user to log in, rather than a generic failure, when an authenticated action gets a 401", async () => {
    statusBody = { jwt_enabled: true, registration_allowed: true };
    createConversationShouldReturn401 = true;
    const user = userEvent.setup();
    render(<App />);

    await screen.findByRole("heading", { name: "Sign in" });
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByText(/Log in to do this/i)).toBeInTheDocument();
    expect(screen.queryByText(/Session expired/i)).not.toBeInTheDocument();
    // Still showing the sign-in form, unchanged — nothing to log out of.
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("shows a message next to the sign-in form when fields are empty, not just the far-away chat status", async () => {
    statusBody = { jwt_enabled: true, registration_allowed: true };
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /^Register$/i }));

    const message = await screen.findByRole("alert");
    expect(message).toHaveTextContent("Enter a username and password.");
    expect(message.closest(".auth-form")).not.toBeNull();
  });

  it("shows a failed login's error message next to the sign-in form", async () => {
    statusBody = { jwt_enabled: true, registration_allowed: true };
    loginShouldFail = true;
    const user = userEvent.setup();
    render(<App />);

    await user.type(await screen.findByLabelText(/Username/i), "alice");
    await user.type(screen.getByLabelText(/Password/i), "wrong-password");
    await user.click(screen.getByRole("button", { name: /^Log in$/i }));

    const message = await screen.findByRole("alert");
    expect(message).toHaveTextContent("Invalid username or password");
    expect(message.closest(".auth-form")).not.toBeNull();
    // Still on the sign-in form — a failed login doesn't leave the page in
    // some ambiguous "did it work?" state.
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("hides the Register button when registration is disabled", async () => {
    statusBody = { jwt_enabled: true, registration_allowed: false };
    render(<App />);
    expect(await screen.findByRole("button", { name: /^Log in$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Register$/i })).toBeNull();
  });

  it("shows a sign-in-required banner when JWT is enabled and nobody's logged in", async () => {
    statusBody = { jwt_enabled: true, registration_allowed: true };
    render(<App />);
    await screen.findByRole("heading", { name: "Sign in" });
    expect(await screen.findByText(/Sign in required/i)).toBeInTheDocument();
  });

  it("focuses the username field when the sign-in banner's button is clicked", async () => {
    statusBody = { jwt_enabled: true, registration_allowed: true };
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/Sign in required/i);

    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(screen.getByLabelText("Username")).toHaveFocus();
  });

  it("hides the sign-in-required banner once logged in", async () => {
    statusBody = { jwt_enabled: true, registration_allowed: true };
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/Sign in required/i);

    await user.type(screen.getByLabelText(/Username/i), "alice");
    await user.type(screen.getByLabelText(/Password/i), "password123");
    await user.click(screen.getByRole("button", { name: /^Log in$/i }));

    await screen.findByText("alice");
    expect(screen.queryByText(/Sign in required/i)).not.toBeInTheDocument();
  });

  it("does not show the sign-in-required banner when JWT is disabled", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    expect(screen.queryByText(/Sign in required/i)).not.toBeInTheDocument();
  });

  it("labels the API token field as optional when no static token is required", async () => {
    render(<App />);
    expect(await screen.findByLabelText("API token (optional)")).toBeInTheDocument();
  });

  it("shows an API-token-required banner when a static token is needed and none is entered", async () => {
    statusBody = { jwt_enabled: false, registration_allowed: true, auth_enabled: true };
    render(<App />);
    expect(await screen.findByText(/API token required/i)).toBeInTheDocument();
    expect(screen.getByLabelText("API token")).toBeInTheDocument();
    expect(screen.queryByLabelText("API token (optional)")).not.toBeInTheDocument();
  });

  it("focuses the token field when the API-token banner's button is clicked", async () => {
    statusBody = { jwt_enabled: false, registration_allowed: true, auth_enabled: true };
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/API token required/i);

    await user.click(screen.getByRole("button", { name: "Enter token" }));

    expect(screen.getByLabelText("API token")).toHaveFocus();
  });

  it("hides the API-token-required banner once a token is typed", async () => {
    statusBody = { jwt_enabled: false, registration_allowed: true, auth_enabled: true };
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/API token required/i);

    await user.type(screen.getByLabelText("API token"), "some-token");

    expect(screen.queryByText(/API token required/i)).not.toBeInTheDocument();
  });

  it("does not show the API-token-required banner when JWT is enabled instead", async () => {
    statusBody = { jwt_enabled: true, registration_allowed: true, auth_enabled: true };
    render(<App />);
    await screen.findByText(/Sign in required/i);
    expect(screen.queryByText(/API token required/i)).not.toBeInTheDocument();
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

  function makeFakePrintElement(tag: string) {
    return {
      tagName: tag,
      textContent: "",
      className: "",
      children: [] as { textContent: string; className: string }[],
      appendChild(child: { textContent: string; className: string }) {
        this.children.push(child);
      },
    };
  }

  it("exports a conversation as a print-ready PDF document", async () => {
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

    const fakeDoc = {
      title: "",
      head: makeFakePrintElement("head"),
      body: makeFakePrintElement("body"),
      createElement: (tag: string) => makeFakePrintElement(tag),
    };
    const fakePrintWindow = { document: fakeDoc, focus: vi.fn(), print: vi.fn() };
    const openSpy = vi
      .spyOn(window, "open")
      .mockReturnValue(fakePrintWindow as unknown as Window);

    try {
      const user = userEvent.setup();
      render(<App />);
      await screen.findByText("hello!");

      await user.selectOptions(screen.getByLabelText(/Export conversation/i), "pdf");

      expect(openSpy).toHaveBeenCalledWith("", "_blank");
      expect(fakeDoc.title).toBe("First chat");
      expect(fakeDoc.body.children[0].textContent).toBe("First chat");
      expect(fakeDoc.body.children[1].className).toBe("pdf-message user");
      expect(fakeDoc.body.children[1].children[1].textContent).toBe("hi there");
      expect(fakeDoc.body.children[2].className).toBe("pdf-message assistant");
      expect(fakeDoc.body.children[2].children[1].textContent).toBe("hello!");
      expect(fakePrintWindow.focus).toHaveBeenCalled();
      await waitFor(() => expect(fakePrintWindow.print).toHaveBeenCalled());
    } finally {
      openSpy.mockRestore();
    }
  });

  it("shows a status message when the PDF print window is blocked", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);

    try {
      const user = userEvent.setup();
      render(<App />);
      await screen.findByText("hello!");

      await user.selectOptions(screen.getByLabelText(/Export conversation/i), "pdf");

      expect(await screen.findByText(/popup blocker/i)).toBeInTheDocument();
    } finally {
      openSpy.mockRestore();
    }
  });

  it("copies the whole conversation as Markdown to the clipboard", async () => {
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
    const user = userEvent.setup();
    clipboardWriteText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    render(<App />);
    await screen.findByText("hello!");

    await user.selectOptions(screen.getByLabelText(/Export conversation/i), "copy-markdown");

    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining("# First chat"));
    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining("hi there"));
    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining("hello!"));
    expect(await screen.findByText("Copied conversation as Markdown.")).toBeInTheDocument();
  });

  it("shows a status message when copying the conversation to the clipboard fails", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    const user = userEvent.setup();
    clipboardWriteText = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockRejectedValue(new Error("denied"));
    render(<App />);
    await screen.findByText("hello!");

    await user.selectOptions(screen.getByLabelText(/Export conversation/i), "copy-markdown");

    expect(await screen.findByText(/Failed to copy to clipboard\./i)).toBeInTheDocument();
  });

  it("copies a shareable link to the whole conversation via the Export dropdown", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    const user = userEvent.setup();
    clipboardWriteText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    render(<App />);
    await screen.findByText("hello!");

    await user.selectOptions(screen.getByLabelText(/Export conversation/i), "copy-link");

    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining("c=1"));
    expect(clipboardWriteText).not.toHaveBeenCalledWith(expect.stringContaining("m="));
    expect(await screen.findByText("Copied link to this conversation.")).toBeInTheDocument();
  });

  it("shows a status message when copying the conversation link fails", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    const user = userEvent.setup();
    clipboardWriteText = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockRejectedValue(new Error("denied"));
    render(<App />);
    await screen.findByText("hello!");

    await user.selectOptions(screen.getByLabelText(/Export conversation/i), "copy-link");

    expect(await screen.findByText(/Failed to copy link to clipboard\./i)).toBeInTheDocument();
  });

  it("exports all conversations as a single JSON bundle", async () => {
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

      await user.click(screen.getByRole("button", { name: /Export all/ }));

      await waitFor(() => expect(capturedBlob).not.toBeNull());
      expect(capturedBlob?.type).toBe("application/json");
      const text = await capturedBlob?.text();
      const parsed = JSON.parse(text ?? "{}") as {
        exported_at: string;
        conversations: { conversation: { title: string }; messages: { content: string }[] }[];
      };
      expect(typeof parsed.exported_at).toBe("string");
      expect(parsed.conversations).toHaveLength(1);
      expect(parsed.conversations[0].conversation.title).toBe("First chat");
      expect(parsed.conversations[0].messages.map((m) => m.content)).toEqual([
        "hi there",
        "hello!",
      ]);
    } finally {
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
      clickSpy.mockRestore();
    }
  });

  it("includes archived conversations in the export-all bundle", async () => {
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
      await screen.findByRole("heading", { name: "First chat" });

      await user.click(screen.getByRole("button", { name: /^Archive$/ }));
      await screen.findByRole("heading", { name: "No conversation selected" });

      // Export all must include it even without toggling "Show archived" —
      // a backup that silently dropped archived conversations would defeat
      // the point of an "export everything" action.
      await user.click(screen.getByRole("button", { name: /Export all/ }));

      await waitFor(() => expect(capturedBlob).not.toBeNull());
      const text = await capturedBlob?.text();
      const parsed = JSON.parse(text ?? "{}") as {
        conversations: { conversation: { title: string; archived: boolean } }[];
      };
      expect(parsed.conversations).toHaveLength(1);
      expect(parsed.conversations[0].conversation.archived).toBe(true);
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

  it("disables the Find button when the conversation has no messages", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(screen.getByRole("button", { name: "🔎 Find" })).toBeDisabled();
  });

  it("finds text within the conversation and cycles through matches", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "tell me about apples", created_at: "2026-07-18 10:01:00" },
      { id: 2, conversation_id: 1, role: "assistant", content: "Apples are a great fruit.", created_at: "2026-07-18 10:01:04" },
      { id: 3, conversation_id: 1, role: "user", content: "what about oranges", created_at: "2026-07-18 10:01:08" },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("what about oranges");

    await user.click(screen.getByRole("button", { name: "🔎 Find" }));
    await user.type(screen.getByLabelText("Find in conversation"), "apple");

    expect(screen.getByText("1 of 2")).toBeInTheDocument();
    expect(document.querySelector('[data-message-id="1"]')).toHaveClass("find-active");
    expect(document.querySelector('[data-message-id="2"]')).not.toHaveClass("find-active");

    await user.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByText("2 of 2")).toBeInTheDocument();
    expect(document.querySelector('[data-message-id="2"]')).toHaveClass("find-active");

    await user.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByText("1 of 2")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Previous match" }));
    expect(screen.getByText("2 of 2")).toBeInTheDocument();
  });

  it("shows 'No matches' for a find query with no hits", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hello there", created_at: "2026-07-18 10:01:00" },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello there");

    await user.click(screen.getByRole("button", { name: "🔎 Find" }));
    await user.type(screen.getByLabelText("Find in conversation"), "xyz");

    expect(screen.getByText("No matches")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next match" })).toBeDisabled();
  });

  it("closes the find bar and clears the query on Escape", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hello there", created_at: "2026-07-18 10:01:00" },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello there");

    await user.click(screen.getByRole("button", { name: "🔎 Find" }));
    await user.type(screen.getByLabelText("Find in conversation"), "hello");
    expect(screen.getByText("1 of 1")).toBeInTheDocument();

    await user.keyboard("{Escape}");

    expect(screen.queryByLabelText("Find in conversation")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "🔎 Find" }));
    expect(screen.getByLabelText("Find in conversation")).toHaveValue("");
  });

  it("closes the find bar via the close button", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hello there", created_at: "2026-07-18 10:01:00" },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello there");

    await user.click(screen.getByRole("button", { name: "🔎 Find" }));
    await user.click(screen.getByRole("button", { name: "Close find" }));

    expect(screen.queryByLabelText("Find in conversation")).not.toBeInTheDocument();
  });

  it("shows a Jump to latest button once scrolled away from the bottom, and it scrolls to the end", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
    ];
    render(<App />);
    await screen.findByText("hi there");

    expect(screen.queryByRole("button", { name: /Jump to latest/ })).not.toBeInTheDocument();

    // The whole page scrolls in this layout, not the .messages pane — and
    // jsdom performs no real layout (these always read 0) — so fake a
    // "scrolled well away from the bottom" state on the document/window.
    Object.defineProperty(document.documentElement, "scrollHeight", {
      configurable: true,
      value: 2000,
    });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 500 });
    Object.defineProperty(window, "scrollY", { configurable: true, value: 0 });
    const scrollSpy = vi
      .spyOn(window.HTMLElement.prototype, "scrollIntoView")
      .mockImplementation(() => {});
    try {
      fireEvent.scroll(window);

      const jumpButton = await screen.findByRole("button", { name: /Jump to latest/ });
      await userEvent.setup().click(jumpButton);

      expect(scrollSpy).toHaveBeenCalledWith(
        expect.objectContaining({ block: "end", behavior: "smooth" }),
      );
    } finally {
      scrollSpy.mockRestore();
      delete (document.documentElement as unknown as Record<string, unknown>).scrollHeight;
      delete (window as unknown as Record<string, unknown>).innerHeight;
      delete (window as unknown as Record<string, unknown>).scrollY;
    }
  });

  it("hides the Jump to latest button once scrolled back near the bottom", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
    ];
    render(<App />);
    await screen.findByText("hi there");

    Object.defineProperty(document.documentElement, "scrollHeight", {
      configurable: true,
      value: 2000,
    });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 500 });
    Object.defineProperty(window, "scrollY", { configurable: true, value: 0 });
    try {
      fireEvent.scroll(window);
      await screen.findByRole("button", { name: /Jump to latest/ });

      Object.defineProperty(window, "scrollY", { configurable: true, value: 1490 });
      fireEvent.scroll(window);

      await waitFor(() =>
        expect(screen.queryByRole("button", { name: /Jump to latest/ })).not.toBeInTheDocument(),
      );
    } finally {
      delete (document.documentElement as unknown as Record<string, unknown>).scrollHeight;
      delete (window as unknown as Record<string, unknown>).innerHeight;
      delete (window as unknown as Record<string, unknown>).scrollY;
    }
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

  it("branches a new conversation from a message and selects it", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hi there");

    await user.click(screen.getByRole("button", { name: /Branch a new conversation from the user message/ }));

    await screen.findByRole("heading", { name: "First chat (branch)" });
    expect(capturedBranchUrl).toMatch(/\/v1\/conversations\/1\/messages\/1\/branch$/);
    expect(await screen.findByText(/Branched as "First chat \(branch\)"\./i)).toBeInTheDocument();
  });

  it("shows an error when branching fails", async () => {
    branchShouldFail = true;
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hi there");

    await user.click(screen.getByRole("button", { name: /Branch a new conversation from the user message/ }));

    expect(await screen.findByText(/Failed to branch conversation/i)).toBeInTheDocument();
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
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));
    await screen.findByRole("button", { name: /^Stop$/i });

    // Switch to the second (idle) conversation — its composer must show Ask,
    // not the first conversation's Stop button.
    await user.click(copyButton);
    await screen.findByRole("heading", { name: "First chat (copy)" });
    expect(screen.getByRole("button", { name: /^\$ Ask$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Stop$/i })).not.toBeInTheDocument();

    // Asking here must not silently no-op — a single shared stream slot
    // backs the whole app, so this should explain why rather than pretend
    // the click did nothing.
    await user.type(screen.getByLabelText(/Ask a question/i), "another question");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));
    expect(
      await screen.findByText(/Another conversation is still answering/i),
    ).toBeInTheDocument();

    // The first conversation's stream must be untouched by any of this.
    await user.click(firstChatButton);
    await screen.findByRole("heading", { name: "First chat" });
    expect(screen.getByRole("button", { name: /^Stop$/i })).toBeInTheDocument();
  });

  it("offers Undo after deleting a conversation, and restores it via Import when clicked", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
      { id: 2, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello!");

    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(await screen.findByText(/Deleted "First chat"\./i)).toBeInTheDocument();
    const undoButton = screen.getByRole("button", { name: "Undo" });

    await user.click(undoButton);

    await waitFor(() => {
      expect(capturedImportBody?.title).toBe("First chat");
    });
    const importedMessages = capturedImportBody?.messages as { content: string }[];
    expect(importedMessages.map((m) => m.content)).toEqual(["hi there", "hello!"]);
    expect(await screen.findByText(/Conversation restored\./i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
  });

  it("restores an emptied conversation via a plain create, not Import, when it had no messages", async () => {
    messages = [];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Delete" }));
    await screen.findByRole("button", { name: "Undo" });

    await user.click(screen.getByRole("button", { name: "Undo" }));

    await waitFor(() => {
      expect(capturedCreateBody?.title).toBe("First chat");
    });
    expect(capturedImportBody).toBeNull();
  });

  it("shows a status message when restoring a deleted conversation fails", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    importShouldFail = true;
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hello!");

    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(await screen.findByRole("button", { name: "Undo" }));

    expect(await screen.findByText(/Import failed: bad data/i)).toBeInTheDocument();
  });

  it("shows a status message when deleting a conversation fails", async () => {
    deleteShouldFailForId = 1;
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(await screen.findByText(/Failed to delete conversation/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
  });

  it("does not delete a conversation when confirmation is cancelled", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "First chat" })).toBeInTheDocument();
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

  it("opens the bookmarks panel from the header button", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Bookmarks" }));

    expect(await screen.findByRole("dialog", { name: "Bookmarks" })).toBeInTheDocument();
  });

  it("opens the summarize panel from the header button and shows the summary", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
    ];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("hi there");

    await user.click(screen.getByRole("button", { name: "🧾 Summarize" }));

    expect(await screen.findByRole("dialog", { name: "Summarize conversation" })).toBeInTheDocument();
    expect(await screen.findByText("A short recap of the conversation.")).toBeInTheDocument();
  });

  it("disables the Summarize button when there are no messages", async () => {
    messages = [];
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(screen.getByRole("button", { name: "🧾 Summarize" })).toBeDisabled();
  });

  it("clicking a bookmark closes the panel and scrolls to/highlights that message", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
      { id: 2, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    bookmarksResponse = [
      {
        id: 2,
        conversation_id: 1,
        conversation_title: "First chat",
        role: "assistant",
        content: "hello!",
        created_at: "2026-07-18 10:01:04",
      },
    ];
    const scrollSpy = vi
      .spyOn(window.HTMLElement.prototype, "scrollIntoView")
      .mockImplementation(() => {});
    try {
      const user = userEvent.setup();
      render(<App />);
      await screen.findByText("hello!");

      await user.click(screen.getByRole("button", { name: "Bookmarks" }));
      await screen.findByRole("dialog", { name: "Bookmarks" });
      await user.click(screen.getByText("hello!", { selector: ".bookmark-snippet" }));

      expect(screen.queryByRole("dialog", { name: "Bookmarks" })).not.toBeInTheDocument();
      await waitFor(() => {
        const target = document.querySelector('[data-message-id="2"]');
        expect(target).toHaveClass("deep-link-target");
      });
    } finally {
      scrollSpy.mockRestore();
    }
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
    const input = screen.getByLabelText(/Import a conversation.*from a JSON file/i);
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
        truncated: false,
        code_results: null,
        fact_checks: null,
        math_results: null,
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
        truncated: false,
        code_results: null,
        fact_checks: null,
        math_results: null,
      },
    ]);
    expect(screen.getByText("any good ramen spots?")).toBeInTheDocument();
    expect(screen.getByText("Try Ichiran.")).toBeInTheDocument();
    expect(await screen.findByText(/Imported "Trip to Japan"\./i)).toBeInTheDocument();
  });

  it("forwards pin, instructions, tags, tokens, cost, cached, sources, truncated, and code_results from the export file", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const exportJson = JSON.stringify({
      conversation: {
        id: 9,
        title: "Trip to Japan",
        pinned_model: "claude-sonnet-5",
        system_prompt: "Be terse.",
        tags: ["travel", "food"],
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
          truncated: true,
          code_results: [{ code: "print(1)", logs: "1", images: null }],
        },
      ],
    });
    const file = new File([exportJson], "trip-to-japan.json", { type: "application/json" });
    const input = screen.getByLabelText(/Import a conversation.*from a JSON file/i);
    await user.upload(input, file);

    await screen.findByRole("heading", { name: "Trip to Japan" });
    expect(capturedImportBody?.pinned_model).toBe("claude-sonnet-5");
    expect(capturedImportBody?.system_prompt).toBe("Be terse.");
    expect(capturedImportBody?.tags).toEqual(["travel", "food"]);
    const forwardedMessages = capturedImportBody?.messages as Record<string, unknown>[];
    expect(forwardedMessages[0]).toMatchObject({
      input_tokens: 120,
      output_tokens: 45,
      cost_usd: 0.0031,
      cached: true,
      sources: [{ title: "Ichiran", url: "https://example.com/ichiran" }],
      truncated: true,
      code_results: [{ code: "print(1)", logs: "1", images: null }],
    });
  });

  it("shows an error for a file that isn't an exported conversation", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const file = new File(["{}"], "not-an-export.json", { type: "application/json" });
    const input = screen.getByLabelText(/Import a conversation.*from a JSON file/i);
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
    const input = screen.getByLabelText(/Import a conversation.*from a JSON file/i);
    await user.upload(input, file);

    expect(await screen.findByText(/Import failed: bad data/i)).toBeInTheDocument();
  });

  it("imports every conversation in an Export all bundle", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const bundle = JSON.stringify({
      exported_at: "2026-07-26T00:00:00.000Z",
      conversations: [
        {
          conversation: { title: "Trip to Japan" },
          messages: [{ role: "user", content: "any good ramen spots?" }],
        },
        {
          conversation: { title: "Recipe ideas" },
          messages: [{ role: "user", content: "what to cook tonight?" }],
        },
      ],
    });
    const file = new File([bundle], "ai-workbench-export.json", { type: "application/json" });
    const input = screen.getByLabelText(/Import a conversation.*from a JSON file/i);
    await user.upload(input, file);

    expect(await screen.findByText(/Imported 2 conversations\./i)).toBeInTheDocument();
    expect(capturedImportBodies).toHaveLength(2);
    expect(capturedImportBodies.map((b) => b.title)).toEqual(["Trip to Japan", "Recipe ideas"]);
    expect(screen.getByText("Trip to Japan", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByText("Recipe ideas", { selector: "span" })).toBeInTheDocument();
  });

  it("keeps importing the rest of a bundle after one entry fails, and reports the split", async () => {
    importFailAfterCount = 1;
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const bundle = JSON.stringify({
      exported_at: "2026-07-26T00:00:00.000Z",
      conversations: [
        { conversation: { title: "Good one" }, messages: [{ role: "user", content: "hi" }] },
        { conversation: { title: "Bad one" }, messages: [{ role: "user", content: "hi" }] },
      ],
    });
    const file = new File([bundle], "ai-workbench-export.json", { type: "application/json" });
    const input = screen.getByLabelText(/Import a conversation.*from a JSON file/i);
    await user.upload(input, file);

    expect(
      await screen.findByText(/Imported 1 of 2 conversations \(1 failed\)\./i),
    ).toBeInTheDocument();
  });

  it("reports an empty Export all bundle as an error rather than silently doing nothing", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const bundle = JSON.stringify({ exported_at: "2026-07-26T00:00:00.000Z", conversations: [] });
    const file = new File([bundle], "ai-workbench-export.json", { type: "application/json" });
    const input = screen.getByLabelText(/Import a conversation.*from a JSON file/i);
    await user.upload(input, file);

    expect(
      await screen.findByText(/doesn't contain any conversations/i),
    ).toBeInTheDocument();
    expect(capturedImportBodies).toHaveLength(0);
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

    // The matched term is now split into its own <mark> node (see the
    // highlighting test below), so the snippet's full text is only present
    // on the .search-snippet element's combined textContent, not as one
    // plain text node — match on that instead of an exact string.
    const snippet = await screen.findByText(
      (_, element) => element?.className === "search-snippet" && element.textContent === "...volcanoes in Iceland...",
    );
    expect(snippet).toBeInTheDocument();
    expect(capturedSearchQuery).toBe("volcano");
  });

  it("highlights the matched term in a search result's title and snippet", async () => {
    searchResultsResponse = [
      {
        id: 1,
        title: "Volcano trip planning",
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
    await screen.findByText(/Iceland/);

    const marks = document.querySelectorAll(".search-results mark.search-match");
    expect(Array.from(marks).map((mark) => mark.textContent)).toEqual(["Volcano", "volcano"]);
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
    const result = await screen.findByText(
      (_, element) => element?.className === "search-snippet" && element.textContent === "...volcanoes in Iceland...",
    );

    await user.click(result);

    expect(searchBox).toHaveValue("");
    expect(screen.queryByText(/volcanoes in Iceland/)).not.toBeInTheDocument();
    expect(screen.getByText("#1")).toBeInTheDocument();
  });

  it("shows a live token/cost preview after a pause in typing", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "what is this");

    expect(await screen.findByText(/up to \$0\.0123/i)).toBeInTheDocument();
    expect(screen.getByText(/on gpt-5/i)).toBeInTheDocument();
    expect(capturedEstimateBody).toEqual({ question: "what is this", mode: "auto" });
  });

  it("clears the cost preview once the composer is emptied", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const textarea = screen.getByLabelText(/Ask a question/i);
    await user.type(textarea, "what is this");
    await screen.findByText(/up to \$0\.0123/i);

    await user.clear(textarea);
    expect(screen.queryByText(/up to \$0\.0123/i)).not.toBeInTheDocument();
  });

  it("clears the cost preview once the question is sent", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await screen.findByText(/up to \$0\.0123/i);

    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    expect(await screen.findByText("Hello world")).toBeInTheDocument();
    expect(screen.queryByText(/up to \$0\.0123/i)).not.toBeInTheDocument();
  });

  it("silently shows no preview when the estimate request fails", async () => {
    estimateShouldFail = true;
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "what is this");

    // Give the debounce+fetch a chance to run and fail; no crash, no preview.
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(screen.queryByText(/tokens/i)).not.toBeInTheDocument();
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

  it("Alt+N starts a new conversation and focuses the composer", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    fireEvent.keyDown(window, { key: "n", altKey: true });

    expect(await screen.findByRole("heading", { name: "New AI Workbench Conversation" })).toBeInTheDocument();
    expect(capturedCreateBody).toEqual({ title: "New AI Workbench Conversation" });
    expect(screen.getByLabelText(/Ask a question/i)).toHaveFocus();
  });

  it("plain 'n' (no Alt) does not trigger the new-conversation shortcut", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    fireEvent.keyDown(window, { key: "n" });

    expect(capturedCreateBody).toBeNull();
    expect(screen.getByRole("heading", { name: "First chat" })).toBeInTheDocument();
  });

  it("Alt+B opens the Bookmarks panel", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    fireEvent.keyDown(window, { key: "b", altKey: true });

    expect(await screen.findByRole("dialog", { name: "Bookmarks" })).toBeInTheDocument();
  });

  it("plain 'b' (no Alt) does not trigger the Bookmarks shortcut", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    fireEvent.keyDown(window, { key: "b" });

    expect(screen.queryByRole("dialog", { name: "Bookmarks" })).not.toBeInTheDocument();
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
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    const errorStatus = await screen.findByText(/Conversation not found/i);
    expect(errorStatus).toBeInTheDocument();
    expect(errorStatus).toHaveClass("chat-status-error");
    expect(box).toHaveValue("will fail");
  });

  it("shows a clear rate-limit message (not a bare 429) and restores the question", async () => {
    streamMode = "rate_limited";
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const box = screen.getByLabelText(/Ask a question/i);
    await user.type(box, "will be rate limited");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    const errorStatus = await screen.findByText(
      /sending requests too fast.*Rate limit exceeded: 60 per 1 minute/i,
    );
    expect(errorStatus).toBeInTheDocument();
    expect(errorStatus).toHaveClass("chat-status-error");
    expect(box).toHaveValue("will be rate limited");
  });

  it("does not mark a successful answer's status as an error", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "say hi");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

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
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

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
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

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

  it("defaults background reply notifications to off", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(
      screen.getByRole("button", { name: /Background reply notifications off/ }),
    ).toBeInTheDocument();
    expect(window.localStorage.getItem("ai_workbench_notify_enabled")).toBeNull();
  });

  it("toggles background reply notifications on, persisting the choice and requesting permission", async () => {
    vi.stubGlobal("Notification", MockNotification);
    MockNotification.permission = "default";
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: /Background reply notifications off/ }));

    expect(
      screen.getByRole("button", { name: /Background reply notifications on/ }),
    ).toBeInTheDocument();
    expect(window.localStorage.getItem("ai_workbench_notify_enabled")).toBe("true");
    expect(MockNotification.requestPermission).toHaveBeenCalled();
  });

  it("does not re-prompt for permission if it was already granted or denied", async () => {
    vi.stubGlobal("Notification", MockNotification);
    MockNotification.permission = "denied";
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: /Background reply notifications off/ }));

    expect(MockNotification.requestPermission).not.toHaveBeenCalled();
  });

  it("flashes the document title when a reply finishes while the tab is hidden and notifications are enabled", async () => {
    window.localStorage.setItem("ai_workbench_notify_enabled", "true");
    setDocumentHidden(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    await waitFor(() => expect(document.title).toBe("💬 New reply — AI Workbench"));
  });

  it("reverts the flashed title once the tab becomes visible again", async () => {
    window.localStorage.setItem("ai_workbench_notify_enabled", "true");
    setDocumentHidden(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));
    await waitFor(() => expect(document.title).toBe("💬 New reply — AI Workbench"));

    setDocumentHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));

    expect(document.title).toBe("AI Workbench");
  });

  it("does not touch the title when the tab is visible, even with notifications enabled", async () => {
    window.localStorage.setItem("ai_workbench_notify_enabled", "true");
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    await screen.findByText("Hello world");
    expect(document.title).toBe("AI Workbench");
  });

  it("does not flash the title or fire a Notification when the toggle is off, even if the tab is hidden", async () => {
    vi.stubGlobal("Notification", MockNotification);
    setDocumentHidden(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    await screen.findByText("Hello world");
    expect(document.title).toBe("AI Workbench");
    expect(createdNotifications).toHaveLength(0);
  });

  it("shows a browser Notification with the conversation title and answer when permission is granted", async () => {
    vi.stubGlobal("Notification", MockNotification);
    window.localStorage.setItem("ai_workbench_notify_enabled", "true");
    setDocumentHidden(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    await waitFor(() => expect(createdNotifications).toHaveLength(1));
    expect(createdNotifications[0]?.title).toBe("First chat");
    expect(createdNotifications[0]?.body).toBe("Hello world");
  });

  it("does not create a Notification when permission was never granted, but still flashes the title", async () => {
    vi.stubGlobal("Notification", MockNotification);
    MockNotification.permission = "denied";
    window.localStorage.setItem("ai_workbench_notify_enabled", "true");
    setDocumentHidden(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    await waitFor(() => expect(document.title).toBe("💬 New reply — AI Workbench"));
    expect(createdNotifications).toHaveLength(0);
  });

  class MockAudioContext {
    currentTime = 0;
    close = vi.fn(async () => {});
    createOscillator() {
      const oscillator = {
        type: "",
        frequency: { value: 0 },
        connect: vi.fn(),
        start: vi.fn(),
        stop: vi.fn(),
        onended: null as (() => void) | null,
      };
      createdOscillators.push(oscillator);
      return oscillator;
    }
    createGain() {
      return {
        gain: { setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() },
        connect: vi.fn(),
      };
    }
  }
  let createdOscillators: { start: ReturnType<typeof vi.fn> }[];

  it("does not show the sound toggle when background notifications are off", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(screen.queryByRole("button", { name: /Notification sound/ })).not.toBeInTheDocument();
  });

  it("shows the sound toggle once notifications are enabled, off by default", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: /Background reply notifications off/ }));

    expect(
      screen.getByRole("button", { name: "Notification sound off. Click to turn on." }),
    ).toBeInTheDocument();
  });

  it("toggles the sound preference, persisting it to localStorage", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: /Background reply notifications off/ }));
    await user.click(screen.getByRole("button", { name: "Notification sound off. Click to turn on." }));

    expect(
      screen.getByRole("button", { name: "Notification sound on. Click to turn off." }),
    ).toBeInTheDocument();
    expect(window.localStorage.getItem("ai_workbench_notify_sound_enabled")).toBe("true");
  });

  it("hides the sound toggle again after notifications are turned back off", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const notifyToggle = () => screen.getByRole("button", { name: /Background reply notifications/ });
    await user.click(notifyToggle());
    await user.click(notifyToggle());

    expect(screen.queryByRole("button", { name: /Notification sound/ })).not.toBeInTheDocument();
  });

  it("plays a beep when a reply finishes hidden, notifications and sound are both on", async () => {
    createdOscillators = [];
    vi.stubGlobal("AudioContext", MockAudioContext);
    window.localStorage.setItem("ai_workbench_notify_enabled", "true");
    window.localStorage.setItem("ai_workbench_notify_sound_enabled", "true");
    setDocumentHidden(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    await waitFor(() => expect(createdOscillators).toHaveLength(1));
    expect(createdOscillators[0].start).toHaveBeenCalled();
  });

  it("does not play a beep when sound is off, even with notifications on and the tab hidden", async () => {
    createdOscillators = [];
    vi.stubGlobal("AudioContext", MockAudioContext);
    window.localStorage.setItem("ai_workbench_notify_enabled", "true");
    setDocumentHidden(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    await waitFor(() => expect(document.title).toBe("💬 New reply — AI Workbench"));
    expect(createdOscillators).toHaveLength(0);
  });

  it("does not throw when sound is on but the browser has no AudioContext available", async () => {
    window.localStorage.setItem("ai_workbench_notify_enabled", "true");
    window.localStorage.setItem("ai_workbench_notify_sound_enabled", "true");
    setDocumentHidden(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    // No AudioContext in jsdom by default — the title flash still succeeds,
    // proving the missing-API branch degrades silently instead of throwing.
    await waitFor(() => expect(document.title).toBe("💬 New reply — AI Workbench"));
  });

  it("shows a $ cost legend explaining the paid-action marker", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    const legend = screen.getByRole("button", { name: "What does the $ marker mean?" });
    expect(legend).toHaveAttribute("title", "$ = this action uses paid API tokens/credits.");
  });

  it("opens the keyboard shortcuts help from its sidebar button", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Keyboard shortcuts" }));

    expect(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
  });

  it("opens the keyboard shortcuts help on '?' when not typing in a field", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.keyboard("?");

    expect(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
  });

  it("does not open the shortcuts help when '?' is typed into the composer", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "what now?");

    expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).not.toBeInTheDocument();
  });

  it("closes the shortcuts help on Escape", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Keyboard shortcuts" }));
    await screen.findByRole("dialog", { name: "Keyboard shortcuts" });

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).not.toBeInTheDocument();
  });

  it("shows no budget warning when no per-owner cap is configured", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(screen.queryByText(/left of your.*daily budget/i)).not.toBeInTheDocument();
  });

  it("shows no budget warning when the caller still has plenty of room", async () => {
    usageBudgetOverride = { daily_budget_per_owner_usd: 1, owner_remaining_usd: 0.5 };
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    // Asking a question is a real async boundary that guarantees the
    // post-mount /v1/usage check (fired in the same effect as this one) has
    // long since resolved by the time the answer lands.
    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));
    await screen.findByText("Hello world");

    expect(screen.queryByText(/left of your.*daily budget/i)).not.toBeInTheDocument();
  });

  it("shows a budget warning on load when remaining room is 15% or less of the cap", async () => {
    usageBudgetOverride = { daily_budget_per_owner_usd: 1, owner_remaining_usd: 0.1 };
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(
      await screen.findByText("⚠️ Only $0.1000 left of your $1.0000 daily budget today."),
    ).toBeInTheDocument();
  });

  it("shows a budget warning when remaining room has hit zero", async () => {
    usageBudgetOverride = { daily_budget_per_owner_usd: 1, owner_remaining_usd: 0 };
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(await screen.findByText(/Only \$0 left/)).toBeInTheDocument();
  });

  it("re-checks the budget warning after an answer completes", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    expect(screen.queryByText(/left of your.*daily budget/i)).not.toBeInTheDocument();

    // The answer's cost pushed the caller close to their cap — simulated by
    // flipping the mocked /v1/usage response before the post-answer re-check.
    usageBudgetOverride = { daily_budget_per_owner_usd: 1, owner_remaining_usd: 0.05 };
    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    expect(
      await screen.findByText("⚠️ Only $0.0500 left of your $1.0000 daily budget today."),
    ).toBeInTheDocument();
  });

  it("shows today's spend in the sidebar on load", async () => {
    usageTodayOverride = { today_usd: 0.42, daily_budget_usd: null };
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(await screen.findByText(/\$0\.4200 today/)).toBeInTheDocument();
  });

  it("shows the daily cap alongside today's spend when one is configured", async () => {
    usageTodayOverride = { today_usd: 0.42, daily_budget_usd: null };
    usageBudgetOverride = { daily_budget_per_owner_usd: 5, owner_remaining_usd: 4.58 };
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(await screen.findByText(/\$0\.4200 \/ \$5\.0000 today/)).toBeInTheDocument();
  });

  it("shows the 🛟 saved indicator when the response cache has avoided cost today", async () => {
    usageTodayOverride = {
      today_usd: 0.42,
      daily_budget_usd: null,
      avoided_cost_today_usd: 0.05,
    };
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(await screen.findByText(/\$0\.0500 saved today/)).toBeInTheDocument();
  });

  it("hides the 🛟 saved indicator when nothing has been avoided today", async () => {
    usageTodayOverride = {
      today_usd: 0.42,
      daily_budget_usd: null,
      avoided_cost_today_usd: 0,
    };
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText(/\$0\.4200 today/);

    expect(screen.queryByText(/saved today/)).not.toBeInTheDocument();
  });

  it("refreshes today's spend after an answer completes", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText(/\$0 today/);

    usageTodayOverride = { today_usd: 0.02, daily_budget_usd: null };
    await user.type(screen.getByLabelText(/Ask a question/i), "hi there");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));

    expect(await screen.findByText(/\$0\.0200 today/)).toBeInTheDocument();
  });

  function seedBulkConversations() {
    bulkExtraConversations = [
      { id: 30, title: "Second chat", owner: null, pinned_model: null, system_prompt: null, favorite: false, archived: false, created_at: "2026-07-21 09:00:00", updated_at: "2026-07-21 09:00:00" },
      { id: 31, title: "Third chat", owner: null, pinned_model: null, system_prompt: null, favorite: false, archived: false, created_at: "2026-07-22 09:00:00", updated_at: "2026-07-22 09:00:00" },
    ];
  }

  it("reflects the selected conversation in the URL's ?c= query param", async () => {
    seedBulkConversations();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    expect(window.location.search).toBe("?c=1");

    await user.click(screen.getByText("Second chat"));

    await screen.findByRole("heading", { name: "Second chat" });
    expect(window.location.search).toBe("?c=30");
  });

  it("selects the conversation named by ?c= on load instead of the default pick", async () => {
    seedBulkConversations();
    window.history.replaceState(null, "", "/?c=30");
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Second chat" })).toBeInTheDocument();
  });

  it("falls back to the default pick when ?c= names a conversation that doesn't exist", async () => {
    seedBulkConversations();
    window.history.replaceState(null, "", "/?c=999999");
    render(<App />);
    expect(await screen.findByRole("heading", { name: "First chat" })).toBeInTheDocument();
  });

  it("scrolls to and highlights the message named by &m= once it loads, then strips it from the URL", async () => {
    window.history.replaceState(null, "", "/?c=1&m=2");
    messages = [
      { id: 1, conversation_id: 1, role: "user", content: "hi there", created_at: "2026-07-18 10:01:00" },
      { id: 2, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    const scrollSpy = vi
      .spyOn(window.HTMLElement.prototype, "scrollIntoView")
      .mockImplementation(() => {});
    try {
      render(<App />);
      await screen.findByText("hello!");

      await waitFor(() => {
        const target = document.querySelector('[data-message-id="2"]');
        expect(target).toHaveClass("deep-link-target");
      });
      expect(window.location.search).toBe("?c=1");
    } finally {
      scrollSpy.mockRestore();
    }
  });

  it("does not highlight anything when there's no &m= in the URL", async () => {
    messages = [
      { id: 1, conversation_id: 1, role: "assistant", content: "hello!", created_at: "2026-07-18 10:01:04" },
    ];
    render(<App />);
    await screen.findByText("hello!");
    expect(document.querySelector(".deep-link-target")).not.toBeInTheDocument();
  });

  it("ArrowDown moves the selection to the next conversation in the list", async () => {
    seedBulkConversations();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    const firstChatButton = screen.getByText("First chat", { selector: "span" }).closest("button");
    if (!firstChatButton) throw new Error("First chat sidebar button not found");
    firstChatButton.focus();
    await user.keyboard("{ArrowDown}");

    expect(await screen.findByRole("heading", { name: "Second chat" })).toBeInTheDocument();
    // The focus move is deferred a tick (requestAnimationFrame) after the
    // click handler updates selection state — wait for it rather than
    // asserting synchronously, which was flaky under CI's timing.
    await waitFor(() =>
      expect(document.activeElement).toHaveAttribute("data-conversation-id", "30"),
    );
  });

  it("ArrowUp moves the selection to the previous conversation in the list", async () => {
    seedBulkConversations();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    const secondChatButton = screen.getByText("Second chat", { selector: "span" }).closest("button");
    if (!secondChatButton) throw new Error("Second chat sidebar button not found");
    secondChatButton.focus();
    await user.keyboard("{ArrowUp}");

    expect(await screen.findByRole("heading", { name: "First chat" })).toBeInTheDocument();
    await waitFor(() =>
      expect(document.activeElement).toHaveAttribute("data-conversation-id", "1"),
    );
  });

  it("clamps at the ends of the list instead of wrapping", async () => {
    seedBulkConversations();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Third chat");

    const thirdChatButton = screen.getByText("Third chat", { selector: "span" }).closest("button");
    if (!thirdChatButton) throw new Error("Third chat sidebar button not found");
    thirdChatButton.focus();
    await user.keyboard("{ArrowDown}");

    expect(await screen.findByRole("heading", { name: "Third chat" })).toBeInTheDocument();
    expect(document.activeElement).toHaveAttribute("data-conversation-id", "31");
  });

  it("Home and End jump to the first and last conversation", async () => {
    seedBulkConversations();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    const firstChatButton = screen.getByText("First chat", { selector: "span" }).closest("button");
    if (!firstChatButton) throw new Error("First chat sidebar button not found");
    firstChatButton.focus();
    await user.keyboard("{End}");
    expect(await screen.findByRole("heading", { name: "Third chat" })).toBeInTheDocument();

    await user.keyboard("{Home}");
    expect(await screen.findByRole("heading", { name: "First chat" })).toBeInTheDocument();
  });

  it("enters select mode showing a checkbox per conversation, with none selected initially", async () => {
    seedBulkConversations();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    expect(screen.queryByLabelText('Select "First chat"')).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Select" }));

    expect(screen.getByLabelText('Select "First chat"')).not.toBeChecked();
    expect(screen.getByLabelText('Select "Second chat"')).not.toBeChecked();
    expect(screen.getByText("0 selected")).toBeInTheDocument();
  });

  it("cancelling select mode clears the checkboxes and the selection count", async () => {
    seedBulkConversations();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    await user.click(screen.getByRole("button", { name: "Select" }));
    await user.click(screen.getByLabelText('Select "First chat"'));
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Cancel select" }));
    expect(screen.queryByText(/selected$/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Select" }));
    expect(screen.getByText("0 selected")).toBeInTheDocument();
  });

  it("adds a tag to every selected conversation", async () => {
    seedBulkConversations();
    vi.spyOn(window, "prompt").mockReturnValue("work");
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    await user.click(screen.getByRole("button", { name: "Select" }));
    await user.click(screen.getByLabelText('Select "Second chat"'));
    await user.click(screen.getByLabelText('Select "Third chat"'));
    await user.click(screen.getByRole("button", { name: "Add tag" }));

    await waitFor(() => expect(screen.getByText(/Tagged 2 conversations\./)).toBeInTheDocument());
    expect(screen.getAllByText("work", { selector: ".tag-chip" })).toHaveLength(2);
  });

  it("merges the bulk tag with each conversation's existing tags instead of replacing them", async () => {
    seedBulkConversations();
    bulkExtraConversations[0].tags = ["personal"];
    vi.spyOn(window, "prompt").mockReturnValue("work");
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    await user.click(screen.getByRole("button", { name: "Select" }));
    await user.click(screen.getByLabelText('Select "Second chat"'));
    await user.click(screen.getByRole("button", { name: "Add tag" }));

    await waitFor(() => expect(screen.getByText(/Tagged 1 conversation\./)).toBeInTheDocument());
    expect(screen.getByText("personal", { selector: ".tag-chip" })).toBeInTheDocument();
    expect(screen.getByText("work", { selector: ".tag-chip" })).toBeInTheDocument();
  });

  it("does nothing when the Add tag prompt is cancelled", async () => {
    seedBulkConversations();
    vi.spyOn(window, "prompt").mockReturnValue(null);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    await user.click(screen.getByRole("button", { name: "Select" }));
    await user.click(screen.getByLabelText('Select "Second chat"'));
    await user.click(screen.getByRole("button", { name: "Add tag" }));

    expect(capturedTagsBody).toBeNull();
  });

  it("reports a partial failure when tagging one of several selected conversations fails", async () => {
    seedBulkConversations();
    tagsShouldFailForId = 31;
    vi.spyOn(window, "prompt").mockReturnValue("work");
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    await user.click(screen.getByRole("button", { name: "Select" }));
    await user.click(screen.getByLabelText('Select "Second chat"'));
    await user.click(screen.getByLabelText('Select "Third chat"'));
    await user.click(screen.getByRole("button", { name: "Add tag" }));

    await waitFor(() =>
      expect(screen.getByText(/Tagged 1 of 2 conversations \(1 failed\)\./)).toBeInTheDocument(),
    );
  });

  it("bulk-archives the selected conversations and hides them from the default list", async () => {
    seedBulkConversations();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    await user.click(screen.getByRole("button", { name: "Select" }));
    await user.click(screen.getByLabelText('Select "Second chat"'));
    await user.click(screen.getByLabelText('Select "Third chat"'));
    await user.click(screen.getByRole("button", { name: "Archive selected" }));

    await waitFor(() =>
      expect(screen.getByText(/Archived 2 conversations\./)).toBeInTheDocument(),
    );
    expect(screen.queryByText("Second chat")).not.toBeInTheDocument();
    expect(screen.queryByText("Third chat")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "First chat" })).toBeInTheDocument();
  });

  it("bulk-deletes the selected conversations after confirmation", async () => {
    seedBulkConversations();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    await user.click(screen.getByRole("button", { name: "Select" }));
    await user.click(screen.getByLabelText('Select "Second chat"'));
    await user.click(screen.getByRole("button", { name: "Delete selected" }));

    await waitFor(() => expect(screen.getByText(/Deleted 1 conversation\./)).toBeInTheDocument());
    expect(screen.queryByText("Second chat")).not.toBeInTheDocument();
    expect(screen.getByText("Third chat")).toBeInTheDocument();
  });

  it("does not delete anything when the bulk-delete confirmation is declined", async () => {
    seedBulkConversations();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    await user.click(screen.getByRole("button", { name: "Select" }));
    await user.click(screen.getByLabelText('Select "Second chat"'));
    await user.click(screen.getByRole("button", { name: "Delete selected" }));

    expect(screen.getByText("Second chat")).toBeInTheDocument();
  });

  it("reports a partial failure when one of several bulk-archived conversations fails", async () => {
    seedBulkConversations();
    archiveShouldFailForId = 31;
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    await user.click(screen.getByRole("button", { name: "Select" }));
    await user.click(screen.getByLabelText('Select "Second chat"'));
    await user.click(screen.getByLabelText('Select "Third chat"'));
    await user.click(screen.getByRole("button", { name: "Archive selected" }));

    await waitFor(() =>
      expect(screen.getByText(/Archived 1 of 2 conversations \(1 failed\)\./)).toBeInTheDocument(),
    );
    expect(screen.queryByText("Second chat")).not.toBeInTheDocument();
    expect(screen.getByText("Third chat")).toBeInTheDocument();
  });

  it("exports only the selected conversations as one JSON bundle", async () => {
    seedBulkConversations();
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
      await screen.findByRole("heading", { name: "First chat" });
      await screen.findByText("Second chat");

      await user.click(screen.getByRole("button", { name: "Select" }));
      await user.click(screen.getByLabelText('Select "Second chat"'));
      await user.click(screen.getByRole("button", { name: "Export selected" }));

      await waitFor(() => expect(capturedBlob).not.toBeNull());
      expect(capturedBlob?.type).toBe("application/json");
      const text = await capturedBlob?.text();
      const parsed = JSON.parse(text ?? "{}") as {
        exported_at: string;
        conversations: { conversation: { title: string } }[];
      };
      expect(typeof parsed.exported_at).toBe("string");
      expect(parsed.conversations).toHaveLength(1);
      expect(parsed.conversations[0].conversation.title).toBe("Second chat");
      expect(await screen.findByText("Exported 1 conversation.")).toBeInTheDocument();
    } finally {
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
      clickSpy.mockRestore();
    }
  });

  it("does nothing when Export selected is clicked with no conversations checked", async () => {
    seedBulkConversations();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    await user.click(screen.getByRole("button", { name: "Select" }));

    expect(screen.getByRole("button", { name: "Export selected" })).toBeDisabled();
  });

  it("shows the plain Tags button with no count when a conversation has no tags", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(screen.getByRole("button", { name: "Tags" })).toBeInTheDocument();
  });

  it("sets tags via the Tags prompt and shows them as chips in the sidebar", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "prompt").mockReturnValue("work, urgent");
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Tags" }));

    expect(capturedTagsBody).toEqual({ tags: ["work", "urgent"] });
    expect(await screen.findByText("work", { selector: ".tag-chip" })).toBeInTheDocument();
    expect(screen.getByText("urgent", { selector: ".tag-chip" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tags (2)" })).toBeInTheDocument();
  });

  it("does not change tags when the prompt is cancelled", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "prompt").mockReturnValue(null);
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Tags" }));

    expect(capturedTagsBody).toBeNull();
  });

  it("clears tags when the prompt is submitted empty", async () => {
    const user = userEvent.setup();
    tagsState = ["work"];
    vi.spyOn(window, "prompt").mockReturnValue("");
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("work", { selector: ".tag-chip" });

    await user.click(screen.getByRole("button", { name: "Tags (1)" }));

    expect(capturedTagsBody).toEqual({ tags: [] });
    await waitFor(() =>
      expect(screen.queryByText("work", { selector: ".tag-chip" })).not.toBeInTheDocument(),
    );
  });

  it("shows a status message when updating tags fails", async () => {
    const user = userEvent.setup();
    tagsShouldFail = true;
    vi.spyOn(window, "prompt").mockReturnValue("work");
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Tags" }));

    expect(await screen.findByText(/Failed to update tags/i)).toBeInTheDocument();
  });

  it("filters the sidebar by tag using the tag filter dropdown", async () => {
    seedBulkConversations();
    bulkExtraConversations[0].tags = ["work"];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    await user.selectOptions(screen.getByLabelText("Filter conversations by tag"), "work");

    expect(screen.getByText("Second chat")).toBeInTheDocument();
    expect(screen.queryByText("Third chat")).not.toBeInTheDocument();
    // "First chat" has no tags, so it drops out of the filtered sidebar list —
    // check the sidebar row specifically, since the chat header (an h2, not
    // this button) still shows the previously-selected conversation's title.
    expect(screen.queryByRole("button", { name: /^First chat/ })).not.toBeInTheDocument();
  });

  it("hides the tag filter dropdown when no conversation has any tags", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    expect(screen.queryByLabelText("Filter conversations by tag")).not.toBeInTheDocument();
  });

  it("sorts the conversation list by name and back to recent", async () => {
    bulkExtraConversations = [
      { id: 30, title: "Zebra chat", owner: null, pinned_model: null, system_prompt: null, favorite: false, archived: false, created_at: "2026-07-21 09:00:00", updated_at: "2026-07-21 09:00:00" },
      { id: 31, title: "Mango chat", owner: null, pinned_model: null, system_prompt: null, favorite: false, archived: false, created_at: "2026-07-22 09:00:00", updated_at: "2026-07-22 09:00:00" },
    ];
    const user = userEvent.setup();
    const { container } = render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Zebra chat");

    const idsInOrder = () =>
      Array.from(container.querySelectorAll(".conversation-list [data-conversation-id]")).map((el) =>
        el.getAttribute("data-conversation-id"),
      );
    expect(idsInOrder()).toEqual(["1", "30", "31"]);

    await user.selectOptions(screen.getByLabelText("Sort conversations"), "name");
    // First chat (1) < Mango chat (31) < Zebra chat (30)
    expect(idsInOrder()).toEqual(["1", "31", "30"]);

    await user.selectOptions(screen.getByLabelText("Sort conversations"), "recent");
    expect(idsInOrder()).toEqual(["1", "30", "31"]);
  });

  it("shows no 'Back' control until a second conversation has been visited", async () => {
    seedBulkConversations();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    expect(screen.queryByRole("button", { name: /^← Back to/ })).not.toBeInTheDocument();
  });

  it("switches back to the last conversation and flips on a second click", async () => {
    seedBulkConversations();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    const secondChatButton = screen.getByText("Second chat", { selector: "span" }).closest("button");
    if (!secondChatButton) throw new Error("Second chat sidebar button not found");

    await user.click(secondChatButton);
    await screen.findByRole("heading", { name: "Second chat" });

    const backButton = await screen.findByRole("button", { name: '← Back to "First chat"' });
    await user.click(backButton);

    expect(await screen.findByRole("heading", { name: "First chat" })).toBeInTheDocument();
    // Clicking Back again flips to the conversation just left, like Alt+Tab.
    expect(
      await screen.findByRole("button", { name: '← Back to "Second chat"' }),
    ).toBeInTheDocument();
  });

  it("shows a message-count badge only for conversations that have messages", async () => {
    bulkExtraConversations = [
      {
        id: 30,
        title: "Second chat",
        owner: null,
        pinned_model: null,
        system_prompt: null,
        favorite: false,
        archived: false,
        created_at: "2026-07-21 09:00:00",
        updated_at: "2026-07-21 09:00:00",
        message_count: 4,
      },
    ];
    const { container } = render(<App />);
    await screen.findByText("Second chat");
    const secondChatRow = container.querySelector('[data-conversation-id="30"]');
    const firstChatRow = container.querySelector('[data-conversation-id="1"]');
    if (!secondChatRow || !firstChatRow) throw new Error("conversation rows not found");

    expect(secondChatRow.querySelector(".message-count-badge")).toHaveTextContent("4");
    // First chat's mocked fixture carries no message_count, so no badge.
    expect(firstChatRow.querySelector(".message-count-badge")).not.toBeInTheDocument();
  });

  it("shows onboarding hints instead of the plain empty state on a fresh install", async () => {
    deletedConversationIds.add(1);
    render(<App />);

    expect(
      await screen.findByText("Welcome to AI Workbench.", { exact: false }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, element) =>
          element?.tagName === "LI" &&
          (element.textContent ?? "").includes("click Create to start your first conversation"),
      ),
    ).toBeInTheDocument();
  });

  it("shows only favorited conversations when 'Favorites only' is checked", async () => {
    seedBulkConversations();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    await user.click(screen.getByRole("button", { name: 'Favorite "First chat"' }));
    await screen.findByRole("button", { name: 'Unfavorite "First chat"' });

    await user.click(screen.getByLabelText("★ Favorites only"));

    expect(screen.getByRole("button", { name: /^First chat/ })).toBeInTheDocument();
    expect(screen.queryByText("Second chat")).not.toBeInTheDocument();
    expect(screen.queryByText("Third chat")).not.toBeInTheDocument();
  });

  it("unchecking 'Favorites only' restores the full conversation list", async () => {
    seedBulkConversations();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    const favoritesOnlyCheckbox = screen.getByLabelText("★ Favorites only");
    await user.click(favoritesOnlyCheckbox);
    expect(screen.queryByText("Second chat")).not.toBeInTheDocument();

    await user.click(favoritesOnlyCheckbox);
    expect(screen.getByText("Second chat")).toBeInTheDocument();
    expect(screen.getByText("Third chat")).toBeInTheDocument();
  });

  it("combines the tag filter and 'Favorites only' filter", async () => {
    seedBulkConversations();
    bulkExtraConversations[0].tags = ["work"];
    bulkExtraConversations[1].tags = ["work"];
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await screen.findByText("Second chat");

    await user.click(screen.getByRole("button", { name: 'Favorite "Second chat"' }));
    await screen.findByRole("button", { name: 'Unfavorite "Second chat"' });

    await user.selectOptions(screen.getByLabelText("Filter conversations by tag"), "work");
    await user.click(screen.getByLabelText("★ Favorites only"));

    expect(screen.getByText("Second chat")).toBeInTheDocument();
    expect(screen.queryByText("Third chat")).not.toBeInTheDocument();
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

  it("auto-saves a draft when switching away, and restores it on switching back", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.click(screen.getByRole("button", { name: "Duplicate" }));
    await screen.findByRole("heading", { name: "First chat (copy)" });

    const firstChatButton = screen.getByText("First chat", { selector: "span" }).closest("button");
    const copyButton = screen.getByText("First chat (copy)", { selector: "span" }).closest("button");
    if (!firstChatButton || !copyButton) throw new Error("sidebar buttons not found");

    await user.click(firstChatButton);
    await screen.findByRole("heading", { name: "First chat" });
    await user.type(screen.getByLabelText(/Ask a question/i), "half-typed draft");

    // Switch away without sending — must neither vanish nor leak into the
    // OTHER conversation's composer.
    await user.click(copyButton);
    await screen.findByRole("heading", { name: "First chat (copy)" });
    expect(screen.getByLabelText(/Ask a question/i)).toHaveValue("");

    // Switch back — the draft must reappear.
    await user.click(firstChatButton);
    await screen.findByRole("heading", { name: "First chat" });
    expect(screen.getByLabelText(/Ask a question/i)).toHaveValue("half-typed draft");
  });

  it("restores a saved draft after a full remount (simulating a page reload)", async () => {
    const user = userEvent.setup();
    const first = render(<App />);
    await screen.findByRole("heading", { name: "First chat" });
    await user.type(screen.getByLabelText(/Ask a question/i), "reload me");

    // Wait past the 400ms debounce so the draft is actually persisted before
    // the "reload" (unmount/remount), not just sitting in React state. A
    // wider margin than the debounce itself (not just barely past it) since
    // the cost-preview debounce fires on the same `question` change and adds
    // its own async fetch — under CI's slower/shared runners, 500ms cut it
    // close enough to occasionally flake.
    await new Promise((resolve) => setTimeout(resolve, 900));
    first.unmount();

    render(<App />);
    expect(await screen.findByRole("heading", { name: "First chat" })).toBeInTheDocument();
    expect(await screen.findByLabelText(/Ask a question/i)).toHaveValue("reload me");
  });

  it("clears the draft once the message is actually sent", async () => {
    const user = userEvent.setup();
    const first = render(<App />);
    await screen.findByRole("heading", { name: "First chat" });

    await user.type(screen.getByLabelText(/Ask a question/i), "will be sent");
    await user.click(screen.getByRole("button", { name: /^\$ Ask$/i }));
    await screen.findByText("Hello world");

    // Wait past the debounce so the now-empty question is what gets
    // persisted, not stale leftover text from before sending.
    await new Promise((resolve) => setTimeout(resolve, 500));
    first.unmount();

    render(<App />);
    expect(await screen.findByRole("heading", { name: "First chat" })).toBeInTheDocument();
    expect(screen.getByLabelText(/Ask a question/i)).toHaveValue("");
  });
});
