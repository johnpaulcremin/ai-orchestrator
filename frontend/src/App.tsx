import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { extractSseFrames, type SseFrame } from "./sse";
import { formatTimestamp, formatCost, downloadTextFile } from "./format";
import { ErrorBoundary } from "./ErrorBoundary";
import { ChangePassword } from "./ChangePassword";
import { ShortcutsHelp } from "./ShortcutsHelp";
import { Sidebar } from "./Sidebar";
import { MessageList } from "./MessageList";
import { Composer } from "./Composer";
import { useTheme } from "./useTheme";
import { useNotificationPreferences } from "./useNotificationPreferences";
import { HeaderOverflowMenu } from "./HeaderOverflowMenu";
import { loadDraftMap, saveDraftMap, setDraft } from "./drafts";
import { buildConversationMarkdown } from "./exportMarkdown";
import { getSpeechRecognitionConstructor, type SpeechRecognitionLike } from "./speechRecognition";

// Lazily loaded: each is a whole modal panel behind an explicit open action
// (Settings/Compare/Usage/Bookmarks/Templates/Summarize buttons), never
// needed for the initial chat view, so keeping them out of the main bundle
// shrinks first-load JS without touching what's visible on first paint.
// ShortcutsHelp stays a regular import -- small enough that splitting it out
// wouldn't move the needle, and it's opened via the '?' key for an
// instant-feeling reference popup where even one chunk-fetch tick would be
// noticeable.
const Bookmarks = lazy(() => import("./Bookmarks").then((m) => ({ default: m.Bookmarks })));
const Templates = lazy(() => import("./Templates").then((m) => ({ default: m.Templates })));
const Library = lazy(() => import("./Library").then((m) => ({ default: m.Library })));
const Summarize = lazy(() => import("./Summarize").then((m) => ({ default: m.Summarize })));
const Compare = lazy(() => import("./Compare").then((m) => ({ default: m.Compare })));
const Settings = lazy(() => import("./Settings").then((m) => ({ default: m.Settings })));
const Usage = lazy(() => import("./Usage").then((m) => ({ default: m.Usage })));
const Share = lazy(() => import("./Share").then((m) => ({ default: m.Share })));
import type {
  Mode,
  Conversation,
  SearchResult,
  Source,
  PendingAction,
  CodeResult,
  FactCheckResult,
  AcademicResult,
  MathResult,
  LibrarySource,
  WorkflowStep,
  ActionStatus,
  AudioAttachment,
  FileAttachment,
  Message,
  StreamState,
} from "./types";
import "./App.css";

const MAX_ATTACHED_IMAGES = 4;
const MAX_ATTACHED_FILES = 4;
const XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
// Mirrors the backend's FileAttachment mime allowlist (schemas.py). Kept as
// the real (non-normalized) mime for each — unlike .csv below, an .xlsx
// attachment is sent as-is (the backend converts it server-side; see
// app/spreadsheet_ingestion.py), so its real mime has to survive, not get
// normalized away to text/plain the way an unrecognized mime does.
const ACCEPTED_FILE_MIMES = new Set(["application/pdf", "text/plain", XLSX_MIME]);
// Mirrors the backend's TranscribeRequest mime allowlist (schemas.py), in
// preference order — the first one the browser's MediaRecorder supports wins.
const PREFERRED_AUDIO_MIME_TYPES = ["audio/webm", "audio/ogg", "audio/mp4", "audio/wav"];

// Meeting/voice-memo attachment (distinct from the mic-button dictation flow
// above, which hits /v1/transcribe directly): mirrors the backend's
// AudioAttachment mime allowlist and _MAX_INPUT_AUDIO/_MAX_INPUT_AUDIO_CHARS
// (schemas.py) — a clip over the transcription API's real 25MB limit is
// rejected with a clear message rather than chunked (v1 scope decision, see
// app/audio_ingestion.py's module docstring).
const MAX_ATTACHED_AUDIO = 2;
const MAX_AUDIO_BYTES = 25 * 1024 * 1024;
const ACCEPTED_AUDIO_MIMES = new Set([
  "audio/webm",
  "audio/wav",
  "audio/mp3",
  "audio/mpeg",
  "audio/mp4",
  "audio/m4a",
  "audio/ogg",
]);

const API_BASE = "/api";
const TOKEN_STORAGE_KEY = "ai_workbench_token";

const BASE_DOCUMENT_TITLE = "AI Workbench";
// Used only to label the search shortcut hint (⌘K vs Ctrl+K); the shortcut
// itself listens for either metaKey or ctrlKey regardless of platform.
const IS_MAC = typeof navigator !== "undefined" && /Mac|iPod|iPhone|iPad/.test(navigator.platform);

