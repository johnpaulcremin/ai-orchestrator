import { type ComponentPropsWithoutRef, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { extractSseFrames, type SseFrame } from "./sse";
import { formatTimestamp, formatCost } from "./format";
import { Compare } from "./Compare";
import { Settings } from "./Settings";
import { Usage } from "./Usage";
import "./App.css";

type Mode = "auto" | "budget" | "fast" | "smart";

type Conversation = {
  id: number;
  title: string;
  owner?: string | null;
  pinned_model?: string | null;
  system_prompt?: string | null;
  created_at: string;
  updated_at: string;
};

type SearchResult = Conversation & {
  snippet: string;
};

type Source = {
  title: string;
  url: string;
};

type PendingAction = {
  action: string;
  summary: string;
  payload: Record<string, unknown>;
};

type ActionStatus = "pending" | "confirmed" | "declined" | "failed";

type FileAttachment = {
  filename: string;
  data: string;
};

type Message = {
  id: number;
  conversation_id: number;
  role: string;
  content: string;
  mode_used?: string | null;
  notes?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cost_usd?: number | null;
  cached?: boolean;
  sources?: Source[] | null;
  pending_action?: PendingAction | null;
  action_status?: ActionStatus | null;
  // For an assistant message: images the model generated. For a user
  // message: images the user attached (vision input).
  images?: string[] | null;
  // Documents (PDF/plain text) the user attached; always absent on assistant
  // messages — the model can read a file, never produce one.
  files?: FileAttachment[] | null;
  created_at: string;
};

type StreamState = {
  conversationId: number;
  question: string;
  answer: string;
  sources?: Source[] | null;
  pending_action?: PendingAction | null;
  images?: string[] | null;
  // Images/files the user attached to THIS question, distinct from `images`
  // above which is the model's generated output.
  questionImages?: string[] | null;
  questionFiles?: FileAttachment[] | null;
};

const MAX_ATTACHED_IMAGES = 4;
const MAX_ATTACHED_FILES = 4;
// Mirrors the backend's FileAttachment mime allowlist (schemas.py).
const ACCEPTED_FILE_MIMES = new Set(["application/pdf", "text/plain"]);
// Mirrors the backend's TranscribeRequest mime allowlist (schemas.py), in
// preference order — the first one the browser's MediaRecorder supports wins.
const PREFERRED_AUDIO_MIME_TYPES = ["audio/webm", "audio/ogg", "audio/mp4", "audio/wav"];

const API_BASE = "/api";
const TOKEN_STORAGE_KEY = "ai_workbench_token";
// Used only to label the search shortcut hint (⌘K vs Ctrl+K); the shortcut
// itself listens for either metaKey or ctrlKey regardless of platform.
const IS_MAC = typeof navigator !== "undefined" && /Mac|iPod|iPhone|iPad/.test(navigator.platform);

// Overrides ReactMarkdown's <pre> rendering for fenced code blocks (never
// matches inline `code`, which has no <pre> ancestor) to add a copy button.
function CodeBlock({ children, ...rest }: ComponentPropsWithoutRef<"pre">) {
  const [copied, setCopied] = useState(false);
  const preRef = useRef<HTMLPreElement>(null);

  async function handleCopy() {
    const text = preRef.current?.textContent ?? "";
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access can fail (permissions, insecure context); no status
      // update here — this is a nice-to-have, not worth interrupting the chat.
    }
  }

  return (
    <div className="code-block">
      <button
        type="button"
        className="code-copy-button"
        onClick={() => void handleCopy()}
        aria-label={copied ? "Copied!" : "Copy code"}
      >
        {copied ? "✓ Copied" : "Copy"}
      </button>
      <pre ref={preRef} {...rest}>
        {children}
      </pre>
    </div>
  );
}

