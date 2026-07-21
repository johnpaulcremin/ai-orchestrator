import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { extractSseFrames, type SseFrame } from "./sse";
import { formatTimestamp, formatCost } from "./format";
import { Settings } from "./Settings";
import "./App.css";

type Mode = "auto" | "budget" | "fast" | "smart";

type Conversation = {
  id: number;
  title: string;
  owner?: string | null;
  pinned_model?: string | null;
  created_at: string;
  updated_at: string;
};

type Source = {
  title: string;
  url: string;
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
  created_at: string;
};

type StreamState = {
  conversationId: number;
  question: string;
  answer: string;
};

const API_BASE = "/api";
const TOKEN_STORAGE_KEY = "ai_workbench_token";

function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedConversationId, setSelectedConversationId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [title, setTitle] = useState("New AI Workbench Conversation");
  const [question, setQuestion] = useState("");
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
    opts?: { startStatus?: string; onEmptyError?: () => void },
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
    setStreamState({ conversationId, question: displayQuestion, answer: "" });

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

    setQuestion("");
    await streamInto(
      `${API_BASE}/v1/conversations/${selectedConversationId}/ask/stream`,
      { question: cleanQuestion, mode },
      cleanQuestion,
      {
        startStatus: "Asking...",
        // Give the user their text back so a transient failure stays retryable.
        onEmptyError: () => setQuestion((current) => (current ? current : cleanQuestion)),
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

            <button className="secondary-button" onClick={() => setSettingsOpen(true)}>
              Settings
            </button>

            <button className="secondary-button" onClick={renameConversation} disabled={busy || !selectedConversation}>
              Rename
            </button>

            <button className="danger-button" onClick={deleteConversation} disabled={busy || !selectedConversation}>
              Delete
            </button>
          </div>
        </header>

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
                </div>
                {message.role === "assistant" ? (
                  <div className="markdown-body">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
                  </div>
                ) : (
                  <p>{message.content}</p>
                )}
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

        <div className="composer">
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
    </main>
  );
}

export default App;