function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedConversationId, setSelectedConversationId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [title, setTitle] = useState("New AI Workbench Conversation");
  const [question, setQuestion] = useState("");
  const [attachedImages, setAttachedImages] = useState<string[]>([]);
  const [attachedFiles, setAttachedFiles] = useState<FileAttachment[]>([]);
  const [attachedAudio, setAttachedAudio] = useState<AudioAttachment[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [mode, setMode] = useState<Mode>("auto");
  const [researchMode, setResearchMode] = useState(false);
  // Live worst-case token/cost preview for the question currently being
  // typed — same estimate the DAILY_BUDGET_USD gate itself uses on dispatch
  // (see backend budget.estimate_worst_case), so this is never a second,
  // possibly-inconsistent number. null while empty/not-yet-estimated.
  const [costPreview, setCostPreview] = useState<{
    model: string;
    input_tokens_estimate: number;
    output_tokens_estimate: number;
    cost_usd_estimate: number | null;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState("Ready");
  const [statusIsError, setStatusIsError] = useState(false);
  // A question was persisted but got no answer (budget refusal, truncated
  // reasoning call, etc.) — the status line above already turns red for this
  // (see showStatus), but it's easy to miss since it's outside the message
  // thread itself. This drives an inline notice under the dangling user turn.
  const [unansweredNotice, setUnansweredNotice] = useState<{
    conversationId: number;
    note: string;
  } | null>(null);
  // The streaming bubble's text updates many times a second and isn't
  // itself a live region (announcing every delta would be unusable), so a
  // screen reader user got no indication an answer ever arrived. This holds
  // the complete answer, announced once when streaming finishes.
  const [srAnswerAnnouncement, setSrAnswerAnnouncement] = useState("");
  // Every status update goes through here so error-styling can never linger
  // from a previous message: routine telemetry and hard failures used to
  // render identically (same grey 14px text), so a budget refusal or a
  // failed request was easy to mistake for normal routing telemetry.
  function showStatus(text: string, opts?: { error?: boolean }) {
    setStatus(text);
    setStatusIsError(Boolean(opts?.error));
  }
  // A snapshot of the just-deleted conversation, kept just long enough to
  // offer Undo — restored via Import (fresh id, same content) since the
  // DELETE itself is a real, permanent removal. null once there's nothing to
  // undo (never restored, expired, or already used).
  const [undoDelete, setUndoDelete] = useState<{
    title: string;
    payload: {
      title: string;
      pinned_model: string | null;
      system_prompt: string | null;
      favorite: boolean;
      tags: string[];
      messages: unknown[];
    };
  } | null>(null);
  const undoDeleteTimerRef = useRef<number | null>(null);
  useEffect(() => {
    return () => {
      if (undoDeleteTimerRef.current !== null) {
        window.clearTimeout(undoDeleteTimerRef.current);
      }
    };
  }, []);
  // Same idea as undoDelete, one level down: a snapshot of the just-deleted
  // MESSAGE, restored (fresh id, no model call) via the dedicated restore
  // endpoint rather than Import — Import always creates a whole new
  // conversation, which would be wrong for putting one message back into
  // the conversation it came from.
  const [undoMessageDelete, setUndoMessageDelete] = useState<{
    conversationId: number;
    message: Message;
  } | null>(null);
  const undoMessageDeleteTimerRef = useRef<number | null>(null);
  useEffect(() => {
    return () => {
      if (undoMessageDeleteTimerRef.current !== null) {
        window.clearTimeout(undoMessageDeleteTimerRef.current);
      }
    };
  }, []);
  const [token, setToken] = useState(() => window.localStorage.getItem(TOKEN_STORAGE_KEY) ?? "");
  const [theme, setTheme] = useTheme();
  const [showArchived, setShowArchived] = useState(false);
  const [bulkSelectMode, setBulkSelectMode] = useState(false);
  const [bulkSelectedIds, setBulkSelectedIds] = useState<Set<number>>(new Set());
  const [bulkWorking, setBulkWorking] = useState(false);
  const [exportingSelected, setExportingSelected] = useState(false);
  const [tagFilter, setTagFilter] = useState("");
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  const [sortOrder, setSortOrder] = useState<"recent" | "name">("recent");
  // The conversation selected immediately before the current one — lets a
  // "Back" control flip between the last two, like Alt+Tab, without a full
  // history stack.
  const [previousConversationId, setPreviousConversationId] = useState<number | null>(null);
  const lastSelectedConversationIdRef = useRef<number | null>(null);
  const { notifyEnabled, setNotifyEnabled, notifySoundEnabled, setNotifySoundEnabled } =
    useNotificationPreferences();
  const [streamState, setStreamState] = useState<StreamState | null>(null);
  const [jwtEnabled, setJwtEnabled] = useState(false);
  const [authEnabled, setAuthEnabled] = useState(false);
  const [registrationAllowed, setRegistrationAllowed] = useState(true);
  const [me, setMe] = useState<string | null>(null);
  // Whether `me` must set its own password before doing anything else (an
  // admin-created/reset account) — from /v1/auth/me (see refreshMe below).
  // Admin status itself is only needed inside Settings (which fetches its
  // own is_admin from /v1/settings), so it isn't tracked here.
  const [mustChangePassword, setMustChangePassword] = useState(false);
  const [loginUsername, setLoginUsername] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  // A dedicated message shown right next to the sign-in form, not just the
  // global chat-header status line — that line sits far enough away (top of
  // the chat panel) that a login/register failure there reads as "nothing
  // happened" rather than as an error.
  const [authMessage, setAuthMessage] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [usageOpen, setUsageOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [compareOpen, setCompareOpen] = useState(false);
  const [bookmarksOpen, setBookmarksOpen] = useState(false);
  const [templatesOpen, setTemplatesOpen] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [summarizeOpen, setSummarizeOpen] = useState(false);
  const [shortcutsHelpOpen, setShortcutsHelpOpen] = useState(false);
  const [headerMenuOpen, setHeaderMenuOpen] = useState(false);
  const [regenChoice, setRegenChoice] = useState("");
  const [statusModels, setStatusModels] = useState<{
    router?: string;
    budget?: string;
    fast?: string;
    smart?: string;
    fallback?: string;
  }>({});
  // Set only when a per-owner daily cap is configured AND the caller's
  // remaining room is low — null the rest of the time, including whenever no
  // cap is set at all (refreshUsageIndicators below never manufactures
  // urgency out of nothing).
  const [budgetWarning, setBudgetWarning] = useState<string | null>(null);
  // The caller's own spend today, for the persistent 💰 sidebar indicator —
  // refreshed (not accumulated client-side) after every paid action so it
  // always reflects the server's own ledger. todayCap prefers the per-owner
  // cap (the boundary the caller can actually see) and falls back to the
  // global cap purely as a denominator — never the live global spend itself,
  // which stays private to the operator (same rule refreshUsageIndicators follows).
  const [todaySpend, setTodaySpend] = useState<number | null>(null);
  const [todayCap, setTodayCap] = useState<number | null>(null);
  // Spend the app's own response cache avoided today (see the 🛟 indicator) —
  // distinct from todaySpend, which is real spend; this is money NOT spent.
  const [todayAvoidedCost, setTodayAvoidedCost] = useState<number | null>(null);

  const abortControllerRef = useRef<AbortController | null>(null);
  // The current in-flight stream's idempotency key (see
  // app/request_registry.py) — also doubles as the Stop button's explicit-
  // abort handle (POST /v1/requests/{request_id}/cancel), distinct from
  // just aborting the fetch: a bare fetch abort only stops THIS browser
  // tab from listening, it never tells the server to actually stop the
  // model call (see stopStreaming below).
  const currentRequestIdRef = useRef<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const messagesContainerRef = useRef<HTMLDivElement | null>(null);
  // Set whenever the selected conversation changes, so the next scroll pass
  // jumps straight to the latest message regardless of the "near the
  // bottom already" heuristic below — that heuristic is right mid-stream
  // (don't yank a reader back down), but wrong on a fresh conversation load
  // or switch, where scrollTop is stale from whatever was viewed before.
  const forceScrollRef = useRef(false);
  const selectedIdRef = useRef<number | null>(selectedConversationId);
  // Which conversation the draft-flush effect last saw selected, so it can
  // tell which conversation a half-typed question is being switched AWAY
  // from (there's no other way to know the "previous" value of a piece of
  // state inside the effect that reacts to it changing).
  const prevConversationIdRef = useRef<number | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const questionInputRef = useRef<HTMLTextAreaElement | null>(null);
  const importFileInputRef = useRef<HTMLInputElement | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const audioPlayerRef = useRef<HTMLAudioElement | null>(null);
  const [speakingMessageId, setSpeakingMessageId] = useState<number | null>(null);
  // Free, browser-native alternatives to the paid $🔊/$🎤 features (Web
  // Speech API — SpeechSynthesis/SpeechRecognition — runs entirely on-device,
  // no API call). Separate state from their paid counterparts since either
  // pair can be mid-flight independently, but starting one always stops any
  // other speech (paid or free) already in progress.
  const [freeSpeakingMessageId, setFreeSpeakingMessageId] = useState<number | null>(null);
  const speechRecognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const [freeRecording, setFreeRecording] = useState(false);
  const [copiedMessageId, setCopiedMessageId] = useState<number | null>(null);
  const [copiedLinkMessageId, setCopiedLinkMessageId] = useState<number | null>(null);
  const [deletingMessageId, setDeletingMessageId] = useState<number | null>(null);
  const [branchingMessageId, setBranchingMessageId] = useState<number | null>(null);
  const [continuingMessageId, setContinuingMessageId] = useState<number | null>(null);
  const [synthesizingMessageId, setSynthesizingMessageId] = useState<number | null>(null);
  const [editingMessageId, setEditingMessageId] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [instructionsOpen, setInstructionsOpen] = useState(false);
  const [instructionsDraft, setInstructionsDraft] = useState("");
  const [instructionsSaving, setInstructionsSaving] = useState(false);
  const [importing, setImporting] = useState(false);
  const [exportingAll, setExportingAll] = useState(false);
  const [showJumpToBottom, setShowJumpToBottom] = useState(false);
  const [findOpen, setFindOpen] = useState(false);
  const [findQuery, setFindQuery] = useState("");
  const [findActiveIndex, setFindActiveIndex] = useState(0);
  const findInputRef = useRef<HTMLInputElement | null>(null);
  const usernameInputRef = useRef<HTMLInputElement | null>(null);
  const tokenInputRef = useRef<HTMLInputElement | null>(null);

  // Scoped to the conversation actually being viewed — a single shared
  // stream slot backs the whole app (see abortControllerRef in streamInto),
  // so streamState can belong to a DIFFERENT conversation than the one
  // selected. Without this scoping, `streaming` (and everything gated on
  // it — the composer's Ask/Stop button, Create/Rename/Duplicate/Delete/
  // Regenerate) would reflect some other conversation's in-flight stream:
  // Stop would abort the wrong one, and the composer would show "Stop" for
  // a conversation that isn't actually streaming.
  const streaming = streamState !== null && streamState.conversationId === selectedConversationId;
  const busy = loading || streaming;

  // Keep a ref copy of the selection so async stream callbacks can tell whether
  // the user has since switched conversations without re-binding the closure.
  useEffect(() => {
    selectedIdRef.current = selectedConversationId;
  }, [selectedConversationId]);

  const selectedConversation =
    conversations.find((conversation) => conversation.id === selectedConversationId) ?? null;

  function requestHeaders(extra: Record<string, string> = {}): Record<string, string> {
    const headers = { ...extra };
    const cleanToken = token.trim();
    if (cleanToken) {
      headers.Authorization = `Bearer ${cleanToken}`;
    }
    return headers;
  }

  // A 401 on an authenticated call means one of two distinct things, and
  // conflating them produces a misleading message either way: a
  // previously-valid token can go stale between actions (expired, or
  // revoked by a logout in another tab) — recovered by signing out locally
  // so the login form reappears — or there was simply never a token at all
  // (auth is required here but nobody's logged in yet), which needs no
  // sign-out but does need to actually say so, rather than every action
  // showing its own generic "Failed to X" for what's really one of these two
  // fixable causes. By default this throws so the caller's existing
  // try/catch surfaces the message; pass `silent: true` for the two
  // initial-load flows that already have their own "return empty and stay
  // quiet" behavior — a fresh, not-yet-logged-in page load isn't an error
  // worth flashing, so that path only speaks up for the stale-token case.
  async function authFetch(
    url: string,
    init: RequestInit = {},
    opts: { silent?: boolean } = {},
  ): Promise<Response> {
    const res = await fetch(url, init);
    if (res.status === 401) {
      const hadToken = Boolean(token.trim());
      if (hadToken) {
        logout();
      }
      // Wording depends on which credential this deployment actually uses —
      // "sign in again" would be confusing advice in a static-token-only
      // deployment, which has no sign-in form, just a token field.
      const message = hadToken
        ? jwtEnabled
          ? "Session expired — please sign in again."
          : "Your API token was rejected — enter a valid one in the sidebar."
        : jwtEnabled
          ? "Log in to do this — see the sign-in form in the sidebar."
          : "Enter your API token in the sidebar to do this.";
      if (opts.silent) {
        if (hadToken) {
          showStatus(message, { error: true });
        }
      } else {
        throw new Error(message);
      }
    }
    return res;
  }

  async function loadConversations(
    preferredConversationId?: number | null,
    includeArchivedOverride?: boolean,
  ) {
    const includeArchived = includeArchivedOverride ?? showArchived;
    const res = await authFetch(
      `${API_BASE}/v1/conversations${includeArchived ? "?include_archived=true" : ""}`,
      { headers: requestHeaders() },
      { silent: true },
    );
    if (res.status === 401) {
      return [];
    }
    if (!res.ok) throw new Error("Failed to load conversations");

    const data = (await res.json()) as Conversation[];
    setConversations(data);

    if (preferredConversationId && data.some((item) => item.id === preferredConversationId)) {
      setSelectedConversationId(preferredConversationId);
      return data;
    }

    if (selectedConversationId && data.some((item) => item.id === selectedConversationId)) {
      return data;
    }

    setSelectedConversationId(data.length > 0 ? data[0].id : null);
    return data;
  }

  async function loadMessages(conversationId: number) {
    const res = await authFetch(
      `${API_BASE}/v1/conversations/${conversationId}/messages`,
      { headers: requestHeaders() },
      { silent: true },
    );
    if (res.status === 401) {
      return;
    }
    if (!res.ok) throw new Error("Failed to load messages");

    const data = (await res.json()) as Message[];
    setMessages(data);
  }

  async function resolveAction(conversationId: number, messageId: number, confirm: boolean) {
    // Optimistic: reflect the click immediately, then reconcile with the
    // server's outcome (webhook success/failure) once the response lands.
    setMessages((prev) =>
      prev.map((message) =>
        message.id === messageId
          ? { ...message, action_status: confirm ? "confirmed" : "declined" }
          : message,
      ),
    );

    try {
      const res = await authFetch(
        `${API_BASE}/v1/conversations/${conversationId}/messages/${messageId}/action`,
        {
          method: "POST",
          headers: requestHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ confirm }),
        },
      );
      if (!res.ok) {
        showStatus("Failed to resolve the action.", { error: true });
        return;
      }
      const data = (await res.json()) as { action_status: ActionStatus; detail?: string | null };
      setMessages((prev) =>
        prev.map((message) =>
          message.id === messageId ? { ...message, action_status: data.action_status } : message,
        ),
      );
      if (data.detail) {
        showStatus(data.detail, { error: data.action_status === "failed" });
      }
    } catch {
      showStatus("Failed to resolve the action.", { error: true });
    }
  }

  async function createConversation() {
    setLoading(true);
    showStatus("Creating conversation...");

    try {
      const res = await authFetch(`${API_BASE}/v1/conversations`, {
        method: "POST",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ title }),
      });

      if (!res.ok) throw new Error("Failed to create conversation");

      const conversation = (await res.json()) as Conversation;
      setSelectedConversationId(conversation.id);
      setTitle("New AI Workbench Conversation");
      await loadConversations(conversation.id);
      await loadMessages(conversation.id);
      showStatus(`Created conversation #${conversation.id}`);
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Unknown error", { error: true });
    } finally {
      setLoading(false);
    }
  }

  async function renameConversation() {
    if (!selectedConversation) {
      showStatus("Select a conversation first.");
      return;
    }

    const newTitle = window.prompt("Rename conversation:", selectedConversation.title);
    if (!newTitle || !newTitle.trim()) {
      return;
    }

    setLoading(true);
    showStatus("Renaming conversation...");

    try {
      const res = await authFetch(`${API_BASE}/v1/conversations/${selectedConversation.id}`, {
        method: "PATCH",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ title: newTitle.trim() }),
      });

      if (!res.ok) throw new Error("Failed to rename conversation");

      await loadConversations(selectedConversation.id);
      showStatus("Conversation renamed.");
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Unknown error", { error: true });
    } finally {
      setLoading(false);
    }
  }

  async function editTags() {
    if (!selectedConversation) {
      showStatus("Select a conversation first.");
      return;
    }

    const input = window.prompt(
      "Tags (comma-separated):",
      (selectedConversation.tags ?? []).join(", "),
    );
    if (input === null) {
      return;
    }
    const tags = input
      .split(",")
      .map((tag) => tag.trim())
      .filter(Boolean);

    setLoading(true);
    showStatus("Updating tags...");

    try {
      const res = await authFetch(`${API_BASE}/v1/conversations/${selectedConversation.id}/tags`, {
        method: "PUT",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ tags }),
      });

      if (!res.ok) throw new Error("Failed to update tags");

      await loadConversations(selectedConversation.id);
      showStatus("Tags updated.");
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Unknown error", { error: true });
    } finally {
      setLoading(false);
    }
  }

  async function duplicateConversation() {
    if (!selectedConversation) {
      showStatus("Select a conversation first.");
      return;
    }

    setLoading(true);
    showStatus("Duplicating conversation...");

    try {
      const res = await authFetch(
        `${API_BASE}/v1/conversations/${selectedConversation.id}/duplicate`,
        { method: "POST", headers: requestHeaders() },
      );
      if (!res.ok) throw new Error(`Failed to duplicate conversation (${res.status})`);

      const conversation = (await res.json()) as Conversation;
      await loadConversations(conversation.id);
      await loadMessages(conversation.id);
      showStatus(`Duplicated as "${conversation.title}".`);
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Failed to duplicate conversation", {
        error: true,
      });
    } finally {
      setLoading(false);
    }
  }

  function exportConversationAsPdf() {
    if (!selectedConversation) {
      return;
    }
    // No PDF library dependency: render a print-friendly document in a new
    // window and hand off to the browser's native print dialog, whose "Save
    // as PDF" destination is standard in every modern browser. Built via DOM
    // APIs (not document.write + an HTML string) so message content —
    // arbitrary user/model text — is never parsed as markup, just set as
    // text.
    const printWindow = window.open("", "_blank");
    if (!printWindow) {
      showStatus("Couldn't open the print window — check your browser's popup blocker.", {
        error: true,
      });
      return;
    }

    const doc = printWindow.document;
    doc.title = selectedConversation.title;

    const style = doc.createElement("style");
    style.textContent = `
      body { font-family: -apple-system, sans-serif; max-width: 720px; margin: 40px auto; color: #111; }
      h1 { font-size: 22px; }
      h2 { font-size: 13px; color: #555; margin-bottom: 4px; text-transform: capitalize; }
      .pdf-message { margin-bottom: 20px; padding-bottom: 14px; border-bottom: 1px solid #ddd; }
      p { white-space: pre-wrap; line-height: 1.5; margin: 0; }
    `;
    doc.head.appendChild(style);

    const heading = doc.createElement("h1");
    heading.textContent = selectedConversation.title;
    doc.body.appendChild(heading);

    for (const message of messages) {
      const section = doc.createElement("section");
      section.className = `pdf-message ${message.role}`;

      const messageHeading = doc.createElement("h2");
      messageHeading.textContent = `${message.role === "user" ? "User" : "Assistant"} — ${formatTimestamp(message.created_at)}`;
      section.appendChild(messageHeading);

      const body = doc.createElement("p");
      body.textContent = message.content;
      section.appendChild(body);

      doc.body.appendChild(section);
    }

    printWindow.focus();
    // A short delay lets the new window finish laying out the document
    // before print() captures it — calling it immediately can print a blank
    // page in some browsers.
    window.setTimeout(() => printWindow.print(), 150);
  }

  function exportConversation(format: "markdown" | "json") {
    if (!selectedConversation) {
      return;
    }
    const filenameBase =
      selectedConversation.title.trim().replace(/[^a-z0-9]+/gi, "_").replace(/^_+|_+$/g, "").toLowerCase() ||
      "conversation";

    const content =
      format === "json"
        ? JSON.stringify({ conversation: selectedConversation, messages }, null, 2)
        : buildConversationMarkdown(selectedConversation, messages);
    const mime = format === "json" ? "application/json" : "text/markdown";
    const extension = format === "json" ? "json" : "md";

    downloadTextFile(content, mime, `${filenameBase}.${extension}`);
  }

  async function copyConversationAsMarkdown() {
    if (!selectedConversation) {
      return;
    }
    try {
      await navigator.clipboard.writeText(buildConversationMarkdown(selectedConversation, messages));
      showStatus("Copied conversation as Markdown.");
    } catch {
      showStatus("Failed to copy to clipboard.", { error: true });
    }
  }

  async function copyConversationLink() {
    if (!selectedConversation) {
      return;
    }
    const url = new URL(window.location.href);
    url.search = "";
    url.searchParams.set("c", String(selectedConversation.id));
    try {
      await navigator.clipboard.writeText(url.toString());
      showStatus("Copied link to this conversation.");
    } catch {
      showStatus("Failed to copy link to clipboard.", { error: true });
    }
  }

  async function exportAllConversations() {
    setExportingAll(true);
    showStatus("Exporting all conversations...");
    try {
      // Archived conversations are included too — a backup that silently
      // dropped them would defeat the point of an "export everything" action.
      const res = await authFetch(`${API_BASE}/v1/conversations?include_archived=true`, {
        headers: requestHeaders(),
      });
      if (!res.ok) throw new Error("Failed to load conversations");
      const allConversations = (await res.json()) as Conversation[];

      const bundle = [];
      for (const conversation of allConversations) {
        const messagesRes = await authFetch(
          `${API_BASE}/v1/conversations/${conversation.id}/messages`,
          { headers: requestHeaders() },
        );
        const conversationMessages = messagesRes.ok
          ? ((await messagesRes.json()) as Message[])
          : [];
        bundle.push({ conversation, messages: conversationMessages });
      }

      const content = JSON.stringify(
        { exported_at: new Date().toISOString(), conversations: bundle },
        null,
        2,
      );
      downloadTextFile(content, "application/json", "ai-workbench-export.json");
      showStatus(`Exported ${bundle.length} conversation${bundle.length === 1 ? "" : "s"}.`);
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Failed to export conversations", {
        error: true,
      });
    } finally {
      setExportingAll(false);
    }
  }

  type ImportableEntry = {
    conversation?: {
      title?: string;
      pinned_model?: string | null;
      system_prompt?: string | null;
      favorite?: boolean;
      tags?: string[] | null;
    };
    messages?: {
      role?: string;
      content?: string;
      mode_used?: string | null;
      notes?: string | null;
      input_tokens?: number | null;
      output_tokens?: number | null;
      cost_usd?: number | null;
      cached?: boolean;
      sources?: Source[] | null;
      truncated?: boolean;
      code_results?: CodeResult[] | null;
      fact_checks?: FactCheckResult[] | null;
      academic_results?: AcademicResult[] | null;
      math_results?: MathResult[] | null;
      library_sources?: LibrarySource[] | null;
      workflow_steps?: WorkflowStep[] | null;
      model?: string | null;
      feedback?: number | null;
      feedback_reason?: string | null;
    }[];
  };

  async function importOneConversation(entry: ImportableEntry): Promise<Conversation> {
    if (!Array.isArray(entry.messages) || entry.messages.length === 0) {
      throw new Error("That file doesn't look like an exported conversation.");
    }

    const res = await authFetch(`${API_BASE}/v1/conversations/import`, {
      method: "POST",
      headers: requestHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        title: entry.conversation?.title,
        pinned_model: entry.conversation?.pinned_model ?? null,
        system_prompt: entry.conversation?.system_prompt ?? null,
        favorite: entry.conversation?.favorite ?? false,
        tags: entry.conversation?.tags ?? [],
        messages: entry.messages.map((message) => ({
          role: message.role,
          content: message.content,
          mode_used: message.mode_used ?? null,
          notes: message.notes ?? null,
          input_tokens: message.input_tokens ?? null,
          output_tokens: message.output_tokens ?? null,
          cost_usd: message.cost_usd ?? null,
          cached: message.cached ?? false,
          sources: message.sources ?? null,
          truncated: message.truncated ?? false,
          code_results: message.code_results ?? null,
          fact_checks: message.fact_checks ?? null,
          academic_results: message.academic_results ?? null,
          math_results: message.math_results ?? null,
          library_sources: message.library_sources ?? null,
          workflow_steps: message.workflow_steps ?? null,
          model: message.model ?? null,
          feedback: message.feedback ?? null,
          feedback_reason: message.feedback_reason ?? null,
        })),
      }),
    });
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
      throw new Error(
        typeof body.detail === "string" ? body.detail : `Import failed (${res.status})`,
      );
    }
    return (await res.json()) as Conversation;
  }

  async function importConversation(fileList: FileList | null) {
    const file = fileList?.[0];
    if (!file) {
      return;
    }

    setImporting(true);
    showStatus("Importing...");
    try {
      const parsed = JSON.parse(await file.text()) as ImportableEntry & {
        conversations?: ImportableEntry[];
      };

      if (Array.isArray(parsed.conversations)) {
        // A bulk "Export all" bundle — best-effort, same philosophy as
        // Compare's fan-out: one bad entry doesn't abort the rest.
        if (parsed.conversations.length === 0) {
          throw new Error("That file doesn't contain any conversations.");
        }
        let lastImported: Conversation | null = null;
        let successCount = 0;
        let failureCount = 0;
        for (const entry of parsed.conversations) {
          try {
            lastImported = await importOneConversation(entry);
            successCount += 1;
          } catch {
            failureCount += 1;
          }
        }
        if (lastImported) {
          await loadConversations(lastImported.id);
          await loadMessages(lastImported.id);
        } else {
          await loadConversations(null);
        }
        showStatus(
          failureCount > 0
            ? `Imported ${successCount} of ${parsed.conversations.length} conversations (${failureCount} failed).`
            : `Imported ${successCount} conversation${successCount === 1 ? "" : "s"}.`,
          { error: successCount === 0 },
        );
        return;
      }

      const conversation = await importOneConversation(parsed);
      await loadConversations(conversation.id);
      await loadMessages(conversation.id);
      showStatus(`Imported "${conversation.title}".`);
    } catch (error) {
      showStatus(
        error instanceof Error
          ? error.message
          : "Failed to import — is it valid JSON?",
        { error: true },
      );
    } finally {
      setImporting(false);
    }
  }

  async function setPin(model: string) {
    if (!selectedConversationId) {
      return;
    }
    const conversationId = selectedConversationId;
    try {
      const res = await authFetch(`${API_BASE}/v1/conversations/${conversationId}/pin`, {
        method: "PUT",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ model }),
      });
      if (!res.ok) throw new Error(`Failed to pin model (${res.status})`);
      await loadConversations(conversationId);
      showStatus(model ? `Pinned this conversation to ${model}` : "Pin cleared.");
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Failed to pin model", { error: true });
    }
  }

  async function toggleFavorite(conversation: Conversation) {
    const nextFavorite = !conversation.favorite;
    try {
      const res = await authFetch(`${API_BASE}/v1/conversations/${conversation.id}/favorite`, {
        method: "PUT",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ favorite: nextFavorite }),
      });
      if (!res.ok) throw new Error(`Failed to update favorite (${res.status})`);
      await loadConversations(selectedConversationId);
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Failed to update favorite", {
        error: true,
      });
    }
  }

  async function toggleShowArchived() {
    const next = !showArchived;
    setShowArchived(next);
    try {
      await loadConversations(selectedConversationId, next);
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Failed to load conversations", {
        error: true,
      });
    }
  }

  // Opt-in background notification: request permission the moment the user
  // turns it on (not on every app load, which would be an unsolicited
  // permission prompt). Turning it on with permission denied/unavailable
  // still enables the document-title flash fallback below, which needs no
  // permission at all.
  // A short synthesized beep via the Web Audio API — no audio asset file or
  // new dependency needed for a one-off notification sound.
  function playNotificationSound() {
    try {
      const AudioCtx =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!AudioCtx) {
        return;
      }
      const ctx = new AudioCtx();
      const oscillator = ctx.createOscillator();
      const gain = ctx.createGain();
      oscillator.type = "sine";
      oscillator.frequency.value = 880;
      gain.gain.setValueAtTime(0.15, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.3);
      oscillator.connect(gain);
      gain.connect(ctx.destination);
      oscillator.start();
      oscillator.stop(ctx.currentTime + 0.3);
      oscillator.onended = () => void ctx.close();
    } catch {
      // Audio playback can fail under some browser autoplay policies; skip
      // silently rather than let a beep break a notification.
    }
  }

  function toggleNotify() {
    const next = !notifyEnabled;
    setNotifyEnabled(next);
    if (next && "Notification" in window && Notification.permission === "default") {
      void Notification.requestPermission();
    }
  }

  function openInstructions() {
    if (!selectedConversation) {
      return;
    }
    setInstructionsDraft(selectedConversation.system_prompt ?? "");
    setInstructionsOpen(true);
  }

  function cancelInstructions() {
    setInstructionsOpen(false);
    setInstructionsDraft("");
  }

  async function saveInstructions() {
    if (!selectedConversationId) {
      return;
    }
    const conversationId = selectedConversationId;
    setInstructionsSaving(true);
    try {
      const res = await authFetch(`${API_BASE}/v1/conversations/${conversationId}/system_prompt`, {
        method: "PUT",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ system_prompt: instructionsDraft }),
      });
      if (!res.ok) throw new Error(`Failed to save instructions (${res.status})`);
      await loadConversations(conversationId);
      setInstructionsOpen(false);
      showStatus(instructionsDraft.trim() ? "Instructions saved." : "Instructions cleared.");
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Failed to save instructions", {
        error: true,
      });
    } finally {
      setInstructionsSaving(false);
    }
  }

  async function deleteConversation() {
    if (!selectedConversation) {
      showStatus("Select a conversation first.");
      return;
    }

    const confirmed = window.confirm(
      `Delete "${selectedConversation.title}"?\n\nThis will permanently delete its saved messages from the local database.`,
    );

    if (!confirmed) {
      return;
    }

    // Captured before the DELETE fires, so Undo can restore it via Import
    // (fresh id, same content — attachments excepted, same Import limitation
    // documented elsewhere) even though the delete itself is permanent.
    const snapshotPayload = {
      title: selectedConversation.title,
      pinned_model: selectedConversation.pinned_model ?? null,
      system_prompt: selectedConversation.system_prompt ?? null,
      favorite: selectedConversation.favorite ?? false,
      tags: selectedConversation.tags ?? [],
      messages: messages.map((message) => ({
        role: message.role,
        content: message.content,
        mode_used: message.mode_used ?? null,
        notes: message.notes ?? null,
        input_tokens: message.input_tokens ?? null,
        output_tokens: message.output_tokens ?? null,
        cost_usd: message.cost_usd ?? null,
        cached: message.cached ?? false,
        sources: message.sources ?? null,
        truncated: message.truncated ?? false,
        code_results: message.code_results ?? null,
        fact_checks: message.fact_checks ?? null,
        academic_results: message.academic_results ?? null,
        math_results: message.math_results ?? null,
        library_sources: message.library_sources ?? null,
        workflow_steps: message.workflow_steps ?? null,
          model: message.model ?? null,
          feedback: message.feedback ?? null,
          feedback_reason: message.feedback_reason ?? null,
      })),
    };
    const snapshotTitle = selectedConversation.title;

    setLoading(true);
    showStatus("Deleting conversation...");

    try {
      const res = await authFetch(`${API_BASE}/v1/conversations/${selectedConversation.id}`, {
        method: "DELETE",
        headers: requestHeaders(),
      });

      if (!res.ok) throw new Error("Failed to delete conversation");

      // Clear any draft for the now-gone conversation — the id is never
      // coming back, and clearing `question` here (rather than leaving
      // stale text for the flush effect to re-save) keeps that effect from
      // recreating the very draft this just deleted.
      const drafts = loadDraftMap();
      delete drafts[String(selectedConversation.id)];
      saveDraftMap(drafts);

      setMessages([]);
      setQuestion("");
      setSelectedConversationId(null);
      const updatedConversations = await loadConversations(null);

      if (updatedConversations.length > 0) {
        await loadMessages(updatedConversations[0].id);
      }

      showStatus("Conversation deleted.");

      if (undoDeleteTimerRef.current !== null) {
        window.clearTimeout(undoDeleteTimerRef.current);
      }
      setUndoDelete({ title: snapshotTitle, payload: snapshotPayload });
      undoDeleteTimerRef.current = window.setTimeout(() => {
        setUndoDelete(null);
        undoDeleteTimerRef.current = null;
      }, 8000);
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Unknown error", { error: true });
    } finally {
      setLoading(false);
    }
  }

  async function undoConversationDelete() {
    if (!undoDelete) {
      return;
    }
    if (undoDeleteTimerRef.current !== null) {
      window.clearTimeout(undoDeleteTimerRef.current);
      undoDeleteTimerRef.current = null;
    }
    const { payload, title } = undoDelete;
    setUndoDelete(null);
    showStatus("Restoring conversation...");

    try {
      // An emptied conversation (no messages) has nothing for Import to
      // restore (it requires at least one message) — just recreate the
      // title, which is all there was to lose.
      const res =
        payload.messages.length > 0
          ? await authFetch(`${API_BASE}/v1/conversations/import`, {
              method: "POST",
              headers: requestHeaders({ "Content-Type": "application/json" }),
              body: JSON.stringify(payload),
            })
          : await authFetch(`${API_BASE}/v1/conversations`, {
              method: "POST",
              headers: requestHeaders({ "Content-Type": "application/json" }),
              body: JSON.stringify({ title }),
            });

      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
        throw new Error(
          typeof body.detail === "string" ? body.detail : `Failed to restore conversation (${res.status})`,
        );
      }
      const restored = (await res.json()) as Conversation;

      await loadConversations(null);
      setSelectedConversationId(restored.id);
      await loadMessages(restored.id);
      showStatus("Conversation restored.");
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Failed to restore conversation", {
        error: true,
      });
    }
  }

  async function archiveConversation() {
    if (!selectedConversation) {
      showStatus("Select a conversation first.");
      return;
    }
    const conversation = selectedConversation;
    const nextArchived = !conversation.archived;

    setLoading(true);
    try {
      const res = await authFetch(`${API_BASE}/v1/conversations/${conversation.id}/archive`, {
        method: "PUT",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ archived: nextArchived }),
      });
      if (!res.ok) throw new Error(`Failed to update archive status (${res.status})`);

      if (nextArchived && !showArchived) {
        // It just vanished from the visible list — same fallback as delete:
        // drop the selection and land on whatever's now at the top, if anything.
        setMessages([]);
        setSelectedConversationId(null);
        const updatedConversations = await loadConversations(null);
        if (updatedConversations.length > 0) {
          await loadMessages(updatedConversations[0].id);
        }
      } else {
        await loadConversations(conversation.id);
      }

      showStatus(nextArchived ? "Conversation archived." : "Conversation restored.");
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Unknown error", { error: true });
    } finally {
      setLoading(false);
    }
  }

  function toggleBulkSelectMode() {
    setBulkSelectMode((current) => !current);
    setBulkSelectedIds(new Set());
  }

  function toggleBulkSelected(conversationId: number) {
    setBulkSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(conversationId)) {
        next.delete(conversationId);
      } else {
        next.add(conversationId);
      }
      return next;
    });
  }

  // Best-effort like Export all/bulk import: one conversation failing to
  // archive/delete never blocks the rest of the batch.
  // Same bundle shape as Export all, scoped to just the checked
  // conversations — fetches each one's own messages client-side, same as
  // Export all, so it works whether or not the selection includes archived
  // conversations.
  async function exportSelectedConversations() {
    const ids = Array.from(bulkSelectedIds);
    if (ids.length === 0) {
      return;
    }
    setExportingSelected(true);
    showStatus(`Exporting ${ids.length} conversation${ids.length === 1 ? "" : "s"}...`);
    try {
      const selected = conversations.filter((conversation) => bulkSelectedIds.has(conversation.id));
      const bundle = [];
      for (const conversation of selected) {
        const messagesRes = await authFetch(
          `${API_BASE}/v1/conversations/${conversation.id}/messages`,
          { headers: requestHeaders() },
        );
        const conversationMessages = messagesRes.ok
          ? ((await messagesRes.json()) as Message[])
          : [];
        bundle.push({ conversation, messages: conversationMessages });
      }

      const content = JSON.stringify(
        { exported_at: new Date().toISOString(), conversations: bundle },
        null,
        2,
      );
      downloadTextFile(content, "application/json", "ai-workbench-export-selected.json");
      showStatus(`Exported ${bundle.length} conversation${bundle.length === 1 ? "" : "s"}.`);
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Failed to export conversations", {
        error: true,
      });
    } finally {
      setExportingSelected(false);
    }
  }

  async function bulkArchiveSelected() {
    const ids = Array.from(bulkSelectedIds);
    if (ids.length === 0) {
      return;
    }
    setBulkWorking(true);
    showStatus(`Archiving ${ids.length} conversation${ids.length === 1 ? "" : "s"}...`);
    let successCount = 0;
    for (const id of ids) {
      try {
        const res = await authFetch(`${API_BASE}/v1/conversations/${id}/archive`, {
          method: "PUT",
          headers: requestHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ archived: true }),
        });
        if (res.ok) successCount += 1;
      } catch {
        // Counted as a failure below via the successCount shortfall.
      }
    }
    const failureCount = ids.length - successCount;
    setBulkSelectedIds(new Set());
    if (selectedConversationId && ids.includes(selectedConversationId) && !showArchived) {
      setMessages([]);
      setSelectedConversationId(null);
      const updatedConversations = await loadConversations(null);
      if (updatedConversations.length > 0) {
        await loadMessages(updatedConversations[0].id);
      }
    } else {
      await loadConversations(selectedConversationId, showArchived);
    }
    showStatus(
      failureCount > 0
        ? `Archived ${successCount} of ${ids.length} conversations (${failureCount} failed).`
        : `Archived ${successCount} conversation${successCount === 1 ? "" : "s"}.`,
      { error: successCount === 0 },
    );
    setBulkWorking(false);
  }

  // Adds one tag to every selected conversation without disturbing any tags
  // they already have — the tags endpoint replaces wholesale, so each
  // conversation's existing tag list (already in local state) is merged with
  // the new one before the PUT, rather than clobbering it.
  async function bulkTagSelected() {
    const ids = Array.from(bulkSelectedIds);
    if (ids.length === 0) {
      return;
    }
    const input = window.prompt("Add this tag to all selected conversations:");
    const tag = input?.trim();
    if (!tag) {
      return;
    }

    setBulkWorking(true);
    showStatus(`Tagging ${ids.length} conversation${ids.length === 1 ? "" : "s"}...`);
    let successCount = 0;
    for (const id of ids) {
      const conversation = conversations.find((candidate) => candidate.id === id);
      const existingTags = conversation?.tags ?? [];
      if (existingTags.includes(tag)) {
        successCount += 1;
        continue;
      }
      try {
        const res = await authFetch(`${API_BASE}/v1/conversations/${id}/tags`, {
          method: "PUT",
          headers: requestHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ tags: [...existingTags, tag] }),
        });
        if (res.ok) successCount += 1;
      } catch {
        // Counted as a failure below via the successCount shortfall.
      }
    }
    const failureCount = ids.length - successCount;
    setBulkSelectedIds(new Set());
    await loadConversations(selectedConversationId, showArchived);
    showStatus(
      failureCount > 0
        ? `Tagged ${successCount} of ${ids.length} conversations (${failureCount} failed).`
        : `Tagged ${successCount} conversation${successCount === 1 ? "" : "s"}.`,
      { error: successCount === 0 },
    );
    setBulkWorking(false);
  }

  async function bulkDeleteSelected() {
    const ids = Array.from(bulkSelectedIds);
    if (ids.length === 0) {
      return;
    }
    const confirmed = window.confirm(
      `Delete ${ids.length} conversation${ids.length === 1 ? "" : "s"}?\n\nThis will permanently delete their saved messages from the local database.`,
    );
    if (!confirmed) {
      return;
    }

    setBulkWorking(true);
    showStatus(`Deleting ${ids.length} conversation${ids.length === 1 ? "" : "s"}...`);
    let successCount = 0;
    const drafts = loadDraftMap();
    for (const id of ids) {
      try {
        const res = await authFetch(`${API_BASE}/v1/conversations/${id}`, {
          method: "DELETE",
          headers: requestHeaders(),
        });
        if (res.ok) {
          successCount += 1;
          delete drafts[String(id)];
        }
      } catch {
        // Counted as a failure below via the successCount shortfall.
      }
    }
    saveDraftMap(drafts);
    const failureCount = ids.length - successCount;
    setBulkSelectedIds(new Set());
    if (selectedConversationId && ids.includes(selectedConversationId)) {
      setMessages([]);
      setQuestion("");
      setSelectedConversationId(null);
      const updatedConversations = await loadConversations(null);
      if (updatedConversations.length > 0) {
        await loadMessages(updatedConversations[0].id);
      }
    } else {
      await loadConversations(selectedConversationId, showArchived);
    }
    showStatus(
      failureCount > 0
        ? `Deleted ${successCount} of ${ids.length} conversations (${failureCount} failed).`
        : `Deleted ${successCount} conversation${successCount === 1 ? "" : "s"}.`,
      { error: successCount === 0 },
    );
    setBulkWorking(false);
  }

  async function refreshAfterStream(conversationId: number) {
    // Fetch the now-persisted messages, but only replace the visible pane if the
    // user is still on this conversation — otherwise we'd clobber the pane they
    // switched to. Clear the streaming bubble in the same tick as the message
    // swap so React batches them into one render (no duplicated-pair flash).
    let fetched: Message[] | null = null;
    try {
      const res = await authFetch(`${API_BASE}/v1/conversations/${conversationId}/messages`, {
        headers: requestHeaders(),
      });
      if (res.ok) {
        fetched = (await res.json()) as Message[];
      }
    } catch {
      // Keep whatever status the stream handler already set.
    }

    if (fetched && selectedIdRef.current === conversationId) {
      setMessages(fetched);
    }
    setStreamState(null);

    try {
      await loadConversations(selectedIdRef.current ?? conversationId);
    } catch {
      // Sidebar refresh is best-effort.
    }
  }

  // Shared SSE machinery for both asking and regenerating. `displayQuestion` is
  // shown in the streaming bubble; the caller has already done any pre-work.
  async function streamInto(
    url: string,
    body: Record<string, unknown>,
    displayQuestion: string,
    opts?: {
      startStatus?: string;
      onEmptyError?: () => void;
      questionImages?: string[];
      questionFiles?: FileAttachment[];
      questionAudio?: { filename: string; duration_seconds?: number | null }[];
    },
  ) {
    if (loading) {
      return;
    }
    if (streamState !== null && streamState.conversationId !== selectedConversationId) {
      // A single shared stream slot backs the whole app (see
      // abortControllerRef below) — a different conversation is still using
      // it. Say so, rather than a silent no-op that looks like the click did
      // nothing at all.
      showStatus(
        "Another conversation is still answering — stop it or wait for it to finish, then try again.",
        { error: true },
      );
      return;
    }
    if (streaming) {
      // Already streaming into this exact conversation — unreachable via the
      // UI (the composer shows Stop, not Ask, in that case); guards a stray
      // programmatic double-call.
      return;
    }
    if (!selectedConversationId) {
      showStatus("Create or select a conversation first.");
      return;
    }

    const conversationId = selectedConversationId;
    const controller = new AbortController();
    abortControllerRef.current = controller;
    // A fresh idempotency key per send (see app/request_registry.py) — kept
    // in a ref (not local-only) so stopStreaming can read it and tell the
    // SERVER to actually cancel, not just abandon this fetch. Cleared once
    // the stream reaches a terminal state below so a later, unrelated Stop
    // click can't cancel a request that already finished.
    const requestId = crypto.randomUUID();
    currentRequestIdRef.current = requestId;
    const bodyWithRequestId = { ...body, request_id: requestId };

    setUnansweredNotice((current) =>
      current?.conversationId === conversationId ? null : current,
    );
    setSrAnswerAnnouncement("");
    showStatus(opts?.startStatus ?? "Asking...");
    setStreamState({
      conversationId,
      question: displayQuestion,
      answer: "",
      questionImages: opts?.questionImages && opts.questionImages.length > 0 ? opts.questionImages : null,
      questionFiles: opts?.questionFiles && opts.questionFiles.length > 0 ? opts.questionFiles : null,
      questionAudio: opts?.questionAudio && opts.questionAudio.length > 0 ? opts.questionAudio : null,
    });

    let answer = "";

    try {
      const res = await authFetch(url, {
        method: "POST",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(bodyWithRequestId),
        signal: controller.signal,
      });

      if (!res.ok) {
        if (res.status === 429) {
          // slowapi's rate-limit response uses {"error": "..."} — a
          // different shape from every other endpoint's {"detail": "..."},
          // since it's raised by the rate-limit middleware, not our own
          // route handlers.
          let reason = "";
          try {
            const errorBody = (await res.json()) as { error?: string };
            reason = errorBody.error ?? "";
          } catch {
            // Not JSON; show the generic rate-limit message alone.
          }
          throw new Error(
            `You're sending requests too fast — please wait a moment and try again.${reason ? ` (${reason})` : ""}`,
          );
        }
        let detail = `Request failed (${res.status})`;
        try {
          const errorBody = (await res.json()) as { detail?: string };
          if (errorBody.detail) {
            detail = errorBody.detail;
          }
        } catch {
          // Not JSON; keep the generic message.
        }
        throw new Error(detail);
      }

      if (!res.body) {
        throw new Error("Streaming is not supported by this browser.");
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let terminal = false;

      const handleFrame = (frame: SseFrame) => {
        let payload: Record<string, unknown>;
        try {
          payload = JSON.parse(frame.data) as Record<string, unknown>;
        } catch {
          return;
        }

        if (frame.event === "meta") {
          showStatus(`Routing: ${String(payload.mode_used ?? "?")} via ${String(payload.model ?? "?")}`);
        } else if (frame.event === "step") {
          const status = String(payload.status ?? "");
          const category = String(payload.category ?? "");
          const index = Number(payload.index ?? 0);
          const total = Number(payload.total ?? 0);
          showStatus(
            `Step ${index + 1}/${total} (${category}): ${status === "running" ? "working…" : status}`,
          );
          setStreamState((prev) => {
            if (!prev) return prev;
            const step: WorkflowStep = {
              category,
              instruction: String(payload.instruction ?? ""),
              model: String(payload.model ?? ""),
              status: status || "running",
            };
            const existing = prev.workflowProgress ?? [];
            const updated =
              index < existing.length
                ? existing.map((s, i) => (i === index ? step : s))
                : [...existing, step];
            return { ...prev, workflowProgress: updated };
          });
        } else if (frame.event === "delta") {
          answer += String(payload.text ?? "");
          setStreamState((prev) => (prev ? { ...prev, answer } : prev));
        } else if (frame.event === "done") {
          terminal = true;
          // An empty answer (budget refusal, truncated reasoning call, etc.)
          // means nothing was saved — flag it as an error rather than
          // routine routing telemetry, which it would otherwise be
          // indistinguishable from.
          const answerText = String(payload.answer ?? "").trim();
          showStatus(`${String(payload.mode_used ?? "?")} | ${String(payload.notes ?? "")}`, {
            error: !answerText,
          });
          setUnansweredNotice(
            answerText
              ? null
              : { conversationId, note: String(payload.notes ?? "No answer was saved.") },
          );
          if (answerText) setSrAnswerAnnouncement(`Answer received: ${answerText}`);
          if (answerText) void refreshUsageIndicators();
          if (answerText && notifyEnabled && document.hidden) {
            // The title flash needs no permission and always works; the
            // Notification popup is strictly better when granted, so both
            // fire together rather than one replacing the other.
            document.title = "💬 New reply — " + BASE_DOCUMENT_TITLE;
            if (notifySoundEnabled) {
              playNotificationSound();
            }
            if ("Notification" in window && Notification.permission === "granted") {
              const convo = conversations.find((c) => c.id === conversationId);
              const notification = new Notification(convo?.title ?? BASE_DOCUMENT_TITLE, {
                body: answerText.slice(0, 200),
              });
              notification.onclick = () => {
                window.focus();
                setSelectedConversationId(conversationId);
                notification.close();
              };
            }
          }
          const sources = Array.isArray(payload.sources) ? (payload.sources as Source[]) : null;
          const pendingAction =
            payload.pending_action && typeof payload.pending_action === "object"
              ? (payload.pending_action as PendingAction)
              : null;
          const images = Array.isArray(payload.images) ? (payload.images as string[]) : null;
          const codeResults = Array.isArray(payload.code_results)
            ? (payload.code_results as CodeResult[])
            : null;
          const factChecks = Array.isArray(payload.fact_checks)
            ? (payload.fact_checks as FactCheckResult[])
            : null;
          const academicResults = Array.isArray(payload.academic_results)
            ? (payload.academic_results as AcademicResult[])
            : null;
          const mathResults = Array.isArray(payload.math_results)
            ? (payload.math_results as MathResult[])
            : null;
          const librarySources = Array.isArray(payload.library_sources)
            ? (payload.library_sources as LibrarySource[])
            : null;
          const workflowSteps = Array.isArray(payload.workflow_steps)
            ? (payload.workflow_steps as WorkflowStep[])
            : null;
          if (
            (sources && sources.length > 0) ||
            pendingAction ||
            (images && images.length > 0) ||
            (codeResults && codeResults.length > 0) ||
            (factChecks && factChecks.length > 0) ||
            (academicResults && academicResults.length > 0) ||
            (mathResults && mathResults.length > 0) ||
            (librarySources && librarySources.length > 0) ||
            (workflowSteps && workflowSteps.length > 0)
          ) {
            setStreamState((prev) =>
              prev
                ? {
                    ...prev,
                    ...(sources && sources.length > 0 ? { sources } : {}),
                    ...(pendingAction ? { pending_action: pendingAction } : {}),
                    ...(images && images.length > 0 ? { images } : {}),
                    ...(codeResults && codeResults.length > 0
                      ? { code_results: codeResults }
                      : {}),
                    ...(factChecks && factChecks.length > 0
                      ? { fact_checks: factChecks }
                      : {}),
                    ...(academicResults && academicResults.length > 0
                      ? { academic_results: academicResults }
                      : {}),
                    ...(mathResults && mathResults.length > 0
                      ? { math_results: mathResults }
                      : {}),
                    ...(librarySources && librarySources.length > 0
                      ? { library_sources: librarySources }
                      : {}),
                    ...(workflowSteps && workflowSteps.length > 0
                      ? { workflow_steps: workflowSteps }
                      : {}),
                  }
                : prev,
            );
          }
        } else if (frame.event === "error") {
          terminal = true;
          showStatus(`Error: ${String(payload.message ?? "stream failed")}`, { error: true });
          setUnansweredNotice({
            conversationId,
            note: String(payload.message ?? "The request failed."),
          });
        }
      };

      for (;;) {
        const { value, done } = await reader.read();
        if (done) {
          break;
        }

        buffer += decoder.decode(value, { stream: true });
        const { frames, rest } = extractSseFrames(buffer);
        buffer = rest;

        for (const frame of frames) {
          handleFrame(frame);
        }

        if (terminal) {
          try {
            await reader.cancel();
          } catch {
            // The stream is already finished server-side.
          }
          break;
        }
      }

      if (!terminal) {
        buffer += decoder.decode();
        const { frames } = extractSseFrames(buffer + "\n\n");
        for (const frame of frames) {
          handleFrame(frame);
        }
        if (!terminal) {
          showStatus("Stream ended unexpectedly.", { error: true });
          setUnansweredNotice({ conversationId, note: "Stream ended unexpectedly." });
        }
      }

      await refreshAfterStream(conversationId);
    } catch (error) {
      const aborted = error instanceof DOMException && error.name === "AbortError";
      showStatus(aborted ? "Stopped." : error instanceof Error ? error.message : "Unknown error", {
        error: !aborted,
      });
      if (!aborted && answer === "") {
        setUnansweredNotice({
          conversationId,
          note: error instanceof Error ? error.message : "The request failed.",
        });
      }
      if (answer === "") {
        opts?.onEmptyError?.();
      }
      await refreshAfterStream(conversationId);
    } finally {
      abortControllerRef.current = null;
      if (currentRequestIdRef.current === requestId) {
        currentRequestIdRef.current = null;
      }
      setStreamState(null);
    }
  }

  function isDocumentFile(file: File): boolean {
    if (ACCEPTED_FILE_MIMES.has(file.type)) {
      return true;
    }
    // A .csv attachment is recognized here (text/csv is a real mime some
    // browsers report) but deliberately NOT added to ACCEPTED_FILE_MIMES
    // above -- that set controls which mimes survive normalization below
    // unchanged, and a .csv should always normalize to text/plain, the
    // same treatment an unrecognized .md mime already gets.
    if (file.type === "text/csv") {
      return true;
    }
    // Some browsers report an empty/nonstandard mime for .txt/.md/.csv;
    // fall back to the extension so those still work. A bare-mime .xlsx is
    // deliberately NOT included here -- unlike a text file, there's no safe
    // "treat it as plain text" fallback for a binary spreadsheet, so an
    // .xlsx with no/wrong mime is left unrecognized rather than guessed at.
    const name = file.name.toLowerCase();
    return (
      file.type === "" &&
      (name.endsWith(".txt") || name.endsWith(".md") || name.endsWith(".csv"))
    );
  }

  function readAsDataUrl(file: File): Promise<string | null> {
    return new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(typeof reader.result === "string" ? reader.result : null);
      reader.onerror = () => resolve(null);
      reader.readAsDataURL(file);
    });
  }

  function isAudioAttachmentFile(file: File): boolean {
    return ACCEPTED_AUDIO_MIMES.has(file.type);
  }

  // Duration for the UI's audio chip, measured client-side via an offscreen
  // <audio> element — this app never decodes audio server-side (see
  // app/audio_ingestion.py), so this is the only place it's ever known.
  // Resolves to null (not 0) on any failure, so a chip can render without a
  // duration rather than a misleading "0:00".
  function readAudioDuration(file: File): Promise<number | null> {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(file);
      const audio = new Audio();
      const cleanup = () => URL.revokeObjectURL(url);
      audio.onloadedmetadata = () => {
        const duration = Number.isFinite(audio.duration) ? audio.duration : null;
        cleanup();
        resolve(duration);
      };
      audio.onerror = () => {
        cleanup();
        resolve(null);
      };
      audio.src = url;
    });
  }

  async function handleFilesSelected(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) {
      return;
    }
    const files = Array.from(fileList);
    const imageFiles = files.filter((file) => file.type.startsWith("image/"));
    const audioFiles = files.filter((file) => isAudioAttachmentFile(file));
    const documentFiles = files.filter(
      (file) =>
        !file.type.startsWith("image/") && !isAudioAttachmentFile(file) && isDocumentFile(file),
    );

    const selectedImages = imageFiles.slice(0, Math.max(0, MAX_ATTACHED_IMAGES - attachedImages.length));
    const selectedDocuments = documentFiles.slice(
      0,
      Math.max(0, MAX_ATTACHED_FILES - attachedFiles.length),
    );
    const selectedAudio = audioFiles.slice(0, Math.max(0, MAX_ATTACHED_AUDIO - attachedAudio.length));

    const validImages = (await Promise.all(selectedImages.map(readAsDataUrl))).filter(
      (url): url is string => url !== null,
    );

    const documentResults = await Promise.all(
      selectedDocuments.map(async (file): Promise<FileAttachment | null> => {
        const dataUrl = await readAsDataUrl(file);
        if (!dataUrl) {
          return null;
        }
        // Normalize a browser-reported empty/nonstandard mime (common for
        // .md) to a supported one so it matches the backend's allowlist.
        const mime = ACCEPTED_FILE_MIMES.has(file.type) ? file.type : "text/plain";
        const base64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
        return { filename: file.name, data: `data:${mime};base64,${base64}` };
      }),
    );
    const validDocuments = documentResults.filter((f): f is FileAttachment => f !== null);

    let oversizedAudio = 0;
    const audioResults = await Promise.all(
      selectedAudio.map(async (file): Promise<AudioAttachment | null> => {
        if (file.size > MAX_AUDIO_BYTES) {
          oversizedAudio += 1;
          return null;
        }
        const [dataUrl, duration] = await Promise.all([
          readAsDataUrl(file),
          readAudioDuration(file),
        ]);
        if (!dataUrl) {
          return null;
        }
        const base64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
        return { filename: file.name, data: `data:${file.type};base64,${base64}`, duration_seconds: duration };
      }),
    );
    const validAudio = audioResults.filter((a): a is AudioAttachment => a !== null);

    if (oversizedAudio > 0) {
      showStatus(
        `${oversizedAudio === 1 ? "One clip is" : `${oversizedAudio} clips are`} over the 25MB transcription limit and ${oversizedAudio === 1 ? "was" : "were"} skipped — split it into a shorter clip.`,
        { error: true },
      );
    }

    const skipped =
      files.length -
      selectedImages.length -
      selectedDocuments.length -
      selectedAudio.length +
      (selectedImages.length - validImages.length) +
      (selectedDocuments.length - validDocuments.length) +
      (selectedAudio.length - validAudio.length - oversizedAudio);
    if (skipped > 0) {
      showStatus(
        `Some files were skipped (images, PDFs/plain text/CSV/.xlsx, and audio only — up to ${MAX_ATTACHED_IMAGES} images / ${MAX_ATTACHED_FILES} documents / ${MAX_ATTACHED_AUDIO} audio clips).`,
      );
    }

    setAttachedImages((prev) => [...prev, ...validImages]);
    setAttachedFiles((prev) => [...prev, ...validDocuments]);
    setAttachedAudio((prev) => [...prev, ...validAudio]);
  }

  function removeAttachedImage(index: number) {
    setAttachedImages((prev) => prev.filter((_, i) => i !== index));
  }

  function removeAttachedFile(index: number) {
    setAttachedFiles((prev) => prev.filter((_, i) => i !== index));
  }

  function removeAttachedAudio(index: number) {
    setAttachedAudio((prev) => prev.filter((_, i) => i !== index));
  }

  function pickAudioMimeType(): string | undefined {
    if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported) {
      return undefined;
    }
    return PREFERRED_AUDIO_MIME_TYPES.find((type) => MediaRecorder.isTypeSupported(type));
  }

  function blobToBase64(blob: Blob): Promise<string | null> {
    return new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = typeof reader.result === "string" ? reader.result : null;
        resolve(result ? result.slice(result.indexOf(",") + 1) : null);
      };
      reader.onerror = () => resolve(null);
      reader.readAsDataURL(blob);
    });
  }

  async function transcribeRecording(chunks: Blob[], mimeType: string) {
    if (chunks.length === 0) {
      showStatus("No audio was recorded.");
      return;
    }
    setTranscribing(true);
    showStatus("Transcribing...");
    try {
      const blob = new Blob(chunks, { type: mimeType });
      const base64 = await blobToBase64(blob);
      if (!base64) {
        showStatus("Failed to read the recording.", { error: true });
        return;
      }
      // Strip codec parameters (e.g. "audio/webm;codecs=opus") — the backend
      // only recognises the bare mime, not the full MediaRecorder type string.
      const baseMime = mimeType.split(";")[0] || "audio/webm";

      const res = await authFetch(`${API_BASE}/v1/transcribe`, {
        method: "POST",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ audio: `data:${baseMime};base64,${base64}` }),
      });

      if (!res.ok) {
        let detail = `Transcription failed (${res.status})`;
        try {
          const body = (await res.json()) as { detail?: string };
          if (body.detail) {
            detail = body.detail;
          }
        } catch {
          // Not JSON; keep the generic message.
        }
        showStatus(detail, { error: true });
        return;
      }

      const data = (await res.json()) as { text: string };
      if (data.text) {
        setQuestion((current) => (current ? `${current} ${data.text}` : data.text));
        showStatus("Ready");
      } else {
        showStatus("No speech was detected.");
      }
      void refreshUsageIndicators();
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Transcription failed.", {
        error: true,
      });
    } finally {
      setTranscribing(false);
    }
  }

  async function toggleRecording() {
    if (recording) {
      mediaRecorderRef.current?.stop();
      return;
    }
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      showStatus("Voice input isn't supported in this browser.", { error: true });
      return;
    }
    // Only one voice-input mode at a time, paid or free.
    speechRecognitionRef.current?.stop();

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = pickAudioMimeType();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      const chunks: Blob[] = [];

      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          chunks.push(event.data);
        }
      };
      recorder.onstop = () => {
        for (const track of stream.getTracks()) {
          track.stop();
        }
        setRecording(false);
        void transcribeRecording(chunks, recorder.mimeType || mimeType || "audio/webm");
      };

      mediaRecorderRef.current = recorder;
      recorder.start();
      setRecording(true);
      showStatus("Recording... click the mic again to stop.");
    } catch {
      showStatus("Microphone access was denied or unavailable.", { error: true });
    }
  }

  // Free alternative to toggleRecording — the browser's own SpeechRecognition,
  // entirely on-device (lower accuracy, but zero API cost). Chrome/Safari
  // only (as `webkitSpeechRecognition`); Firefox has no implementation.
  function toggleFreeRecording() {
    if (freeRecording) {
      speechRecognitionRef.current?.stop();
      return;
    }
    const SpeechRecognitionCtor = getSpeechRecognitionConstructor();
    if (!SpeechRecognitionCtor) {
      showStatus("This browser doesn't support built-in voice input.", { error: true });
      return;
    }
    // Only one voice-input mode at a time, paid or free.
    mediaRecorderRef.current?.stop();

    const recognition = new SpeechRecognitionCtor();
    recognition.lang = (typeof navigator !== "undefined" && navigator.language) || "en-US";
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    recognition.onresult = (event) => {
      const text = event.results[0]?.[0]?.transcript;
      if (text) {
        setQuestion((current) => (current ? `${current} ${text}` : text));
      }
    };
    recognition.onerror = () => {
      showStatus("Browser voice input failed.", { error: true });
    };
    recognition.onend = () => {
      setFreeRecording(false);
    };
    speechRecognitionRef.current = recognition;
    recognition.start();
    setFreeRecording(true);
    showStatus("Listening (free, on-device)... click again to stop.");
  }

  async function copyMessage(message: Message) {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopiedMessageId(message.id);
      window.setTimeout(() => {
        setCopiedMessageId((current) => (current === message.id ? null : current));
      }, 1500);
    } catch {
      showStatus("Failed to copy to clipboard.", { error: true });
    }
  }

  async function copyMessageLink(message: Message) {
    if (!selectedConversationId) {
      return;
    }
    const url = new URL(window.location.href);
    url.search = "";
    url.searchParams.set("c", String(selectedConversationId));
    url.searchParams.set("m", String(message.id));
    try {
      await navigator.clipboard.writeText(url.toString());
      setCopiedLinkMessageId(message.id);
      window.setTimeout(() => {
        setCopiedLinkMessageId((current) => (current === message.id ? null : current));
      }, 1500);
    } catch {
      showStatus("Failed to copy link to clipboard.", { error: true });
    }
  }

  async function deleteMessage(message: Message) {
    if (!selectedConversationId) {
      return;
    }
    const confirmed = window.confirm(
      `Delete this ${message.role} message?\n\nThis removes only this message — nothing else in the conversation is affected.`,
    );
    if (!confirmed) {
      return;
    }

    const conversationId = selectedConversationId;
    setDeletingMessageId(message.id);
    try {
      const res = await authFetch(
        `${API_BASE}/v1/conversations/${conversationId}/messages/${message.id}`,
        { method: "DELETE", headers: requestHeaders() },
      );
      if (!res.ok) throw new Error(`Failed to delete message (${res.status})`);

      setMessages((prev) => prev.filter((candidate) => candidate.id !== message.id));
      showStatus("Message deleted.");

      if (undoMessageDeleteTimerRef.current !== null) {
        window.clearTimeout(undoMessageDeleteTimerRef.current);
      }
      setUndoMessageDelete({ conversationId, message });
      undoMessageDeleteTimerRef.current = window.setTimeout(() => {
        setUndoMessageDelete(null);
        undoMessageDeleteTimerRef.current = null;
      }, 8000);
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Failed to delete message", {
        error: true,
      });
    } finally {
      setDeletingMessageId(null);
    }
  }

  async function branchFromMessage(message: Message) {
    if (!selectedConversationId) {
      return;
    }
    setBranchingMessageId(message.id);
    showStatus("Branching conversation...");
    try {
      const res = await authFetch(
        `${API_BASE}/v1/conversations/${selectedConversationId}/messages/${message.id}/branch`,
        { method: "POST", headers: requestHeaders() },
      );
      if (!res.ok) throw new Error(`Failed to branch conversation (${res.status})`);

      const conversation = (await res.json()) as Conversation;
      await loadConversations(conversation.id);
      await loadMessages(conversation.id);
      showStatus(`Branched as "${conversation.title}".`);
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Failed to branch conversation", {
        error: true,
      });
    } finally {
      setBranchingMessageId(null);
    }
  }

  async function undoMessageDeletion() {
    if (!undoMessageDelete) {
      return;
    }
    if (undoMessageDeleteTimerRef.current !== null) {
      window.clearTimeout(undoMessageDeleteTimerRef.current);
      undoMessageDeleteTimerRef.current = null;
    }
    const { conversationId, message } = undoMessageDelete;
    setUndoMessageDelete(null);
    showStatus("Restoring message...");

    try {
      const res = await authFetch(`${API_BASE}/v1/conversations/${conversationId}/messages/restore`, {
        method: "POST",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          role: message.role,
          content: message.content,
          mode_used: message.mode_used ?? null,
          notes: message.notes ?? null,
          input_tokens: message.input_tokens ?? null,
          output_tokens: message.output_tokens ?? null,
          cost_usd: message.cost_usd ?? null,
          cached: message.cached ?? false,
          sources: message.sources ?? null,
          truncated: message.truncated ?? false,
          code_results: message.code_results ?? null,
          fact_checks: message.fact_checks ?? null,
          academic_results: message.academic_results ?? null,
          math_results: message.math_results ?? null,
          library_sources: message.library_sources ?? null,
          workflow_steps: message.workflow_steps ?? null,
          model: message.model ?? null,
          feedback: message.feedback ?? null,
          feedback_reason: message.feedback_reason ?? null,
        }),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
        throw new Error(
          typeof body.detail === "string" ? body.detail : `Failed to restore message (${res.status})`,
        );
      }

      if (conversationId === selectedConversationId) {
        await loadMessages(conversationId);
      }
      showStatus("Message restored.");
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Failed to restore message", {
        error: true,
      });
    }
  }

  async function toggleMessageBookmark(message: Message) {
    if (!selectedConversationId) {
      return;
    }
    const conversationId = selectedConversationId;
    const nextBookmarked = !message.bookmarked;
    try {
      const res = await authFetch(
        `${API_BASE}/v1/conversations/${conversationId}/messages/${message.id}/bookmark`,
        {
          method: "PUT",
          headers: requestHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ bookmarked: nextBookmarked }),
        },
      );
      if (!res.ok) throw new Error(`Failed to update bookmark (${res.status})`);

      setMessages((prev) =>
        prev.map((candidate) =>
          candidate.id === message.id ? { ...candidate, bookmarked: nextBookmarked } : candidate,
        ),
      );
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Failed to update bookmark", {
        error: true,
      });
    }
  }

  async function rateMessage(
    message: Message,
    verdict: "up" | "down" | null,
    reason?: string,
  ) {
    if (!selectedConversationId) {
      return;
    }
    const conversationId = selectedConversationId;
    try {
      const res = await authFetch(
        `${API_BASE}/v1/conversations/${conversationId}/messages/${message.id}/feedback`,
        {
          method: "PUT",
          headers: requestHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ verdict, reason: reason ?? null }),
        },
      );
      if (!res.ok) throw new Error(`Failed to rate message (${res.status})`);
      const updated = (await res.json()) as Message;
      setMessages((prev) =>
        prev.map((candidate) =>
          candidate.id === message.id
            ? {
                ...candidate,
                feedback: updated.feedback,
                feedback_reason: updated.feedback_reason,
              }
            : candidate,
        ),
      );
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Failed to rate message", {
        error: true,
      });
    }
  }

  async function continueMessage(message: Message) {
    if (!selectedConversationId) {
      return;
    }
    const conversationId = selectedConversationId;
    setContinuingMessageId(message.id);
    try {
      // request_id (see app/request_registry.py): a query param here, not a
      // body field — /continue has never taken a request body (see the
      // endpoint's own docstring).
      const requestId = crypto.randomUUID();
      const res = await authFetch(
        `${API_BASE}/v1/conversations/${conversationId}/messages/${message.id}/continue?request_id=${requestId}`,
        {
          method: "POST",
          headers: requestHeaders({ "Content-Type": "application/json" }),
        },
      );
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string };
        throw new Error(body.detail ?? `Failed to continue (${res.status})`);
      }
      const updated = (await res.json()) as Message;
      setMessages((prev) =>
        prev.map((candidate) => (candidate.id === message.id ? updated : candidate)),
      );
      void refreshUsageIndicators();
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Failed to continue the answer", {
        error: true,
      });
    } finally {
      setContinuingMessageId(null);
    }
  }

  async function toggleSpeak(message: Message) {
    if (speakingMessageId === message.id) {
      audioPlayerRef.current?.pause();
      setSpeakingMessageId(null);
      return;
    }

    // Only one clip plays at a time; stop whatever's currently playing,
    // paid or free.
    audioPlayerRef.current?.pause();
    setSpeakingMessageId(null);
    window.speechSynthesis?.cancel();
    setFreeSpeakingMessageId(null);

    setSynthesizingMessageId(message.id);
    try {
      const res = await authFetch(`${API_BASE}/v1/speak`, {
        method: "POST",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ text: message.content }),
      });

      if (!res.ok) {
        let detail = `Speech synthesis failed (${res.status})`;
        try {
          const body = (await res.json()) as { detail?: string };
          if (body.detail) {
            detail = body.detail;
          }
        } catch {
          // Not JSON; keep the generic message.
        }
        showStatus(detail, { error: true });
        return;
      }

      void refreshUsageIndicators();
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audio.onended = () => {
        setSpeakingMessageId((current) => (current === message.id ? null : current));
        URL.revokeObjectURL(url);
      };
      audioPlayerRef.current = audio;
      await audio.play();
      setSpeakingMessageId(message.id);
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Speech synthesis failed.", {
        error: true,
      });
    } finally {
      setSynthesizingMessageId(null);
    }
  }

  // Free alternative to toggleSpeak — the browser's own SpeechSynthesis
  // voice, entirely on-device (lower quality, but zero API cost).
  function toggleFreeSpeak(message: Message) {
    if (typeof window === "undefined" || !window.speechSynthesis) {
      showStatus("This browser doesn't support built-in text-to-speech.", { error: true });
      return;
    }
    if (freeSpeakingMessageId === message.id) {
      window.speechSynthesis.cancel();
      setFreeSpeakingMessageId(null);
      return;
    }

    // Only one clip plays at a time; stop whatever's currently playing,
    // paid or free.
    window.speechSynthesis.cancel();
    setFreeSpeakingMessageId(null);
    audioPlayerRef.current?.pause();
    setSpeakingMessageId(null);

    const utterance = new SpeechSynthesisUtterance(message.content);
    utterance.onend = () => {
      setFreeSpeakingMessageId((current) => (current === message.id ? null : current));
    };
    utterance.onerror = () => {
      setFreeSpeakingMessageId((current) => (current === message.id ? null : current));
    };
    window.speechSynthesis.speak(utterance);
    setFreeSpeakingMessageId(message.id);
  }

  async function askQuestion() {
    if (busy) {
      return;
    }
    if (!selectedConversationId) {
      showStatus("Create or select a conversation first.");
      return;
    }
    const cleanQuestion = question.trim();
    if (!cleanQuestion) {
      showStatus("Enter a question first.");
      return;
    }

    const cleanImages = attachedImages;
    const cleanFiles = attachedFiles;
    const cleanAudio = attachedAudio;
    setQuestion("");
    setCostPreview(null);
    setAttachedImages([]);
    setAttachedFiles([]);
    setAttachedAudio([]);
    await streamInto(
      `${API_BASE}/v1/conversations/${selectedConversationId}/ask/stream`,
      {
        question: cleanQuestion,
        mode,
        ...(cleanImages.length > 0 ? { images: cleanImages } : {}),
        ...(cleanFiles.length > 0 ? { files: cleanFiles } : {}),
        ...(cleanAudio.length > 0 ? { audio: cleanAudio } : {}),
        ...(researchMode ? { research: true } : {}),
      },
      cleanQuestion,
      {
        startStatus: cleanAudio.length > 0 ? "Transcribing audio..." : "Asking...",
        questionImages: cleanImages,
        questionFiles: cleanFiles,
        questionAudio: cleanAudio.map((a) => ({
          filename: a.filename,
          duration_seconds: a.duration_seconds,
        })),
        // Give the user their text/images/files/audio back so a transient
        // failure stays retryable.
        onEmptyError: () => {
          setQuestion((current) => (current ? current : cleanQuestion));
          setAttachedImages((current) => (current.length > 0 ? current : cleanImages));
          setAttachedFiles((current) => (current.length > 0 ? current : cleanFiles));
          setAttachedAudio((current) => (current.length > 0 ? current : cleanAudio));
        },
      },
    );
  }

  function startEdit(message: Message) {
    if (busy) {
      return;
    }
    setEditingMessageId(message.id);
    setEditDraft(message.content);
  }

  function cancelEdit() {
    setEditingMessageId(null);
    setEditDraft("");
  }

  async function saveEdit(message: Message) {
    const cleanDraft = editDraft.trim();
    if (!cleanDraft) {
      showStatus("Enter a question first.");
      return;
    }
    if (!selectedConversationId) {
      return;
    }

    const editedIndex = messages.findIndex((candidate) => candidate.id === message.id);
    if (editedIndex === -1) {
      return;
    }

    setEditingMessageId(null);
    // Optimistically drop the edited turn and everything after it, so the
    // streaming bubble replaces it in place. If the edit fails,
    // refreshAfterStream restores the server state — which still holds the
    // original message and its answer, since the server only deletes them
    // once the new answer succeeds.
    setMessages((prev) => prev.slice(0, editedIndex));

    // The original attachments (if any) carry over unchanged; only the text
    // is editable here.
    await streamInto(
      `${API_BASE}/v1/conversations/${selectedConversationId}/messages/${message.id}/edit/stream`,
      {
        question: cleanDraft,
        mode: "auto",
        ...(message.images && message.images.length > 0 ? { images: message.images } : {}),
        ...(message.files && message.files.length > 0 ? { files: message.files } : {}),
      },
      cleanDraft,
      {
        startStatus: "Editing...",
        questionImages: message.images ?? undefined,
        questionFiles: message.files ?? undefined,
      },
    );
  }

  async function regenerate() {
    if (busy || !selectedConversationId) {
      return;
    }
    const lastUserIndex = messages.map((message) => message.role).lastIndexOf("user");
    if (lastUserIndex === -1) {
      showStatus("Nothing to regenerate yet.");
      return;
    }
    const lastUser = messages[lastUserIndex];

    // Optimistically drop the turn being regenerated so the streaming bubble
    // replaces it in place (no duplicate question / stale answer). If the retry
    // fails, refreshAfterStream restores the server state — which still holds the
    // old answer, since the server only deletes it once the new one is ready.
    setMessages((prev) => prev.slice(0, lastUserIndex));

    // Parse the "regenerate with" selection into {mode?, model?}.
    const body: Record<string, unknown> = {};
    if (regenChoice.startsWith("mode:")) {
      body.mode = regenChoice.slice("mode:".length);
    } else if (regenChoice.startsWith("model:")) {
      body.model = regenChoice.slice("model:".length);
      body.mode = mode;
    } else {
      body.mode = "auto"; // re-route from scratch
    }

    await streamInto(
      `${API_BASE}/v1/conversations/${selectedConversationId}/regenerate/stream`,
      body,
      lastUser.content,
      { startStatus: "Regenerating..." },
    );
  }

  function stopStreaming() {
    // Explicit abort (see currentRequestIdRef's declaration): tell the
    // SERVER to actually stop the model call and release its budget
    // reservation, not just stop listening. Fire-and-forget — the local
    // fetch abort below already gives the user instant feedback regardless
    // of whether this call itself succeeds, and there's nothing useful to
    // do differently if it fails (the worker will just run to completion
    // as if this had been an ordinary disconnect, which is a safe fallback,
    // not a broken one).
    const requestId = currentRequestIdRef.current;
    if (requestId) {
      void authFetch(`${API_BASE}/v1/requests/${requestId}/cancel`, {
        method: "POST",
        headers: requestHeaders(),
      }).catch(() => {
        // Best-effort — see comment above.
      });
    }
    abortControllerRef.current?.abort();
  }

  async function refreshStatus() {
    try {
      const res = await fetch(`${API_BASE}/v1/status`);
      if (res.ok) {
        const data = (await res.json()) as {
          auth_enabled?: boolean;
          jwt_enabled?: boolean;
          registration_allowed?: boolean;
          models?: { router?: string; budget?: string; fast?: string; smart?: string; fallback?: string };
        };
        setAuthEnabled(Boolean(data.auth_enabled));
        setJwtEnabled(Boolean(data.jwt_enabled));
        setRegistrationAllowed(data.registration_allowed !== false);
        if (data.models) {
          setStatusModels(data.models);
        }
      }
    } catch {
      // Leave status flags as-is if /v1/status is unreachable.
    }
  }

  // Reuses the same /v1/usage the Usage panel calls — refreshed after every
  // paid action (Ask/Regenerate/Continue/Compare/Speak/Transcribe) rather
  // than accumulated client-side, so it can never drift from the server's
  // own ledger. Drives both the low-budget warning banner (only ever the
  // caller's OWN remaining room under DAILY_BUDGET_PER_OWNER_USD, never the
  // live global spend — that stays private to the operator) and the
  // persistent 💰 sidebar spend indicator.
  async function refreshUsageIndicators() {
    try {
      const res = await authFetch(`${API_BASE}/v1/usage?days=1`, { headers: requestHeaders() });
      if (!res.ok) {
        return;
      }
      const data = (await res.json()) as {
        today_usd: number;
        daily_budget_usd: number | null;
        daily_budget_per_owner_usd: number | null;
        owner_remaining_usd: number | null;
        avoided_cost_today_usd: number;
      };

      setTodaySpend(data.today_usd);
      setTodayCap(data.daily_budget_per_owner_usd ?? data.daily_budget_usd ?? null);
      setTodayAvoidedCost(data.avoided_cost_today_usd);

      if (data.daily_budget_per_owner_usd === null || data.owner_remaining_usd === null) {
        setBudgetWarning(null);
        return;
      }
      const ratio =
        data.daily_budget_per_owner_usd > 0
          ? data.owner_remaining_usd / data.daily_budget_per_owner_usd
          : 0;
      if (ratio <= 0.15) {
        setBudgetWarning(
          `Only ${formatCost(data.owner_remaining_usd) || "$0.00"} left of your ${formatCost(data.daily_budget_per_owner_usd) || "$0.00"} daily budget today.`,
        );
      } else {
        setBudgetWarning(null);
      }
    } catch {
      // Leave any existing warning/spend figure as-is if /v1/usage is
      // unreachable — stale-but-true beats silently dropping it on a blip.
    }
  }

  async function refreshMe() {
    try {
      const res = await authFetch(`${API_BASE}/v1/auth/me`, { headers: requestHeaders() });
      if (res.ok) {
        const data = (await res.json()) as {
          username?: string | null;
          is_admin?: boolean;
          must_change_password?: boolean;
        };
        setMe(data.username ?? null);
        setMustChangePassword(Boolean(data.must_change_password));
      } else {
        setMe(null);
        setMustChangePassword(false);
      }
    } catch {
      setMe(null);
      setMustChangePassword(false);
    }
  }

  async function submitAuth(register: boolean) {
    const username = loginUsername.trim();
    const password = loginPassword;
    setAuthMessage(null);
    if (!username || !password) {
      setAuthMessage("Enter a username and password.");
      return;
    }

    setAuthBusy(true);
    try {
      if (register) {
        const res = await fetch(`${API_BASE}/v1/auth/register`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username, password }),
        });
        if (!res.ok) {
          const body = (await res.json().catch(() => ({}))) as { detail?: string };
          throw new Error(body.detail ?? "Registration failed");
        }
      }

      const res = await fetch(`${API_BASE}/v1/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string };
        throw new Error(body.detail ?? "Login failed");
      }

      const data = (await res.json()) as {
        access_token: string;
        must_change_password?: boolean;
      };
      setToken(data.access_token);
      setMe(username);
      setMustChangePassword(Boolean(data.must_change_password));
      setLoginUsername("");
      setLoginPassword("");
      showStatus(`Signed in as ${username}`);
    } catch (error) {
      setAuthMessage(error instanceof Error ? error.message : "Authentication failed");
    } finally {
      setAuthBusy(false);
    }
  }

  function logout() {
    // Best-effort server-side revocation so the token can't be reused elsewhere;
    // clear local state regardless of whether the call succeeds.
    if (token.trim()) {
      void fetch(`${API_BASE}/v1/auth/logout`, {
        method: "POST",
        headers: requestHeaders(),
      }).catch(() => {});
    }
    setToken("");
    setMe(null);
    setMustChangePassword(false);
    setSelectedConversationId(null);
    setConversations([]);
    setMessages([]);
    setLoginUsername("");
    setLoginPassword("");
    showStatus("Signed out.");
  }

  useEffect(() => {
    if (token) {
      window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
    } else {
      window.localStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  }, [token]);

  // The title-flash fallback (see handleFrame's "done" branch) needs to be
  // reverted once the user actually comes back to the tab — otherwise it
  // would linger showing "New reply" long after it's been read.
  useEffect(() => {
    function onVisibilityChange() {
      if (!document.hidden) {
        document.title = BASE_DOCUMENT_TITLE;
      }
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, []);

  // Debounced conversation search: waits for a pause in typing, then asks the
  // backend to search titles + message content. Guards against out-of-order
  // responses the same way the message-load effect below does.
  useEffect(() => {
    const query = searchQuery.trim();
    if (!query) {
      return;
    }

    let cancelled = false;
    const timer = window.setTimeout(async () => {
      if (cancelled) return;
      setSearching(true);
      try {
        const res = await authFetch(`${API_BASE}/v1/search?q=${encodeURIComponent(query)}`, {
          headers: requestHeaders(),
        });
        if (cancelled) return;
        setSearchResults(res.ok ? ((await res.json()) as SearchResult[]) : []);
      } catch {
        if (!cancelled) setSearchResults([]);
      } finally {
        if (!cancelled) setSearching(false);
      }
    }, 300);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchQuery, token]);

  // Debounced live cost preview: waits for a pause in typing, then asks the
  // backend what THIS question would cost if sent right now (POST
  // /v1/estimate — no model or classifier call, just the same worst-case
  // math the DAILY_BUDGET_USD gate itself uses on dispatch). Guards against
  // out-of-order responses the same way the search debounce above does, so a
  // slow response for an earlier keystroke can't clobber a newer one.
  useEffect(() => {
    const text = question.trim();
    if (!text) {
      // Cleared imperatively in the textarea's onChange instead of here —
      // calling setState synchronously in an effect body triggers an extra
      // cascading render (see react-hooks/set-state-in-effect).
      return;
    }

    let cancelled = false;
    const timer = window.setTimeout(async () => {
      if (cancelled) return;
      try {
        const res = await authFetch(`${API_BASE}/v1/estimate`, {
          method: "POST",
          headers: requestHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ question: text, mode }),
        });
        if (cancelled) return;
        if (!res.ok) {
          setCostPreview(null);
          return;
        }
        const data = (await res.json()) as {
          model: string;
          input_tokens_estimate: number;
          output_tokens_estimate: number;
          cost_usd_estimate: number | null;
        };
        setCostPreview(data);
      } catch {
        if (!cancelled) setCostPreview(null);
      }
    }, 400);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [question, mode, token]);

  function selectSearchResult(conversationId: number) {
    setSelectedConversationId(conversationId);
    setSearchQuery("");
    setSearchResults([]);
  }

  // Global keyboard shortcuts. Ctrl/Cmd+K jumps into search from anywhere.
  // Alt+N (Option+N on Mac) starts a new conversation and focuses the
  // composer — Ctrl/Cmd+N is a browser-reserved "new window" shortcut that
  // page JavaScript can't intercept, so this uses Alt instead, which isn't
  // claimed by any mainstream browser. Alt+B opens Bookmarks the same way
  // (Ctrl/Cmd+Shift+B is reserved by Chrome/Firefox for the bookmarks bar
  // toggle, so this avoids that combo too). Escape backs out of whatever's open,
  // most-local first: the Instructions panel, an in-progress edit, then an
  // active search — skipped entirely while Settings/Usage/Compare/Bookmarks
  // are open, since those modals own Escape themselves.
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        if (settingsOpen || usageOpen || compareOpen || bookmarksOpen || templatesOpen || libraryOpen || summarizeOpen || shortcutsHelpOpen || shareOpen || headerMenuOpen || mustChangePassword) {
          return;
        }
        event.preventDefault();
        searchInputRef.current?.focus();
        return;
      }

      if (event.altKey && event.key.toLowerCase() === "n") {
        if (settingsOpen || usageOpen || compareOpen || bookmarksOpen || templatesOpen || libraryOpen || summarizeOpen || shortcutsHelpOpen || shareOpen || headerMenuOpen || mustChangePassword) {
          return;
        }
        event.preventDefault();
        void createConversation().then(() => {
          questionInputRef.current?.focus();
        });
        return;
      }

      if (event.altKey && event.key.toLowerCase() === "b") {
        if (settingsOpen || usageOpen || compareOpen || bookmarksOpen || templatesOpen || libraryOpen || summarizeOpen || shortcutsHelpOpen || shareOpen || headerMenuOpen || mustChangePassword) {
          return;
        }
        event.preventDefault();
        setBookmarksOpen(true);
        return;
      }

      if (event.key === "?") {
        // Only outside a text field — otherwise typing a literal "?" into the
        // composer or a rename prompt would pop this open every time.
        const target = event.target as HTMLElement | null;
        const isTyping =
          target?.tagName === "INPUT" || target?.tagName === "TEXTAREA" || target?.isContentEditable;
        if (isTyping || settingsOpen || usageOpen || compareOpen || bookmarksOpen || templatesOpen || libraryOpen || summarizeOpen || shortcutsHelpOpen || shareOpen || headerMenuOpen || mustChangePassword) {
          return;
        }
        event.preventDefault();
        setShortcutsHelpOpen(true);
        return;
      }

      if (event.key === "Escape") {
        if (settingsOpen || usageOpen || compareOpen || bookmarksOpen || templatesOpen || libraryOpen || summarizeOpen || shortcutsHelpOpen || shareOpen || headerMenuOpen || mustChangePassword) {
          return;
        }
        if (instructionsOpen) {
          cancelInstructions();
          return;
        }
        if (editingMessageId !== null) {
          cancelEdit();
          return;
        }
        if (searchQuery.trim()) {
          setSearchQuery("");
          setSearchResults([]);
          setSearching(false);
        }
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    settingsOpen,
    usageOpen,
    shareOpen,
    compareOpen,
    bookmarksOpen,
    templatesOpen,
    libraryOpen,
    summarizeOpen,
    shortcutsHelpOpen,
    mustChangePassword,
    instructionsOpen,
    editingMessageId,
    searchQuery,
    title,
  ]);

  useEffect(() => {
    const load = async () => {
      await refreshStatus();
    };
    void load();
    void refreshUsageIndicators();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Captured once, synchronously, on the very first render — reading
  // window.location.search fresh inside the async load() below would race
  // the ?c= sync effect further down (which fires in the same pass and
  // would already have overwritten/cleared it by the time an `await`
  // resumes).
  const [initialUrlConversationId] = useState(() => {
    const parsed = Number(new URLSearchParams(window.location.search).get("c"));
    return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined;
  });
  // Same capture-once approach, for an optional &m=<messageId> that scrolls
  // to and briefly highlights one specific message once its conversation's
  // messages have loaded (see the effect below). Feeds into the same
  // pendingMessageTargetId a Bookmarks-row click also sets — a
  // copy-link-to-message target, not an ongoing piece of selection state
  // like ?c=, so it's consumed once (by the effect nulling it back out)
  // rather than kept in sync.
  const [initialUrlMessageId] = useState(() => {
    const parsed = Number(new URLSearchParams(window.location.search).get("m"));
    return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined;
  });
  const [pendingMessageTargetId, setPendingMessageTargetId] = useState<number | null>(
    initialUrlMessageId ?? null,
  );
  const [deepLinkHighlightId, setDeepLinkHighlightId] = useState<number | null>(null);

  // Reload the (per-user) conversation list and current identity whenever the
  // credential changes — login and logout both flow through here. Prefers
  // whatever conversation the URL named on load (see the ?c= sync effect
  // below) so a refreshed, bookmarked, or shared link lands back on the same
  // conversation instead of always falling back to the default pick —
  // loadConversations already no-ops back to that default if the id isn't
  // found (wrong user, deleted, or no ?c= at all).
  useEffect(() => {
    const load = async () => {
      await refreshMe();
      try {
        await loadConversations(initialUrlConversationId);
      } catch (error) {
        showStatus(error instanceof Error ? error.message : "Backend not reachable", {
          error: true,
        });
      }
    };
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  // Keeps the URL's ?c= in sync with the current selection (replaceState, not
  // pushState — a bookmarkable/shareable/refresh-safe link is the goal here,
  // not a browser-back-steps-through-every-conversation history).
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const current = params.get("c");
    const next = selectedConversationId ? String(selectedConversationId) : null;
    if (current === next) {
      return;
    }
    const url = new URL(window.location.href);
    if (next) {
      url.searchParams.set("c", next);
    } else {
      url.searchParams.delete("c");
    }
    window.history.replaceState(null, "", url);
  }, [selectedConversationId]);

  // Once a pending message target (from &m= on load, or a Bookmarks-row
  // click — see jumpToMessage) actually shows up in the loaded list (the
  // conversation fetch this depends on is async, so it won't be there
  // immediately), scroll to it and flash a highlight. Consumed by nulling
  // pendingMessageTargetId back out — messages reloads for other reasons too
  // (regenerate, edit, a new answer streaming in), and re-triggering the
  // scroll/flash on every one of those would be a jarring surprise, not a
  // "you arrived via a link" cue. &m= is stripped from the URL if present,
  // so a later, unrelated conversation switch doesn't drag a stale target
  // along in the address bar.
  useEffect(() => {
    if (!pendingMessageTargetId) {
      return;
    }
    if (!messages.some((message) => message.id === pendingMessageTargetId)) {
      return;
    }
    const targetId = pendingMessageTargetId;
    if (new URLSearchParams(window.location.search).has("m")) {
      const url = new URL(window.location.href);
      url.searchParams.delete("m");
      window.history.replaceState(null, "", url);
    }

    // Deferred a tick (queueMicrotask) per react-hooks/set-state-in-effect —
    // still runs before the next paint, so there's no visible delay.
    queueMicrotask(() => {
      setPendingMessageTargetId(null);
      setDeepLinkHighlightId(targetId);
    });
    const scrollTimer = window.setTimeout(() => {
      messagesContainerRef.current
        ?.querySelector(`[data-message-id="${targetId}"]`)
        ?.scrollIntoView({ block: "center" });
    }, 50);
    const clearTimer = window.setTimeout(() => setDeepLinkHighlightId(null), 2500);
    return () => {
      window.clearTimeout(scrollTimer);
      window.clearTimeout(clearTimer);
    };
  }, [messages, pendingMessageTargetId]);

  // Flushes the OUTGOING conversation's unsent question to its own draft slot
  // (using question as it stood in this same render, before switching), then
  // restores the INCOMING conversation's draft, if any. Runs synchronously on
  // every switch — not debounced — so a fast switch can't race past a
  // pending debounce and lose (or, worse, leak into the wrong conversation)
  // whatever was half-typed.
  useEffect(() => {
    const outgoingId = prevConversationIdRef.current;
    if (outgoingId !== null) {
      const drafts = loadDraftMap();
      setDraft(drafts, outgoingId, question);
      saveDraftMap(drafts);
    }
    prevConversationIdRef.current = selectedConversationId;
    const incomingId = selectedConversationId;
    // Deferred a tick (queueMicrotask, not called synchronously in the effect
    // body) per react-hooks/set-state-in-effect — this still runs before the
    // next paint, so there's no visible flash of the wrong draft.
    queueMicrotask(() => {
      const incomingDrafts = loadDraftMap();
      setQuestion(incomingId !== null ? (incomingDrafts[String(incomingId)] ?? "") : "");
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedConversationId]);

  // Debounced save while staying on the SAME conversation, so a page reload
  // (not a switch — that's the effect above) doesn't lose an in-progress
  // draft either. Re-armed on every keystroke; harmless if it also re-fires
  // right after a switch (it just re-saves the just-restored value).
  useEffect(() => {
    if (!selectedConversationId) {
      return;
    }
    const timer = window.setTimeout(() => {
      const drafts = loadDraftMap();
      setDraft(drafts, selectedConversationId, question);
      saveDraftMap(drafts);
    }, 400);
    return () => window.clearTimeout(timer);
  }, [question, selectedConversationId]);

  useEffect(() => {
    // Guard against out-of-order responses: if the user switches conversations
    // again before this fetch resolves, discard the stale result.
    let cancelled = false;
    forceScrollRef.current = true;
    const load = async () => {
      if (!selectedConversationId) {
        if (!cancelled) {
          setMessages([]);
        }
        return;
      }
      try {
        const res = await authFetch(`${API_BASE}/v1/conversations/${selectedConversationId}/messages`, {
          headers: requestHeaders(),
        });
        if (!res.ok) throw new Error("Failed to load messages");
        const data = (await res.json()) as Message[];
        if (!cancelled) {
          setMessages(data);
        }
      } catch (error) {
        if (!cancelled) {
          showStatus(error instanceof Error ? error.message : "Failed to load messages", {
            error: true,
          });
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedConversationId]);

  // Remembers whatever was selected immediately before this — a single-step
  // "last conversation" rather than a full history stack, updated on every
  // change regardless of what caused it (row click, search, arrow-key nav,
  // create, delete-fallback, etc.), since they all funnel through this one
  // piece of state.
  useEffect(() => {
    const last = lastSelectedConversationIdRef.current;
    if (last !== null && last !== selectedConversationId) {
      setPreviousConversationId(last);
    }
    lastSelectedConversationIdRef.current = selectedConversationId;
  }, [selectedConversationId]);

  useEffect(() => {
    const anchor = messagesEndRef.current;
    if (!anchor) {
      return;
    }
    if (forceScrollRef.current) {
      // A conversation was just selected/loaded — always jump to the latest
      // message rather than leaving scrollTop wherever the previous
      // conversation left it (which could land on an old message, or even
      // freeze the pane if it happens to reject the initial scroll below).
      forceScrollRef.current = false;
      anchor.scrollIntoView({ block: "end" });
      return;
    }
    // Only follow the tail when the user is already near the bottom, so
    // reading back through history mid-stream isn't yanked down on every
    // delta. .messages is the actual scrolling element (see App.css), so
    // distance is measured off its own scrollTop, not the document's.
    const container = messagesContainerRef.current;
    const distanceFromBottom = container
      ? container.scrollHeight - container.scrollTop - container.clientHeight
      : 0;
    if (distanceFromBottom < 120) {
      anchor.scrollIntoView({ block: "end" });
    }
  }, [messages, streamState]);

  // Shows a "jump to latest" button once the user has scrolled far enough up
  // to lose the tail. Listens on .messages' own scroll event (it's the
  // scrolling element, not the window), covering both manual scrolling and
  // the programmatic scrollIntoView calls above (which fire a native scroll
  // event too), so a single mount-time listener is enough.
  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) {
      return;
    }
    function updateJumpToBottom() {
      if (!container) return;
      const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
      setShowJumpToBottom(distance > 200);
    }
    container.addEventListener("scroll", updateJumpToBottom, { passive: true });
    updateJumpToBottom();
    return () => container.removeEventListener("scroll", updateJumpToBottom);
  }, []);

  // Message ids whose content matches the find query, in conversation order —
  // recomputed whenever the query or the message list changes. Distinct from
  // the sidebar's Ctrl+K search, which finds a conversation but doesn't
  // scroll to or highlight anything inside it.
  const findMatchIds = findQuery.trim()
    ? messages
        .filter((message) => message.content.toLowerCase().includes(findQuery.trim().toLowerCase()))
        .map((message) => message.id)
    : [];

  useEffect(() => {
    if (findMatchIds.length === 0) {
      return;
    }
    const activeId = findMatchIds[findActiveIndex % findMatchIds.length];
    const element = messagesContainerRef.current?.querySelector(`[data-message-id="${activeId}"]`);
    element?.scrollIntoView({ block: "center" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [findActiveIndex, findMatchIds.join(",")]);

  function openFind() {
    setFindOpen(true);
    window.setTimeout(() => findInputRef.current?.focus(), 0);
  }

  function closeFind() {
    setFindOpen(false);
    setFindQuery("");
  }

  function findNext() {
    if (findMatchIds.length === 0) {
      return;
    }
    setFindActiveIndex((current) => (current + 1) % findMatchIds.length);
  }

  function findPrev() {
    if (findMatchIds.length === 0) {
      return;
    }
    setFindActiveIndex((current) => (current - 1 + findMatchIds.length) % findMatchIds.length);
  }

  const conversationTokens = messages.reduce(
    (sum, message) => sum + (message.input_tokens ?? 0) + (message.output_tokens ?? 0),
    0,
  );
  const conversationCost = messages.reduce((sum, message) => sum + (message.cost_usd ?? 0), 0);

  // Union of every tag across the currently-loaded conversations, for the
  // filter dropdown — client-side, like the rest of tag filtering, since the
  // conversation list is already fully loaded.
  const allTags = Array.from(
    new Set(conversations.flatMap((conversation) => conversation.tags ?? [])),
  ).sort();
  // "recent" is a no-op sort (Array.prototype.sort is stable), leaving the
  // backend's favorite-then-recency order from list_conversations() intact.
  // "name" re-sorts alphabetically, ignoring that grouping — an alternate
  // view rather than an additional grouping layer on top of it.
  const visibleConversations = conversations
    .filter((conversation) => !tagFilter || (conversation.tags ?? []).includes(tagFilter))
    .filter((conversation) => !favoritesOnly || conversation.favorite)
    .sort((a, b) =>
      sortOrder === "name" ? a.title.localeCompare(b.title, undefined, { sensitivity: "base" }) : 0,
    );

  // Only offer "Back" while that conversation still exists — it may since
  // have been deleted, or filtered out isn't relevant here since Back should
  // still work even if a filter would otherwise hide it.
  const previousConversation = conversations.find((c) => c.id === previousConversationId);

  // The budget tier only exists when OPENAI_MODEL_BUDGET is configured server-side.
  const budgetTierEnabled = Boolean(statusModels.budget);

  // Distinct configured models offered as "force model" options when regenerating.
  const forcedModelOptions = Array.from(
    new Set(
      [
        statusModels.budget,
        statusModels.fast,
        statusModels.smart,
        statusModels.fallback,
        statusModels.router,
      ].filter((model): model is string => Boolean(model)),
    ),
  );
  const canRegenerate = messages.length > 0 && !streaming;

  // The conversation's model pin ("" = not pinned; "budget"/"fast"/"smart" = tier).
  const pinValue = selectedConversation?.pinned_model ?? "";
  const isPinned = Boolean(pinValue);
  const isTierPin = pinValue === "budget" || pinValue === "fast" || pinValue === "smart";
  // Always include the current pinned model as an option, even if it isn't one
  // of the configured tier models, so the selector reflects the real value.
  const pinModelOptions = Array.from(
    new Set(pinValue && !isTierPin ? [...forcedModelOptions, pinValue] : forcedModelOptions),
  );

  // An admin-created/reset account is steered here before anything else in
  // the app — no sidebar, no conversations — until it sets its own password.
  if (jwtEnabled && me && mustChangePassword) {
    return (
      <main className="app-shell">
        <ChangePassword
          apiBase={API_BASE}
          getHeaders={requestHeaders}
          username={me}
          onChanged={() => {
            setMustChangePassword(false);
            showStatus("Password changed.");
          }}
          onSignOut={logout}
        />
      </main>
    );
  }

  return (
    <main className="app-shell">
      <Sidebar
        todaySpend={todaySpend}
        todayCap={todayCap}
        todayAvoidedCost={todayAvoidedCost}
        notifyEnabled={notifyEnabled}
        toggleNotify={toggleNotify}
        notifySoundEnabled={notifySoundEnabled}
        setNotifySoundEnabled={setNotifySoundEnabled}
        theme={theme}
        setTheme={setTheme}
        setShortcutsHelpOpen={setShortcutsHelpOpen}
        title={title}
        setTitle={setTitle}
        createConversation={createConversation}
        busy={busy}
        importFileInputRef={importFileInputRef}
        importConversation={importConversation}
        importing={importing}
        exportAllConversations={exportAllConversations}
        exportingAll={exportingAll}
        previousConversation={previousConversation}
        selectedConversationId={selectedConversationId}
        setSelectedConversationId={setSelectedConversationId}
        searchInputRef={searchInputRef}
        searchQuery={searchQuery}
        setSearchQuery={setSearchQuery}
        setSearchResults={setSearchResults}
        setSearching={setSearching}
        isMac={IS_MAC}
        allTags={allTags}
        tagFilter={tagFilter}
        setTagFilter={setTagFilter}
        sortOrder={sortOrder}
        setSortOrder={setSortOrder}
        showArchived={showArchived}
        toggleShowArchived={toggleShowArchived}
        favoritesOnly={favoritesOnly}
        setFavoritesOnly={setFavoritesOnly}
        bulkSelectMode={bulkSelectMode}
        toggleBulkSelectMode={toggleBulkSelectMode}
        bulkSelectedIds={bulkSelectedIds}
        exportSelectedConversations={exportSelectedConversations}
        exportingSelected={exportingSelected}
        bulkTagSelected={bulkTagSelected}
        bulkWorking={bulkWorking}
        bulkArchiveSelected={bulkArchiveSelected}
        bulkDeleteSelected={bulkDeleteSelected}
        searching={searching}
        searchResults={searchResults}
        selectSearchResult={selectSearchResult}
        visibleConversations={visibleConversations}
        toggleBulkSelected={toggleBulkSelected}
        toggleFavorite={toggleFavorite}
        jwtEnabled={jwtEnabled}
        me={me}
        logout={logout}
        loginUsername={loginUsername}
        setLoginUsername={setLoginUsername}
        loginPassword={loginPassword}
        setLoginPassword={setLoginPassword}
        submitAuth={submitAuth}
        authBusy={authBusy}
        registrationAllowed={registrationAllowed}
        authMessage={authMessage}
        usernameInputRef={usernameInputRef}
        authEnabled={authEnabled}
        token={token}
        setToken={setToken}
        tokenInputRef={tokenInputRef}
      />

      <section className="chat-panel">
        {jwtEnabled && !me ? (
          <div className="signin-required-banner" role="status">
            <span>🔒 Sign in required — this deployment needs an account to do anything here.</span>
            <button
              type="button"
              className="secondary-button"
              onClick={() => {
                usernameInputRef.current?.focus();
                usernameInputRef.current?.scrollIntoView({ block: "center" });
              }}
            >
              Sign in
            </button>
          </div>
        ) : !jwtEnabled && authEnabled && !token.trim() ? (
          <div className="signin-required-banner" role="status">
            <span>🔒 API token required — this deployment needs one to do anything here.</span>
            <button
              type="button"
              className="secondary-button"
              onClick={() => {
                tokenInputRef.current?.focus();
                tokenInputRef.current?.scrollIntoView({ block: "center" });
              }}
            >
              Enter token
            </button>
          </div>
        ) : null}
        <header className="chat-header">
          <div className="chat-header-title">
            <h2>{selectedConversation ? selectedConversation.title : "No conversation selected"}</h2>
            <p aria-live="polite" className={statusIsError ? "chat-status chat-status-error" : "chat-status"}>
              {status}
            </p>
            {undoDelete ? (
              <p className="undo-delete-banner" role="status">
                Deleted "{undoDelete.title}".{" "}
                <button type="button" className="link-button" onClick={() => void undoConversationDelete()}>
                  Undo
                </button>
              </p>
            ) : null}
            {undoMessageDelete ? (
              <p className="undo-delete-banner" role="status">
                Deleted this message.{" "}
                <button type="button" className="link-button" onClick={() => void undoMessageDeletion()}>
                  Undo
                </button>
              </p>
            ) : null}
            {/* The streaming bubble updates many times a second and isn't
                itself announced (that would be unusable) — this announces
                the complete answer once, when streaming finishes. */}
            <div aria-live="polite" className="sr-only">
              {srAnswerAnnouncement}
            </div>
            {conversationTokens > 0 ? (
              <p className="conversation-total">
                {conversationTokens.toLocaleString()} tokens
                {formatCost(conversationCost) ? ` · ~${formatCost(conversationCost)}` : ""} this
                conversation
              </p>
            ) : null}
          </div>

          <div className="header-actions">
            <select
              value={mode}
              onChange={(event) => setMode(event.target.value as Mode)}
              aria-label="Routing mode"
              disabled={isPinned}
              title={isPinned ? "This conversation is pinned; clear the pin to route by mode." : undefined}
            >
              <option value="auto">auto</option>
              {budgetTierEnabled ? <option value="budget">budget</option> : null}
              <option value="fast">fast</option>
              <option value="smart">smart</option>
              <option value="workflow">workflow</option>
            </select>

            <select
              value={pinValue}
              onChange={(event) => setPin(event.target.value)}
              aria-label="Pinned model"
              disabled={!selectedConversation}
              title="Pin a model or tier to this conversation"
            >
              <option value="">📌 not pinned</option>
              {budgetTierEnabled ? <option value="budget">📌 budget tier</option> : null}
              <option value="fast">📌 fast tier</option>
              <option value="smart">📌 smart tier</option>
              {pinModelOptions.map((model) => (
                <option key={model} value={model}>
                  📌 {model}
                </option>
              ))}
            </select>

            <button
              className="secondary-button"
              onClick={openInstructions}
              disabled={!selectedConversation}
              title="Custom instructions (persona/style/rules) for this conversation"
            >
              Instructions{selectedConversation?.system_prompt ? " ●" : ""}
            </button>

            <button
              className="secondary-button"
              onClick={openFind}
              disabled={!selectedConversation || messages.length === 0}
              title="Find text within this conversation"
            >
              🔎 Find
            </button>

            <HeaderOverflowMenu open={headerMenuOpen} onOpenChange={setHeaderMenuOpen}>
              <select
                value=""
                onChange={(event) => {
                  const format = event.target.value;
                  if (format === "markdown" || format === "json") {
                    exportConversation(format);
                  } else if (format === "pdf") {
                    exportConversationAsPdf();
                  } else if (format === "copy-markdown") {
                    void copyConversationAsMarkdown();
                  } else if (format === "copy-link") {
                    void copyConversationLink();
                  }
                  event.target.value = "";
                  setHeaderMenuOpen(false);
                }}
                aria-label="Export conversation"
                disabled={!selectedConversation || messages.length === 0}
                title="Export this conversation"
              >
                <option value="" disabled>
                  ⬇️ Export
                </option>
                <option value="markdown">Markdown (.md)</option>
                <option value="json">JSON (.json)</option>
                <option value="pdf">PDF (print)</option>
                <option value="copy-markdown">📋 Copy as Markdown</option>
                <option value="copy-link">🔗 Copy link</option>
              </select>

              <button
                role="menuitem"
                className="secondary-button"
                onClick={() => {
                  setCompareOpen(true);
                  setHeaderMenuOpen(false);
                }}
              >
                Compare
              </button>

              <button
                role="menuitem"
                className="secondary-button"
                onClick={() => {
                  setUsageOpen(true);
                  setHeaderMenuOpen(false);
                }}
              >
                Usage
              </button>

              <button
                role="menuitem"
                className="secondary-button"
                onClick={() => {
                  setShareOpen(true);
                  setHeaderMenuOpen(false);
                }}
                disabled={!selectedConversation}
                title="Get a read-only link to this conversation"
              >
                🔗 Share
              </button>

              <button
                role="menuitem"
                className="secondary-button"
                onClick={() => {
                  setBookmarksOpen(true);
                  setHeaderMenuOpen(false);
                }}
              >
                Bookmarks
              </button>

              <button
                role="menuitem"
                className="secondary-button"
                onClick={() => {
                  setTemplatesOpen(true);
                  setHeaderMenuOpen(false);
                }}
              >
                📝 Templates
              </button>

              <button
                role="menuitem"
                className="secondary-button"
                onClick={() => {
                  setLibraryOpen(true);
                  setHeaderMenuOpen(false);
                }}
              >
                📚 Library
              </button>

              <button
                role="menuitem"
                className="secondary-button"
                onClick={() => {
                  setSummarizeOpen(true);
                  setHeaderMenuOpen(false);
                }}
                disabled={!selectedConversation || messages.length === 0}
                title="Summarize this conversation"
              >
                🧾 Summarize
              </button>

              <button
                role="menuitem"
                className="secondary-button"
                onClick={() => {
                  setSettingsOpen(true);
                  setHeaderMenuOpen(false);
                }}
              >
                Settings
              </button>

              <button
                role="menuitem"
                className="secondary-button"
                onClick={() => {
                  void renameConversation();
                  setHeaderMenuOpen(false);
                }}
                disabled={busy || !selectedConversation}
              >
                Rename
              </button>

              <button
                role="menuitem"
                className="secondary-button"
                onClick={() => {
                  void editTags();
                  setHeaderMenuOpen(false);
                }}
                disabled={busy || !selectedConversation}
              >
                Tags{selectedConversation?.tags?.length ? ` (${selectedConversation.tags.length})` : ""}
              </button>

              <button
                role="menuitem"
                className="secondary-button"
                onClick={() => {
                  void duplicateConversation();
                  setHeaderMenuOpen(false);
                }}
                disabled={busy || !selectedConversation}
              >
                Duplicate
              </button>

              <button
                role="menuitem"
                className="secondary-button"
                onClick={() => {
                  void archiveConversation();
                  setHeaderMenuOpen(false);
                }}
                disabled={busy || !selectedConversation}
              >
                {selectedConversation?.archived ? "Unarchive" : "Archive"}
              </button>

              <button
                role="menuitem"
                className="danger-button"
                onClick={() => {
                  void deleteConversation();
                  setHeaderMenuOpen(false);
                }}
                disabled={busy || !selectedConversation}
              >
                Delete
              </button>
            </HeaderOverflowMenu>
          </div>
        </header>

        {findOpen ? (
          <div className="find-bar">
            <input
              ref={findInputRef}
              type="text"
              value={findQuery}
              onChange={(event) => {
                setFindQuery(event.target.value);
                setFindActiveIndex(0);
              }}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.stopPropagation();
                  closeFind();
                } else if (event.key === "Enter") {
                  event.preventDefault();
                  if (event.shiftKey) {
                    findPrev();
                  } else {
                    findNext();
                  }
                }
              }}
              placeholder="Find in this conversation…"
              aria-label="Find in conversation"
            />
            <span className="find-count">
              {findQuery.trim()
                ? findMatchIds.length > 0
                  ? `${(findActiveIndex % findMatchIds.length) + 1} of ${findMatchIds.length}`
                  : "No matches"
                : ""}
            </span>
            <button
              type="button"
              className="secondary-button"
              onClick={findPrev}
              disabled={findMatchIds.length === 0}
              aria-label="Previous match"
            >
              ↑
            </button>
            <button
              type="button"
              className="secondary-button"
              onClick={findNext}
              disabled={findMatchIds.length === 0}
              aria-label="Next match"
            >
              ↓
            </button>
            <button
              type="button"
              className="link-button"
              onClick={closeFind}
              aria-label="Close find"
            >
              ✕
            </button>
          </div>
        ) : null}

        {instructionsOpen ? (
          <div className="instructions-panel">
            <label htmlFor="instructions-draft">
              Custom instructions for this conversation
            </label>
            <textarea
              id="instructions-draft"
              value={instructionsDraft}
              onChange={(event) => setInstructionsDraft(event.target.value)}
              placeholder="e.g. Always answer in French. Keep responses under 3 sentences."
              rows={4}
              disabled={instructionsSaving}
            />
            <div className="instructions-actions">
              <button onClick={() => void saveInstructions()} disabled={instructionsSaving}>
                Save
              </button>
              <button
                type="button"
                className="secondary-button"
                onClick={cancelInstructions}
                disabled={instructionsSaving}
              >
                Cancel
              </button>
            </div>
          </div>
        ) : null}

        <MessageList
          messages={messages}
          streaming={streaming}
          streamState={streamState}
          conversations={conversations}
          selectedConversation={selectedConversation}
          findMatchIds={findMatchIds}
          findActiveIndex={findActiveIndex}
          deepLinkHighlightId={deepLinkHighlightId}
          copiedMessageId={copiedMessageId}
          copyMessage={copyMessage}
          copiedLinkMessageId={copiedLinkMessageId}
          copyMessageLink={copyMessageLink}
          toggleMessageBookmark={toggleMessageBookmark}
          rateMessage={rateMessage}
          synthesizingMessageId={synthesizingMessageId}
          speakingMessageId={speakingMessageId}
          toggleSpeak={toggleSpeak}
          freeSpeakingMessageId={freeSpeakingMessageId}
          toggleFreeSpeak={toggleFreeSpeak}
          editingMessageId={editingMessageId}
          startEdit={startEdit}
          busy={busy}
          branchingMessageId={branchingMessageId}
          branchFromMessage={branchFromMessage}
          deletingMessageId={deletingMessageId}
          deleteMessage={deleteMessage}
          continuingMessageId={continuingMessageId}
          continueMessage={continueMessage}
          editDraft={editDraft}
          setEditDraft={setEditDraft}
          saveEdit={saveEdit}
          cancelEdit={cancelEdit}
          resolveAction={resolveAction}
          unansweredNotice={unansweredNotice}
          selectedConversationId={selectedConversationId}
          canRegenerate={canRegenerate}
          regenerate={regenerate}
          isPinned={isPinned}
          regenChoice={regenChoice}
          setRegenChoice={setRegenChoice}
          budgetTierEnabled={budgetTierEnabled}
          forcedModelOptions={forcedModelOptions}
          messagesEndRef={messagesEndRef}
          messagesContainerRef={messagesContainerRef}
          showJumpToBottom={showJumpToBottom}
          insertIntoComposer={(text) =>
            setQuestion((current) => (current.trim() ? `${current}\n${text}` : text))
          }
        />

        <Composer
          attachedImages={attachedImages}
          attachedFiles={attachedFiles}
          attachedAudio={attachedAudio}
          removeAttachedImage={removeAttachedImage}
          removeAttachedFile={removeAttachedFile}
          removeAttachedAudio={removeAttachedAudio}
          budgetWarning={budgetWarning}
          costPreview={costPreview}
          question={question}
          dragActive={dragActive}
          setDragActive={setDragActive}
          handleFilesSelected={handleFilesSelected}
          fileInputRef={fileInputRef}
          maxAttachedImages={MAX_ATTACHED_IMAGES}
          maxAttachedFiles={MAX_ATTACHED_FILES}
          maxAttachedAudio={MAX_ATTACHED_AUDIO}
          recording={recording}
          toggleRecording={toggleRecording}
          transcribing={transcribing}
          freeRecording={freeRecording}
          toggleFreeRecording={toggleFreeRecording}
          researchMode={researchMode}
          setResearchMode={setResearchMode}
          questionInputRef={questionInputRef}
          setQuestion={setQuestion}
          setCostPreview={setCostPreview}
          askQuestion={askQuestion}
          streaming={streaming}
          stopStreaming={stopStreaming}
          loading={loading}
        />
      </section>

      <Suspense fallback={null}>
        {settingsOpen ? (
          <ErrorBoundary label="Settings">
            <Settings
              apiBase={API_BASE}
              getHeaders={requestHeaders}
              onClose={() => setSettingsOpen(false)}
              onChanged={() => {
                void refreshStatus();
              }}
            />
          </ErrorBoundary>
        ) : null}

        {usageOpen ? (
          <ErrorBoundary label="Usage">
            <Usage apiBase={API_BASE} getHeaders={requestHeaders} onClose={() => setUsageOpen(false)} />
          </ErrorBoundary>
        ) : null}

        {shareOpen && selectedConversationId ? (
          <ErrorBoundary label="Share">
            <Share
              apiBase={API_BASE}
              getHeaders={requestHeaders}
              conversationId={selectedConversationId}
              onClose={() => setShareOpen(false)}
            />
          </ErrorBoundary>
        ) : null}

        {bookmarksOpen ? (
          <ErrorBoundary label="Bookmarks">
            <Bookmarks
              apiBase={API_BASE}
              getHeaders={requestHeaders}
              onClose={() => setBookmarksOpen(false)}
              onSelectMessage={(conversationId, messageId) => {
                setSelectedConversationId(conversationId);
                setPendingMessageTargetId(messageId);
              }}
            />
          </ErrorBoundary>
        ) : null}

        {templatesOpen ? (
          <ErrorBoundary label="Templates">
            <Templates
              apiBase={API_BASE}
              getHeaders={requestHeaders}
              onClose={() => setTemplatesOpen(false)}
              onInsert={(content) => {
                setQuestion((current) => (current.trim() ? `${current}\n${content}` : content));
                queueMicrotask(() => questionInputRef.current?.focus());
              }}
            />
          </ErrorBoundary>
        ) : null}

        {libraryOpen ? (
          <ErrorBoundary label="Library">
            <Library
              apiBase={API_BASE}
              getHeaders={requestHeaders}
              onClose={() => setLibraryOpen(false)}
            />
          </ErrorBoundary>
        ) : null}

        {summarizeOpen && selectedConversationId ? (
          <ErrorBoundary label="Summarize">
            <Summarize
              apiBase={API_BASE}
              getHeaders={requestHeaders}
              conversationId={selectedConversationId}
              onClose={() => setSummarizeOpen(false)}
            />
          </ErrorBoundary>
        ) : null}

        {compareOpen ? (
          <ErrorBoundary label="Compare">
            <Compare
              apiBase={API_BASE}
              getHeaders={requestHeaders}
              availableModels={forcedModelOptions}
              onClose={() => setCompareOpen(false)}
              onOpenConversation={(conversationId) => {
                void loadConversations(conversationId);
              }}
              onCostIncurred={() => void refreshUsageIndicators()}
            />
          </ErrorBoundary>
        ) : null}
      </Suspense>

      {shortcutsHelpOpen ? (
        <ErrorBoundary label="Keyboard shortcuts">
          <ShortcutsHelp isMac={IS_MAC} onClose={() => setShortcutsHelpOpen(false)} />
        </ErrorBoundary>
      ) : null}
    </main>
  );
}

export default App;