function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedConversationId, setSelectedConversationId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [title, setTitle] = useState("New AI Workbench Conversation");
  const [question, setQuestion] = useState("");
  const [attachedImages, setAttachedImages] = useState<string[]>([]);
  const [attachedFiles, setAttachedFiles] = useState<FileAttachment[]>([]);
  const [mode, setMode] = useState<Mode>("auto");
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState("Ready");
  const [token, setToken] = useState(() => window.localStorage.getItem(TOKEN_STORAGE_KEY) ?? "");
  const [streamState, setStreamState] = useState<StreamState | null>(null);
  const [jwtEnabled, setJwtEnabled] = useState(false);
  const [registrationAllowed, setRegistrationAllowed] = useState(true);
  const [me, setMe] = useState<string | null>(null);
  const [loginUsername, setLoginUsername] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [usageOpen, setUsageOpen] = useState(false);
  const [compareOpen, setCompareOpen] = useState(false);
  const [regenChoice, setRegenChoice] = useState("");
  const [statusModels, setStatusModels] = useState<{
    router?: string;
    budget?: string;
    fast?: string;
    smart?: string;
    fallback?: string;
  }>({});

  const abortControllerRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const messagesContainerRef = useRef<HTMLDivElement | null>(null);
  const selectedIdRef = useRef<number | null>(selectedConversationId);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const importFileInputRef = useRef<HTMLInputElement | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const audioPlayerRef = useRef<HTMLAudioElement | null>(null);
  const [speakingMessageId, setSpeakingMessageId] = useState<number | null>(null);
  const [copiedMessageId, setCopiedMessageId] = useState<number | null>(null);
  const [deletingMessageId, setDeletingMessageId] = useState<number | null>(null);
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

  const streaming = streamState !== null;
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

  async function loadConversations(preferredConversationId?: number | null) {
    const res = await fetch(`${API_BASE}/v1/conversations`, {
      headers: requestHeaders(),
    });
    if (res.status === 401) {
      // A token that used to work is now rejected (expired/revoked) -> sign out
      // so the login form reappears instead of a stale "signed in" shell.
      if (token.trim()) {
        logout();
        setStatus("Session expired — please sign in again.");
      }
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
    const res = await fetch(`${API_BASE}/v1/conversations/${conversationId}/messages`, {
      headers: requestHeaders(),
    });
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
      const res = await fetch(
        `${API_BASE}/v1/conversations/${conversationId}/messages/${messageId}/action`,
        {
          method: "POST",
          headers: requestHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ confirm }),
        },
      );
      if (!res.ok) {
        setStatus("Failed to resolve the action.");
        return;
      }
      const data = (await res.json()) as { action_status: ActionStatus; detail?: string | null };
      setMessages((prev) =>
        prev.map((message) =>
          message.id === messageId ? { ...message, action_status: data.action_status } : message,
        ),
      );
      if (data.detail) {
        setStatus(data.detail);
      }
    } catch {
      setStatus("Failed to resolve the action.");
    }
  }

  async function createConversation() {
    setLoading(true);
    setStatus("Creating conversation...");

    try {
      const res = await fetch(`${API_BASE}/v1/conversations`, {
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
      setStatus(`Created conversation #${conversation.id}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }

  async function renameConversation() {
    if (!selectedConversation) {
      setStatus("Select a conversation first.");
      return;
    }

    const newTitle = window.prompt("Rename conversation:", selectedConversation.title);
    if (!newTitle || !newTitle.trim()) {
      return;
    }

    setLoading(true);
    setStatus("Renaming conversation...");

    try {
      const res = await fetch(`${API_BASE}/v1/conversations/${selectedConversation.id}`, {
        method: "PATCH",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ title: newTitle.trim() }),
      });

      if (!res.ok) throw new Error("Failed to rename conversation");

      await loadConversations(selectedConversation.id);
      setStatus("Conversation renamed.");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }

  async function duplicateConversation() {
    if (!selectedConversation) {
      setStatus("Select a conversation first.");
      return;
    }

    setLoading(true);
    setStatus("Duplicating conversation...");

    try {
      const res = await fetch(
        `${API_BASE}/v1/conversations/${selectedConversation.id}/duplicate`,
        { method: "POST", headers: requestHeaders() },
      );
      if (!res.ok) throw new Error(`Failed to duplicate conversation (${res.status})`);

      const conversation = (await res.json()) as Conversation;
      await loadConversations(conversation.id);
      await loadMessages(conversation.id);
      setStatus(`Duplicated as "${conversation.title}".`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Failed to duplicate conversation");
    } finally {
      setLoading(false);
    }
  }

  function exportConversation(format: "markdown" | "json") {
    if (!selectedConversation) {
      return;
    }
    const filenameBase =
      selectedConversation.title.trim().replace(/[^a-z0-9]+/gi, "_").replace(/^_+|_+$/g, "").toLowerCase() ||
      "conversation";

    let content: string;
    let mime: string;
    let extension: string;

    if (format === "json") {
      content = JSON.stringify({ conversation: selectedConversation, messages }, null, 2);
      mime = "application/json";
      extension = "json";
    } else {
      const lines: string[] = [`# ${selectedConversation.title}`, ""];
      for (const message of messages) {
        lines.push(`## ${message.role === "user" ? "User" : "Assistant"} — ${formatTimestamp(message.created_at)}`, "");
        lines.push(message.content, "");
        if (message.sources && message.sources.length > 0) {
          lines.push("**Sources:**");
          for (const source of message.sources) {
            lines.push(`- [${source.title || source.url}](${source.url})`);
          }
          lines.push("");
        }
        if (message.images && message.images.length > 0) {
          lines.push(`_${message.images.length} image(s) attached — omitted from this export._`, "");
        }
        if (message.files && message.files.length > 0) {
          lines.push(`**Attached files:** ${message.files.map((file) => file.filename).join(", ")}`, "");
        }
      }
      content = lines.join("\n");
      mime = "text/markdown";
      extension = "md";
    }

    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${filenameBase}.${extension}`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }

  async function importConversation(fileList: FileList | null) {
    const file = fileList?.[0];
    if (!file) {
      return;
    }

    setImporting(true);
    setStatus("Importing conversation...");
    try {
      const parsed = JSON.parse(await file.text()) as {
        conversation?: { title?: string };
        messages?: {
          role?: string;
          content?: string;
          mode_used?: string | null;
          notes?: string | null;
        }[];
      };
      if (!Array.isArray(parsed.messages) || parsed.messages.length === 0) {
        throw new Error("That file doesn't look like an exported conversation.");
      }

      const res = await fetch(`${API_BASE}/v1/conversations/import`, {
        method: "POST",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          title: parsed.conversation?.title,
          messages: parsed.messages.map((message) => ({
            role: message.role,
            content: message.content,
            mode_used: message.mode_used ?? null,
            notes: message.notes ?? null,
          })),
        }),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
        throw new Error(
          typeof body.detail === "string" ? body.detail : `Import failed (${res.status})`,
        );
      }

      const conversation = (await res.json()) as Conversation;
      await loadConversations(conversation.id);
      await loadMessages(conversation.id);
      setStatus(`Imported "${conversation.title}".`);
    } catch (error) {
      setStatus(
        error instanceof Error
          ? error.message
          : "Failed to import conversation — is it valid JSON?",
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
      const res = await fetch(`${API_BASE}/v1/conversations/${conversationId}/pin`, {
        method: "PUT",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ model }),
      });
      if (!res.ok) throw new Error(`Failed to pin model (${res.status})`);
      await loadConversations(conversationId);
      setStatus(model ? `Pinned this conversation to ${model}` : "Pin cleared.");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Failed to pin model");
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
      const res = await fetch(`${API_BASE}/v1/conversations/${conversationId}/system_prompt`, {
        method: "PUT",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ system_prompt: instructionsDraft }),
      });
      if (!res.ok) throw new Error(`Failed to save instructions (${res.status})`);
      await loadConversations(conversationId);
      setInstructionsOpen(false);
      setStatus(instructionsDraft.trim() ? "Instructions saved." : "Instructions cleared.");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Failed to save instructions");
    } finally {
      setInstructionsSaving(false);
    }
  }

  async function deleteConversation() {
    if (!selectedConversation) {
      setStatus("Select a conversation first.");
      return;
    }

    const confirmed = window.confirm(
      `Delete "${selectedConversation.title}"?\n\nThis will permanently delete its saved messages from the local database.`,
    );

    if (!confirmed) {
      return;
    }

    setLoading(true);
    setStatus("Deleting conversation...");

    try {
      const res = await fetch(`${API_BASE}/v1/conversations/${selectedConversation.id}`, {
        method: "DELETE",
        headers: requestHeaders(),
      });

      if (!res.ok) throw new Error("Failed to delete conversation");

      setMessages([]);
      setSelectedConversationId(null);
      const updatedConversations = await loadConversations(null);

      if (updatedConversations.length > 0) {
        await loadMessages(updatedConversations[0].id);
      }

      setStatus("Conversation deleted.");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }

  async function refreshAfterStream(conversationId: number) {
    // Fetch the now-persisted messages, but only replace the visible pane if the
    // user is still on this conversation — otherwise we'd clobber the pane they
    // switched to. Clear the streaming bubble in the same tick as the message
    // swap so React batches them into one render (no duplicated-pair flash).
    let fetched: Message[] | null = null;
    try {
      const res = await fetch(`${API_BASE}/v1/conversations/${conversationId}/messages`, {
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
    },
  ) {
    if (busy) {
      return;
    }
    if (!selectedConversationId) {
      setStatus("Create or select a conversation first.");
      return;
    }

    const conversationId = selectedConversationId;
    const controller = new AbortController();
    abortControllerRef.current = controller;

    setStatus(opts?.startStatus ?? "Asking...");
    setStreamState({
      conversationId,
      question: displayQuestion,
      answer: "",
      questionImages: opts?.questionImages && opts.questionImages.length > 0 ? opts.questionImages : null,
      questionFiles: opts?.questionFiles && opts.questionFiles.length > 0 ? opts.questionFiles : null,
    });

    let answer = "";

    try {
      const res = await fetch(url, {
        method: "POST",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (!res.ok) {
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
          setStatus(`Routing: ${String(payload.mode_used ?? "?")} via ${String(payload.model ?? "?")}`);
        } else if (frame.event === "delta") {
          answer += String(payload.text ?? "");
          setStreamState((prev) => (prev ? { ...prev, answer } : prev));
        } else if (frame.event === "done") {
          terminal = true;
          setStatus(`${String(payload.mode_used ?? "?")} | ${String(payload.notes ?? "")}`);
          const sources = Array.isArray(payload.sources) ? (payload.sources as Source[]) : null;
          const pendingAction =
            payload.pending_action && typeof payload.pending_action === "object"
              ? (payload.pending_action as PendingAction)
              : null;
          const images = Array.isArray(payload.images) ? (payload.images as string[]) : null;
          if ((sources && sources.length > 0) || pendingAction || (images && images.length > 0)) {
            setStreamState((prev) =>
              prev
                ? {
                    ...prev,
                    ...(sources && sources.length > 0 ? { sources } : {}),
                    ...(pendingAction ? { pending_action: pendingAction } : {}),
                    ...(images && images.length > 0 ? { images } : {}),
                  }
                : prev,
            );
          }
        } else if (frame.event === "error") {
          terminal = true;
          setStatus(`Error: ${String(payload.message ?? "stream failed")}`);
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
          setStatus("Stream ended unexpectedly.");
        }
      }

      await refreshAfterStream(conversationId);
    } catch (error) {
      const aborted = error instanceof DOMException && error.name === "AbortError";
      setStatus(aborted ? "Stopped." : error instanceof Error ? error.message : "Unknown error");
      if (answer === "") {
        opts?.onEmptyError?.();
      }
      await refreshAfterStream(conversationId);
    } finally {
      abortControllerRef.current = null;
      setStreamState(null);
    }
  }

  function isDocumentFile(file: File): boolean {
    if (ACCEPTED_FILE_MIMES.has(file.type)) {
      return true;
    }
    // Some browsers report an empty/nonstandard mime for .txt/.md; fall back
    // to the extension so those still work.
    const name = file.name.toLowerCase();
    return file.type === "" && (name.endsWith(".txt") || name.endsWith(".md"));
  }

  function readAsDataUrl(file: File): Promise<string | null> {
    return new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(typeof reader.result === "string" ? reader.result : null);
      reader.onerror = () => resolve(null);
      reader.readAsDataURL(file);
    });
  }

  async function handleFilesSelected(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) {
      return;
    }
    const files = Array.from(fileList);
    const imageFiles = files.filter((file) => file.type.startsWith("image/"));
    const documentFiles = files.filter(
      (file) => !file.type.startsWith("image/") && isDocumentFile(file),
    );

    const selectedImages = imageFiles.slice(0, Math.max(0, MAX_ATTACHED_IMAGES - attachedImages.length));
    const selectedDocuments = documentFiles.slice(
      0,
      Math.max(0, MAX_ATTACHED_FILES - attachedFiles.length),
    );

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

    const skipped =
      files.length -
      selectedImages.length -
      selectedDocuments.length +
      (selectedImages.length - validImages.length) +
      (selectedDocuments.length - validDocuments.length);
    if (skipped > 0) {
      setStatus(
        `Some files were skipped (images, PDFs, and plain text only — up to ${MAX_ATTACHED_IMAGES} images / ${MAX_ATTACHED_FILES} documents).`,
      );
    }

    setAttachedImages((prev) => [...prev, ...validImages]);
    setAttachedFiles((prev) => [...prev, ...validDocuments]);
  }

  function removeAttachedImage(index: number) {
    setAttachedImages((prev) => prev.filter((_, i) => i !== index));
  }

  function removeAttachedFile(index: number) {
    setAttachedFiles((prev) => prev.filter((_, i) => i !== index));
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
      setStatus("No audio was recorded.");
      return;
    }
    setTranscribing(true);
    setStatus("Transcribing...");
    try {
      const blob = new Blob(chunks, { type: mimeType });
      const base64 = await blobToBase64(blob);
      if (!base64) {
        setStatus("Failed to read the recording.");
        return;
      }
      // Strip codec parameters (e.g. "audio/webm;codecs=opus") — the backend
      // only recognises the bare mime, not the full MediaRecorder type string.
      const baseMime = mimeType.split(";")[0] || "audio/webm";

      const res = await fetch(`${API_BASE}/v1/transcribe`, {
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
        setStatus(detail);
        return;
      }

      const data = (await res.json()) as { text: string };
      if (data.text) {
        setQuestion((current) => (current ? `${current} ${data.text}` : data.text));
        setStatus("Ready");
      } else {
        setStatus("No speech was detected.");
      }
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Transcription failed.");
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
      setStatus("Voice input isn't supported in this browser.");
      return;
    }

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
      setStatus("Recording... click the mic again to stop.");
    } catch {
      setStatus("Microphone access was denied or unavailable.");
    }
  }

  async function copyMessage(message: Message) {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopiedMessageId(message.id);
      window.setTimeout(() => {
        setCopiedMessageId((current) => (current === message.id ? null : current));
      }, 1500);
    } catch {
      setStatus("Failed to copy to clipboard.");
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
      const res = await fetch(
        `${API_BASE}/v1/conversations/${conversationId}/messages/${message.id}`,
        { method: "DELETE", headers: requestHeaders() },
      );
      if (!res.ok) throw new Error(`Failed to delete message (${res.status})`);

      setMessages((prev) => prev.filter((candidate) => candidate.id !== message.id));
      setStatus("Message deleted.");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Failed to delete message");
    } finally {
      setDeletingMessageId(null);
    }
  }

  async function toggleSpeak(message: Message) {
    if (speakingMessageId === message.id) {
      audioPlayerRef.current?.pause();
      setSpeakingMessageId(null);
      return;
    }

    // Only one clip plays at a time; stop whatever's currently playing.
    audioPlayerRef.current?.pause();
    setSpeakingMessageId(null);

    setSynthesizingMessageId(message.id);
    try {
      const res = await fetch(`${API_BASE}/v1/speak`, {
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
        setStatus(detail);
        return;
      }

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
      setStatus(error instanceof Error ? error.message : "Speech synthesis failed.");
    } finally {
      setSynthesizingMessageId(null);
    }
  }

  async function askQuestion() {
    if (busy) {
      return;
    }
    if (!selectedConversationId) {
      setStatus("Create or select a conversation first.");
      return;
    }
    const cleanQuestion = question.trim();
    if (!cleanQuestion) {
      setStatus("Enter a question first.");
      return;
    }

    const cleanImages = attachedImages;
    const cleanFiles = attachedFiles;
    setQuestion("");
    setAttachedImages([]);
    setAttachedFiles([]);
    await streamInto(
      `${API_BASE}/v1/conversations/${selectedConversationId}/ask/stream`,
      {
        question: cleanQuestion,
        mode,
        ...(cleanImages.length > 0 ? { images: cleanImages } : {}),
        ...(cleanFiles.length > 0 ? { files: cleanFiles } : {}),
      },
      cleanQuestion,
      {
        startStatus: "Asking...",
        questionImages: cleanImages,
        questionFiles: cleanFiles,
        // Give the user their text/images/files back so a transient failure
        // stays retryable.
        onEmptyError: () => {
          setQuestion((current) => (current ? current : cleanQuestion));
          setAttachedImages((current) => (current.length > 0 ? current : cleanImages));
          setAttachedFiles((current) => (current.length > 0 ? current : cleanFiles));
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
      setStatus("Enter a question first.");
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
      setStatus("Nothing to regenerate yet.");
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
    abortControllerRef.current?.abort();
  }

  async function refreshStatus() {
    try {
      const res = await fetch(`${API_BASE}/v1/status`);
      if (res.ok) {
        const data = (await res.json()) as {
          jwt_enabled?: boolean;
          registration_allowed?: boolean;
          models?: { router?: string; budget?: string; fast?: string; smart?: string; fallback?: string };
        };
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

  async function refreshMe() {
    try {
      const res = await fetch(`${API_BASE}/v1/auth/me`, { headers: requestHeaders() });
      if (res.ok) {
        const data = (await res.json()) as { username?: string | null };
        setMe(data.username ?? null);
      } else {
        setMe(null);
      }
    } catch {
      setMe(null);
    }
  }

  async function submitAuth(register: boolean) {
    const username = loginUsername.trim();
    const password = loginPassword;
    if (!username || !password) {
      setStatus("Enter a username and password.");
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

      const data = (await res.json()) as { access_token: string };
      setToken(data.access_token);
      setMe(username);
      setLoginUsername("");
      setLoginPassword("");
      setStatus(`Signed in as ${username}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Authentication failed");
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
    setSelectedConversationId(null);
    setConversations([]);
    setMessages([]);
    setLoginUsername("");
    setLoginPassword("");
    setStatus("Signed out.");
  }

  useEffect(() => {
    if (token) {
      window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
    } else {
      window.localStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  }, [token]);

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
        const res = await fetch(`${API_BASE}/v1/search?q=${encodeURIComponent(query)}`, {
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

  function selectSearchResult(conversationId: number) {
    setSelectedConversationId(conversationId);
    setSearchQuery("");
    setSearchResults([]);
  }

  // Global keyboard shortcuts. Ctrl/Cmd+K jumps into search from anywhere.
  // Escape backs out of whatever's open, most-local first: the Instructions
  // panel, an in-progress edit, then an active search — skipped entirely
  // while Settings/Usage/Compare are open, since those modals own Escape
  // themselves.
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        if (settingsOpen || usageOpen || compareOpen) {
          return;
        }
        event.preventDefault();
        searchInputRef.current?.focus();
        return;
      }

      if (event.key === "Escape") {
        if (settingsOpen || usageOpen || compareOpen) {
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
  }, [settingsOpen, usageOpen, compareOpen, instructionsOpen, editingMessageId, searchQuery]);

  useEffect(() => {
    const load = async () => {
      await refreshStatus();
    };
    void load();
  }, []);

  // Reload the (per-user) conversation list and current identity whenever the
  // credential changes — login and logout both flow through here.
  useEffect(() => {
    const load = async () => {
      await refreshMe();
      try {
        await loadConversations();
      } catch (error) {
        setStatus(error instanceof Error ? error.message : "Backend not reachable");
      }
    };
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  useEffect(() => {
    // Guard against out-of-order responses: if the user switches conversations
    // again before this fetch resolves, discard the stale result.
    let cancelled = false;
    const load = async () => {
      if (!selectedConversationId) {
        if (!cancelled) {
          setMessages([]);
        }
        return;
      }
      try {
        const res = await fetch(`${API_BASE}/v1/conversations/${selectedConversationId}/messages`, {
          headers: requestHeaders(),
        });
        if (!res.ok) throw new Error("Failed to load messages");
        const data = (await res.json()) as Message[];
        if (!cancelled) {
          setMessages(data);
        }
      } catch (error) {
        if (!cancelled) {
          setStatus(error instanceof Error ? error.message : "Failed to load messages");
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedConversationId]);

  useEffect(() => {
    const container = messagesContainerRef.current;
    const anchor = messagesEndRef.current;
    if (!container || !anchor) {
      return;
    }
    // Only follow the tail when the user is already near the bottom, so reading
    // back through history mid-stream isn't yanked down on every delta.
    const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    if (distanceFromBottom < 120) {
      anchor.scrollIntoView({ block: "end" });
    }
  }, [messages, streamState]);

  const showStream = streamState !== null && streamState.conversationId === selectedConversationId;

  const conversationTokens = messages.reduce(
    (sum, message) => sum + (message.input_tokens ?? 0) + (message.output_tokens ?? 0),
    0,
  );
  const conversationCost = messages.reduce((sum, message) => sum + (message.cost_usd ?? 0), 0);

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
  const canRegenerate = messages.length > 0 && !showStream;

  // The conversation's model pin ("" = not pinned; "budget"/"fast"/"smart" = tier).
  const pinValue = selectedConversation?.pinned_model ?? "";
  const isPinned = Boolean(pinValue);
  const isTierPin = pinValue === "budget" || pinValue === "fast" || pinValue === "smart";
  // Always include the current pinned model as an option, even if it isn't one
  // of the configured tier models, so the selector reflects the real value.
  const pinModelOptions = Array.from(
    new Set(pinValue && !isTierPin ? [...forcedModelOptions, pinValue] : forcedModelOptions),
  );

  return (
    <main className="app-shell">
      <section className="sidebar">
        <div>
          <h1>AI Workbench</h1>
          <p className="subtitle">Free-first AI orchestration foundation</p>
        </div>

        <div className="create-box">
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Conversation title"
            aria-label="New conversation title"
          />
          <button onClick={createConversation} disabled={busy}>
            Create
          </button>
        </div>

        <div className="import-box">
          <input
            ref={importFileInputRef}
            type="file"
            accept="application/json"
            className="visually-hidden"
            aria-label="Import a conversation from a JSON file"
            onChange={(event) => {
              void importConversation(event.target.files);
              event.target.value = "";
            }}
          />
          <button
            type="button"
            className="secondary-button"
            onClick={() => importFileInputRef.current?.click()}
            disabled={importing}
          >
            {importing ? "Importing…" : "⬆️ Import conversation"}
          </button>
        </div>

        <div className="search-box">
          <input
            ref={searchInputRef}
            value={searchQuery}
            onChange={(event) => {
              const value = event.target.value;
              setSearchQuery(value);
              if (!value.trim()) {
                setSearchResults([]);
                setSearching(false);
              }
            }}
            placeholder={`Search conversations… (${IS_MAC ? "⌘K" : "Ctrl+K"})`}
            aria-label="Search conversations"
            type="search"
          />
        </div>

        {searchQuery.trim() ? (
          <div className="conversation-list search-results">
            {searching ? (
              <div className="empty-state small">Searching…</div>
            ) : searchResults.length === 0 ? (
              <div className="empty-state small">No matches.</div>
            ) : (
              searchResults.map((result) => (
                <button
                  key={result.id}
                  className={
                    result.id === selectedConversationId ? "conversation active" : "conversation"
                  }
                  onClick={() => selectSearchResult(result.id)}
                >
                  <span>{result.title}</span>
                  <small className="search-snippet">
                    {result.snippet.length > 140
                      ? `${result.snippet.slice(0, 140)}…`
                      : result.snippet}
                  </small>
                </button>
              ))
            )}
          </div>
        ) : (
          <div className="conversation-list">
            {conversations.map((conversation) => (
              <button
                key={conversation.id}
                className={conversation.id === selectedConversationId ? "conversation active" : "conversation"}
                onClick={() => setSelectedConversationId(conversation.id)}
              >
                <span>{conversation.title}</span>
                <small>#{conversation.id}</small>
              </button>
            ))}
          </div>
        )}

        <div className="sidebar-footer">
          {jwtEnabled ? (
            me ? (
              <div className="auth-signed-in">
                <span>
                  Signed in as <strong>{me}</strong>
                </span>
                <button className="secondary-button" onClick={logout}>
                  Log out
                </button>
              </div>
            ) : (
              <div className="auth-form">
                <label>Sign in</label>
                <input
                  value={loginUsername}
                  onChange={(event) => setLoginUsername(event.target.value)}
                  placeholder="username"
                  aria-label="Username"
                  autoComplete="username"
                />
                <input
                  type="password"
                  value={loginPassword}
                  onChange={(event) => setLoginPassword(event.target.value)}
                  placeholder="password"
                  aria-label="Password"
                  autoComplete="current-password"
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                      event.preventDefault();
                      void submitAuth(false);
                    }
                  }}
                />
                <div className="auth-buttons">
                  <button onClick={() => submitAuth(false)} disabled={authBusy}>
                    Log in
                  </button>
                  {registrationAllowed ? (
                    <button
                      className="secondary-button"
                      onClick={() => submitAuth(true)}
                      disabled={authBusy}
                    >
                      Register
                    </button>
                  ) : null}
                </div>
              </div>
            )
          ) : (
            <>
              <label htmlFor="api-token">API token (optional)</label>
              <input
                id="api-token"
                type="password"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder="Bearer token"
                autoComplete="off"
              />
            </>
          )}
        </div>
      </section>

      <section className="chat-panel">
        <header className="chat-header">
          <div>
            <h2>{selectedConversation ? selectedConversation.title : "No conversation selected"}</h2>
            <p aria-live="polite">{status}</p>
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

            <select
              value=""
              onChange={(event) => {
                const format = event.target.value;
                if (format === "markdown" || format === "json") {
                  exportConversation(format);
                }
                event.target.value = "";
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
            </select>

            <button
              className="secondary-button"
              onClick={openInstructions}
              disabled={!selectedConversation}
              title="Custom instructions (persona/style/rules) for this conversation"
            >
              Instructions{selectedConversation?.system_prompt ? " ●" : ""}
            </button>

            <button className="secondary-button" onClick={() => setCompareOpen(true)}>
              Compare
            </button>

            <button className="secondary-button" onClick={() => setUsageOpen(true)}>
              Usage
            </button>

            <button className="secondary-button" onClick={() => setSettingsOpen(true)}>
              Settings
            </button>

            <button className="secondary-button" onClick={renameConversation} disabled={busy || !selectedConversation}>
              Rename
            </button>

            <button
              className="secondary-button"
              onClick={() => void duplicateConversation()}
              disabled={busy || !selectedConversation}
            >
              Duplicate
            </button>

            <button className="danger-button" onClick={deleteConversation} disabled={busy || !selectedConversation}>
              Delete
            </button>
          </div>
        </header>

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

        <div className="messages" ref={messagesContainerRef}>
          {messages.length === 0 && !showStream ? (
            <div className="empty-state">Create or select a conversation, then ask a question.</div>
          ) : (
            messages.map((message) => (
              <article key={message.id} className={`message ${message.role}`}>
                <div className="message-meta">
                  <strong>{message.role}</strong>
                  {message.mode_used ? <span className="mode-badge">{message.mode_used}</span> : null}
                  {message.role === "assistant" && message.cached ? (
                    <span className="cached-badge">cached · free</span>
                  ) : null}
                  {message.role === "assistant" &&
                  !message.cached &&
                  (message.input_tokens != null || message.output_tokens != null) ? (
                    <span className="usage-badge">
                      {(message.input_tokens ?? 0) + (message.output_tokens ?? 0)} tok
                      {formatCost(message.cost_usd) ? ` · ${formatCost(message.cost_usd)}` : ""}
                    </span>
                  ) : null}
                  <span>{formatTimestamp(message.created_at)}</span>
                  <button
                    type="button"
                    className="secondary-button speak-button"
                    onClick={() => void copyMessage(message)}
                    title={copiedMessageId === message.id ? "Copied!" : "Copy message text"}
                    aria-label={copiedMessageId === message.id ? "Copied!" : "Copy message text"}
                  >
                    {copiedMessageId === message.id ? "✓" : "📋"}
                  </button>
                  {message.role === "assistant" ? (
                    <button
                      type="button"
                      className="secondary-button speak-button"
                      onClick={() => void toggleSpeak(message)}
                      disabled={synthesizingMessageId === message.id}
                      title={speakingMessageId === message.id ? "Stop speaking" : "Read this answer aloud"}
                      aria-label={speakingMessageId === message.id ? "Stop speaking" : "Read this answer aloud"}
                    >
                      {synthesizingMessageId === message.id
                        ? "…"
                        : speakingMessageId === message.id
                          ? "⏹"
                          : "🔊"}
                    </button>
                  ) : null}
                  {message.role === "user" && editingMessageId !== message.id ? (
                    <button
                      type="button"
                      className="secondary-button speak-button"
                      onClick={() => startEdit(message)}
                      disabled={busy}
                      title="Edit and resend this question"
                      aria-label={`Edit message from ${formatTimestamp(message.created_at)}`}
                    >
                      ✏️
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="secondary-button speak-button"
                    onClick={() => void deleteMessage(message)}
                    disabled={deletingMessageId === message.id}
                    title="Delete this message"
                    aria-label={`Delete ${message.role} message from ${formatTimestamp(message.created_at)}`}
                  >
                    🗑️
                  </button>
                </div>
                {message.role === "assistant" ? (
                  <div className="markdown-body">
                    <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ pre: CodeBlock }}>
                      {message.content}
                    </ReactMarkdown>
                  </div>
                ) : editingMessageId === message.id ? (
                  <div className="edit-message-form">
                    <textarea
                      value={editDraft}
                      onChange={(event) => setEditDraft(event.target.value)}
                      aria-label="Edit question"
                      onKeyDown={(event) => {
                        if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                          event.preventDefault();
                          void saveEdit(message);
                        } else if (event.key === "Escape") {
                          cancelEdit();
                        }
                      }}
                    />
                    <div className="edit-message-buttons">
                      <button type="button" onClick={() => void saveEdit(message)}>
                        Save &amp; resend
                      </button>
                      <button type="button" className="secondary-button" onClick={cancelEdit}>
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <p>{message.content}</p>
                )}
                {message.role === "user" && message.images && message.images.length > 0 ? (
                  <div className="message-images">
                    {message.images.map((src, index) => (
                      <img key={`${message.id}-attached-${index}`} src={src} alt="Attached" />
                    ))}
                  </div>
                ) : null}
                {message.role === "user" && message.files && message.files.length > 0 ? (
                  <ul className="message-files" aria-label="Attached files">
                    {message.files.map((file, index) => (
                      <li key={`${message.id}-file-${index}`}>
                        📄 {file.filename}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {message.role === "assistant" && message.sources && message.sources.length > 0 ? (
                  <ul className="message-sources" aria-label="Sources">
                    {message.sources.map((source, index) => (
                      <li key={`${message.id}-source-${index}`}>
                        <a href={source.url} target="_blank" rel="noopener noreferrer">
                          {source.title || source.url}
                        </a>
                      </li>
                    ))}
                  </ul>
                ) : null}
                {message.role === "assistant" && message.images && message.images.length > 0 ? (
                  <div className="message-images">
                    {message.images.map((src, index) => (
                      <img key={`${message.id}-image-${index}`} src={src} alt="Generated" />
                    ))}
                  </div>
                ) : null}
                {message.role === "assistant" && message.pending_action ? (
                  <div className="pending-action" data-status={message.action_status ?? "pending"}>
                    <p className="pending-action-summary">{message.pending_action.summary}</p>
                    {message.action_status === "pending" || !message.action_status ? (
                      <div className="pending-action-buttons">
                        <button
                          className="primary-button"
                          onClick={() => resolveAction(message.conversation_id, message.id, true)}
                        >
                          Confirm
                        </button>
                        <button
                          className="secondary-button"
                          onClick={() => resolveAction(message.conversation_id, message.id, false)}
                        >
                          Decline
                        </button>
                      </div>
                    ) : (
                      <span className="pending-action-status">
                        {message.action_status === "confirmed"
                          ? "✓ Confirmed"
                          : message.action_status === "declined"
                            ? "Declined"
                            : "Failed"}
                      </span>
                    )}
                  </div>
                ) : null}
                {message.notes ? (
                  <details className="message-notes">
                    <summary>details</summary>
                    <small>{message.notes}</small>
                  </details>
                ) : null}
              </article>
            ))
          )}

          {showStream && streamState ? (
            <>
              <article className="message user">
                <div className="message-meta">
                  <strong>user</strong>
                  <span>sending...</span>
                </div>
                <p>{streamState.question}</p>
                {streamState.questionImages && streamState.questionImages.length > 0 ? (
                  <div className="message-images">
                    {streamState.questionImages.map((src, index) => (
                      <img key={`stream-attached-${index}`} src={src} alt="Attached" />
                    ))}
                  </div>
                ) : null}
                {streamState.questionFiles && streamState.questionFiles.length > 0 ? (
                  <ul className="message-files" aria-label="Attached files">
                    {streamState.questionFiles.map((file, index) => (
                      <li key={`stream-file-${index}`}>📄 {file.filename}</li>
                    ))}
                  </ul>
                ) : null}
              </article>
              <article className="message assistant">
                <div className="message-meta">
                  <strong>assistant</strong>
                  <span>streaming...</span>
                </div>
                <p className="streaming-text">
                  {streamState.answer}
                  <span className="streaming-cursor" aria-hidden="true">
                    ▍
                  </span>
                </p>
                {streamState.sources && streamState.sources.length > 0 ? (
                  <ul className="message-sources" aria-label="Sources">
                    {streamState.sources.map((source, index) => (
                      <li key={`stream-source-${index}`}>
                        <a href={source.url} target="_blank" rel="noopener noreferrer">
                          {source.title || source.url}
                        </a>
                      </li>
                    ))}
                  </ul>
                ) : null}
                {streamState.pending_action ? (
                  <div className="pending-action" data-status="pending">
                    <p className="pending-action-summary">{streamState.pending_action.summary}</p>
                    <span className="pending-action-status">Confirm below once sent</span>
                  </div>
                ) : null}
                {streamState.images && streamState.images.length > 0 ? (
                  <div className="message-images">
                    {streamState.images.map((src, index) => (
                      <img key={`stream-image-${index}`} src={src} alt="Generated" />
                    ))}
                  </div>
                ) : null}
              </article>
            </>
          ) : null}

          {canRegenerate ? (
            <div className="regenerate-bar">
              <button className="secondary-button" onClick={regenerate} disabled={busy}>
                ↻ Regenerate
              </button>
              <select
                value={regenChoice}
                onChange={(event) => setRegenChoice(event.target.value)}
                aria-label="Regenerate with"
                disabled={isPinned}
                title={isPinned ? "This conversation is pinned; clear the pin to regenerate with a different model." : undefined}
              >
                <option value="">re-route (auto)</option>
                {budgetTierEnabled ? <option value="mode:budget">budget tier</option> : null}
                <option value="mode:fast">fast tier</option>
                <option value="mode:smart">smart tier</option>
                {forcedModelOptions.length > 0 ? (
                  <optgroup label="force model">
                    {forcedModelOptions.map((model) => (
                      <option key={model} value={`model:${model}`}>
                        {model}
                      </option>
                    ))}
                  </optgroup>
                ) : null}
              </select>
            </div>
          ) : null}

          <div ref={messagesEndRef} className="messages-end" />
        </div>

        {attachedImages.length > 0 || attachedFiles.length > 0 ? (
          <div className="attached-images-preview">
            {attachedImages.map((src, index) => (
              <div className="attached-image-thumb" key={`attached-${index}`}>
                <img src={src} alt={`Attachment ${index + 1}`} />
                <button
                  type="button"
                  className="remove-attached-image"
                  aria-label={`Remove attachment ${index + 1}`}
                  onClick={() => removeAttachedImage(index)}
                >
                  ×
                </button>
              </div>
            ))}
            {attachedFiles.map((file, index) => (
              <div className="attached-file-chip" key={`attached-file-${index}`}>
                <span>📄 {file.filename}</span>
                <button
                  type="button"
                  className="remove-attached-image"
                  aria-label={`Remove attachment ${file.filename}`}
                  onClick={() => removeAttachedFile(index)}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        ) : null}

        <div className="composer">
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*,application/pdf,text/plain,.txt,.md"
            multiple
            className="visually-hidden"
            aria-label="Attach image or document"
            onChange={(event) => {
              void handleFilesSelected(event.target.files);
              event.target.value = "";
            }}
          />
          <button
            type="button"
            className="secondary-button attach-button"
            onClick={() => fileInputRef.current?.click()}
            disabled={
              attachedImages.length >= MAX_ATTACHED_IMAGES &&
              attachedFiles.length >= MAX_ATTACHED_FILES
            }
            title="Attach an image or document (PDF/plain text)"
            aria-label="Attach an image or document"
          >
            📎
          </button>
          <button
            type="button"
            className={`secondary-button mic-button${recording ? " recording" : ""}`}
            onClick={() => void toggleRecording()}
            disabled={transcribing}
            title={recording ? "Stop recording" : "Record a voice question"}
            aria-label={recording ? "Stop recording" : "Record a voice question"}
          >
            {recording ? "⏹" : "🎤"}
          </button>
          <textarea
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            aria-label="Ask a question"
            placeholder="Ask inside this saved conversation... (Enter to send, Shift+Enter for a new line, Ctrl+Enter also sends)"
            onKeyDown={(event) => {
              // Ignore Enter while an IME composition is in progress, otherwise
              // confirming a CJK candidate would submit the half-typed message.
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                void askQuestion();
              }
            }}
          />
          {streaming ? (
            <button className="stop-button" onClick={stopStreaming}>
              Stop
            </button>
          ) : (
            <button onClick={askQuestion} disabled={loading}>
              {loading ? "Working..." : "Ask"}
            </button>
          )}
        </div>
      </section>

      {settingsOpen ? (
        <Settings
          apiBase={API_BASE}
          getHeaders={requestHeaders}
          onClose={() => setSettingsOpen(false)}
          onChanged={() => {
            void refreshStatus();
          }}
        />
      ) : null}

      {usageOpen ? (
        <Usage apiBase={API_BASE} getHeaders={requestHeaders} onClose={() => setUsageOpen(false)} />
      ) : null}

      {compareOpen ? (
        <Compare
          apiBase={API_BASE}
          getHeaders={requestHeaders}
          availableModels={forcedModelOptions}
          onClose={() => setCompareOpen(false)}
        />
      ) : null}
    </main>
  );
}

export default App;
