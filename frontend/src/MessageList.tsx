import {
  type ComponentPropsWithoutRef,
  type Dispatch,
  type RefObject,
  type SetStateAction,
  useEffect,
  useRef,
  useState,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Bookmark,
  BookmarkCheck,
  Check,
  Copy,
  FileDown,
  GitBranch,
  Link2,
  Pencil,
  ThumbsDown,
  ThumbsUp,
  Trash2,
  Volume2,
  VolumeX,
} from "lucide-react";
import { Button } from "./Button";
import { formatTimestamp, formatCost } from "./format";
import type { Conversation, Message, StreamState } from "./types";

function formatAudioDuration(seconds?: number | null): string | null {
  if (seconds == null || !Number.isFinite(seconds)) {
    return null;
  }
  const total = Math.round(seconds);
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

const SUMMARIZE_TRANSCRIPT_PROMPT =
  "Summarize the meeting transcript above, with clear action items and owners if mentioned.";

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

type Props = {
  messages: Message[];
  streaming: boolean;
  streamState: StreamState | null;
  conversations: Conversation[];
  selectedConversation: Conversation | null;
  findMatchIds: number[];
  findActiveIndex: number;
  deepLinkHighlightId: number | null;
  copiedMessageId: number | null;
  copyMessage: (message: Message) => Promise<void>;
  copiedLinkMessageId: number | null;
  copyMessageLink: (message: Message) => Promise<void>;
  toggleMessageBookmark: (message: Message) => Promise<void>;
  rateMessage: (
    message: Message,
    verdict: "up" | "down" | null,
    reason?: string,
  ) => Promise<void>;
  synthesizingMessageId: number | null;
  speakingMessageId: number | null;
  toggleSpeak: (message: Message) => Promise<void>;
  freeSpeakingMessageId: number | null;
  toggleFreeSpeak: (message: Message) => void;
  editingMessageId: number | null;
  startEdit: (message: Message) => void;
  busy: boolean;
  branchingMessageId: number | null;
  branchFromMessage: (message: Message) => Promise<void>;
  deletingMessageId: number | null;
  deleteMessage: (message: Message) => Promise<void>;
  continuingMessageId: number | null;
  continueMessage: (message: Message) => Promise<void>;
  editDraft: string;
  setEditDraft: Dispatch<SetStateAction<string>>;
  saveEdit: (message: Message) => Promise<void>;
  cancelEdit: () => void;
  resolveAction: (conversationId: number, messageId: number, confirm: boolean) => Promise<void>;
  unansweredNotice: { conversationId: number; note: string } | null;
  selectedConversationId: number | null;
  canRegenerate: boolean;
  regenerate: () => Promise<void>;
  isPinned: boolean;
  regenChoice: string;
  setRegenChoice: Dispatch<SetStateAction<string>>;
  budgetTierEnabled: boolean;
  forcedModelOptions: string[];
  messagesEndRef: RefObject<HTMLDivElement | null>;
  messagesContainerRef: RefObject<HTMLDivElement | null>;
  showJumpToBottom: boolean;
  insertIntoComposer: (text: string) => void;
};

export function MessageList({
  messages,
  streaming,
  streamState,
  conversations,
  selectedConversation,
  findMatchIds,
  findActiveIndex,
  deepLinkHighlightId,
  copiedMessageId,
  copyMessage,
  copiedLinkMessageId,
  copyMessageLink,
  toggleMessageBookmark,
  rateMessage,
  synthesizingMessageId,
  speakingMessageId,
  toggleSpeak,
  freeSpeakingMessageId,
  toggleFreeSpeak,
  editingMessageId,
  startEdit,
  busy,
  branchingMessageId,
  branchFromMessage,
  deletingMessageId,
  deleteMessage,
  continuingMessageId,
  continueMessage,
  editDraft,
  setEditDraft,
  saveEdit,
  cancelEdit,
  resolveAction,
  unansweredNotice,
  selectedConversationId,
  canRegenerate,
  regenerate,
  isPinned,
  regenChoice,
  setRegenChoice,
  budgetTierEnabled,
  forcedModelOptions,
  messagesEndRef,
  messagesContainerRef,
  showJumpToBottom,
  insertIntoComposer,
}: Props) {
  // Which engine the merged per-message speak button uses -- a pure UI
  // preference shared across every message (mirrors Composer.tsx's mic
  // engine choice), not lifted to App.tsx since nothing outside this
  // component needs it.
  const [speakEngine, setSpeakEngine] = useState<"paid" | "free">("paid");

  // Which message's 👎 reason popover is currently open, if any -- a click
  // on 👎 for a message not yet rated down opens this instead of rating
  // immediately, so the optional reason has somewhere to go without a
  // modal. Escape/click-away below still records the 👎 with no reason.
  const [reasonPopoverFor, setReasonPopoverFor] = useState<number | null>(null);

  function rateUp(message: Message) {
    setReasonPopoverFor(null);
    void rateMessage(message, "up");
  }

  function rateDown(message: Message) {
    // Already down -> this click clears it (same click-again-to-clear
    // contract the backend enforces); no need for the reason popover then.
    if (message.feedback === -1) {
      void rateMessage(message, "down");
      return;
    }
    setReasonPopoverFor(message.id);
  }

  function submitDownWithReason(message: Message, reason?: string) {
    setReasonPopoverFor(null);
    void rateMessage(message, "down", reason);
  }

  useEffect(() => {
    if (reasonPopoverFor === null) {
      return;
    }
    const message = messages.find((candidate) => candidate.id === reasonPopoverFor);
    if (!message) {
      return;
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        submitDownWithReason(message!);
      }
    }
    function handleClickAway(event: MouseEvent) {
      const target = event.target as HTMLElement;
      if (!target.closest(".feedback-control")) {
        submitDownWithReason(message!);
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("mousedown", handleClickAway);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("mousedown", handleClickAway);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reasonPopoverFor]);

  function toggleMessageSpeak(message: Message) {
    if (speakingMessageId === message.id || freeSpeakingMessageId === message.id) {
      if (speakingMessageId === message.id) void toggleSpeak(message);
      if (freeSpeakingMessageId === message.id) toggleFreeSpeak(message);
      return;
    }
    if (speakEngine === "paid") {
      void toggleSpeak(message);
    } else {
      toggleFreeSpeak(message);
    }
  }

  return (
    <>
      <div className="messages" ref={messagesContainerRef}>
        {messages.length === 0 && !streaming ? (
          conversations.length === 0 && !selectedConversation ? (
            <div className="empty-state onboarding-hint">
              <p>
                <strong>Welcome to AI Workbench.</strong> You don't have any conversations yet — here's how to
                get started:
              </p>
              <ul>
                <li>
                  Type a title in the box above the sidebar and click <strong>Create</strong> to start your
                  first conversation.
                </li>
                <li>Once it's selected, ask anything below — routing picks a suitable model automatically.</li>
                <li>
                  Add your provider API keys via <strong>Settings</strong> in the header if you haven't
                  already.
                </li>
              </ul>
            </div>
          ) : (
            <div className="empty-state">Create or select a conversation, then ask a question.</div>
          )
        ) : (
          messages.map((message) => (
            <article
              key={message.id}
              data-message-id={message.id}
              className={`message ${message.role}${
                findMatchIds.length > 0 && message.id === findMatchIds[findActiveIndex % findMatchIds.length]
                  ? " find-active"
                  : ""
              }${message.id === deepLinkHighlightId ? " deep-link-target" : ""}`}
            >
              <div className="message-meta">
                <strong>{message.role}</strong>
                {message.mode_used ? <span className="mode-badge">{message.mode_used}</span> : null}
                {message.role === "assistant" && message.cached ? (
                  <span className="cached-badge">cached · free</span>
                ) : null}
                {message.role === "assistant" && message.mode_used?.startsWith("auto->free:") ? (
                  <span className="cached-badge">
                    served free via {message.mode_used.slice("auto->free:".length)}
                  </span>
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
                <div className="message-actions">
                  <Button
                    iconOnly
                    size="sm"
                    variant="ghost"
                    onClick={() => void copyMessage(message)}
                    title={copiedMessageId === message.id ? "Copied!" : "Copy message text"}
                    aria-label={copiedMessageId === message.id ? "Copied!" : "Copy message text"}
                    icon={copiedMessageId === message.id ? <Check size={16} /> : <Copy size={16} />}
                  />
                  <Button
                    iconOnly
                    size="sm"
                    variant="ghost"
                    onClick={() => void copyMessageLink(message)}
                    title={copiedLinkMessageId === message.id ? "Link copied!" : "Copy link to this message"}
                    aria-label={
                      copiedLinkMessageId === message.id ? "Link copied!" : "Copy link to this message"
                    }
                    icon={copiedLinkMessageId === message.id ? <Check size={16} /> : <Link2 size={16} />}
                  />
                  <Button
                    iconOnly
                    size="sm"
                    variant="ghost"
                    className={`bookmark-button${message.bookmarked ? " active" : ""}`}
                    onClick={() => void toggleMessageBookmark(message)}
                    title={message.bookmarked ? "Remove bookmark" : "Bookmark this message"}
                    aria-label={
                      message.bookmarked
                        ? `Remove bookmark from ${message.role} message from ${formatTimestamp(message.created_at)}`
                        : `Bookmark ${message.role} message from ${formatTimestamp(message.created_at)}`
                    }
                    aria-pressed={Boolean(message.bookmarked)}
                    icon={
                      message.bookmarked ? <BookmarkCheck size={16} /> : <Bookmark size={16} />
                    }
                  />
                  {message.role === "assistant" ? (
                    <div className="feedback-control">
                      <Button
                        iconOnly
                        size="sm"
                        variant="ghost"
                        className={`feedback-button feedback-up${message.feedback === 1 ? " active" : ""}`}
                        onClick={() => rateUp(message)}
                        title={message.feedback === 1 ? "Remove rating" : "Good answer"}
                        aria-label={
                          message.feedback === 1
                            ? "Remove rating from this answer"
                            : "Rate this answer good"
                        }
                        aria-pressed={message.feedback === 1}
                        icon={<ThumbsUp size={16} />}
                      />
                      <Button
                        iconOnly
                        size="sm"
                        variant="ghost"
                        className={`feedback-button feedback-down${message.feedback === -1 ? " active" : ""}`}
                        onClick={() => rateDown(message)}
                        title={message.feedback === -1 ? "Remove rating" : "Bad answer"}
                        aria-label={
                          message.feedback === -1
                            ? "Remove rating from this answer"
                            : "Rate this answer bad"
                        }
                        aria-pressed={message.feedback === -1}
                        icon={<ThumbsDown size={16} />}
                      />
                      {reasonPopoverFor === message.id ? (
                        <div
                          className="feedback-reason-popover"
                          role="menu"
                          aria-label="Why was this answer bad? (optional)"
                        >
                          {["Wrong", "Incomplete", "Style/format", "Other"].map((reason) => (
                            <button
                              key={reason}
                              type="button"
                              role="menuitem"
                              className="feedback-reason-option"
                              onClick={() => submitDownWithReason(message, reason)}
                            >
                              {reason}
                            </button>
                          ))}
                          <button
                            type="button"
                            className="feedback-reason-skip"
                            onClick={() => submitDownWithReason(message)}
                          >
                            Skip
                          </button>
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                  {message.role === "assistant" ? (
                    <div className="speak-control">
                      <Button
                        iconOnly
                        size="sm"
                        variant="ghost"
                        onClick={() => toggleMessageSpeak(message)}
                        disabled={synthesizingMessageId === message.id}
                        title={
                          synthesizingMessageId === message.id
                            ? "Preparing speech…"
                            : speakingMessageId === message.id || freeSpeakingMessageId === message.id
                              ? "Stop speaking"
                              : speakEngine === "paid"
                                ? "Read this answer aloud — AI voice, uses paid API tokens/credits"
                                : "Read this answer aloud — your browser's built-in voice, on-device, lower quality"
                        }
                        aria-label={
                          speakingMessageId === message.id || freeSpeakingMessageId === message.id
                            ? "Stop speaking"
                            : "Read this answer aloud"
                        }
                        icon={
                          speakingMessageId === message.id || freeSpeakingMessageId === message.id ? (
                            <VolumeX size={16} />
                          ) : (
                            <Volume2 size={16} />
                          )
                        }
                      />
                      <select
                        className="speak-engine-select"
                        aria-label="Voice output engine"
                        value={speakEngine}
                        disabled={speakingMessageId === message.id || freeSpeakingMessageId === message.id}
                        onChange={(event) => setSpeakEngine(event.target.value as "paid" | "free")}
                        title="Choose the voice-output engine"
                      >
                        <option value="paid">$ AI</option>
                        <option value="free">Free</option>
                      </select>
                    </div>
                  ) : null}
                  {message.role === "user" && editingMessageId !== message.id ? (
                    <Button
                      iconOnly
                      size="sm"
                      variant="ghost"
                      onClick={() => startEdit(message)}
                      disabled={busy}
                      title="Edit and resend this question"
                      aria-label={`Edit message from ${formatTimestamp(message.created_at)}`}
                      icon={<Pencil size={16} />}
                    />
                  ) : null}
                  <Button
                    iconOnly
                    size="sm"
                    variant="ghost"
                    onClick={() => void branchFromMessage(message)}
                    disabled={branchingMessageId === message.id}
                    title="Branch a new conversation from this point"
                    aria-label={`Branch a new conversation from the ${message.role} message from ${formatTimestamp(message.created_at)}`}
                    icon={<GitBranch size={16} />}
                  />
                  <Button
                    iconOnly
                    size="sm"
                    variant="ghost"
                    onClick={() => void deleteMessage(message)}
                    disabled={deletingMessageId === message.id}
                    title="Delete this message"
                    aria-label={`Delete ${message.role} message from ${formatTimestamp(message.created_at)}`}
                    icon={<Trash2 size={16} />}
                  />
                </div>
              </div>
              {message.role === "assistant" ? (
                <div className="markdown-body">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ pre: CodeBlock }}>
                    {message.content}
                  </ReactMarkdown>
                </div>
              ) : null}
              {message.role === "assistant" && message.truncated ? (
                <div className="truncated-notice" role="status">
                  <span>⚠️ Response was cut off before it finished.</span>
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => void continueMessage(message)}
                    disabled={continuingMessageId === message.id}
                    title="Uses paid API tokens/credits"
                  >
                    {continuingMessageId === message.id ? "Continuing…" : "$ Continue"}
                  </button>
                </div>
              ) : null}
              {message.role !== "assistant" ? (
                editingMessageId === message.id ? (
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
                )
              ) : null}
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
              {message.role === "user" && message.audio && message.audio.length > 0 ? (
                <div className="message-audio-attachments">
                  <ul className="message-files" aria-label="Attached audio">
                    {message.audio.map((clip, index) => (
                      <li key={`${message.id}-audio-${index}`}>
                        🎙️ {clip.filename}
                        {formatAudioDuration(clip.duration_seconds)
                          ? ` (${formatAudioDuration(clip.duration_seconds)})`
                          : ""}
                      </li>
                    ))}
                  </ul>
                  <button
                    type="button"
                    className="summarize-transcript-suggestion"
                    onClick={() => insertIntoComposer(SUMMARIZE_TRANSCRIPT_PROMPT)}
                  >
                    📝 Summarize with action items
                  </button>
                </div>
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
              {message.role === "assistant" &&
              message.code_results &&
              message.code_results.length > 0 ? (
                <div className="code-results">
                  {message.code_results.map((result, index) => (
                    <details key={`${message.id}-code-${index}`} className="code-result">
                      <summary>Ran code</summary>
                      <CodeBlock>
                        <code>{result.code}</code>
                      </CodeBlock>
                      {result.logs ? <pre className="code-result-logs">{result.logs}</pre> : null}
                      {result.images && result.images.length > 0 ? (
                        <div className="code-result-images">
                          {result.images.map((src, imageIndex) => (
                            <img
                              key={`${message.id}-code-${index}-image-${imageIndex}`}
                              src={src}
                              alt="Code output"
                            />
                          ))}
                        </div>
                      ) : null}
                      {result.files && result.files.length > 0 ? (
                        <ul className="code-result-files" aria-label="Generated files">
                          {result.files.map((file, fileIndex) => (
                            <li key={`${message.id}-code-${index}-file-${fileIndex}`}>
                              <a
                                href={file.data}
                                download={file.filename}
                                className="code-result-file-link"
                              >
                                <FileDown size={16} aria-hidden="true" /> {file.filename}
                              </a>
                            </li>
                          ))}
                        </ul>
                      ) : null}
                      {result.file_warnings && result.file_warnings.length > 0 ? (
                        <ul className="code-result-warnings" aria-label="Files that could not be attached">
                          {result.file_warnings.map((warning, warningIndex) => (
                            <li key={`${message.id}-code-${index}-warning-${warningIndex}`}>
                              ⚠️ {warning}
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </details>
                  ))}
                </div>
              ) : null}
              {message.role === "assistant" &&
              message.fact_checks &&
              message.fact_checks.length > 0 ? (
                <ul className="fact-checks" aria-label="Fact checks">
                  {message.fact_checks.map((result, index) => (
                    <li key={`${message.id}-fact-${index}`} className="fact-check">
                      {result.rating ? (
                        <span className="fact-check-rating" aria-label={`Rating: ${result.rating}`}>
                          {result.rating}
                        </span>
                      ) : null}
                      <span className="fact-check-claim">{result.claim}</span>
                      {result.url ? (
                        <a
                          className="fact-check-source"
                          href={result.url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {result.publisher || result.url}
                        </a>
                      ) : result.publisher ? (
                        <span className="fact-check-source">{result.publisher}</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
              {message.role === "assistant" &&
              message.academic_results &&
              message.academic_results.length > 0 ? (
                <ul className="academic-results" aria-label="Academic search results">
                  {message.academic_results.map((result, index) => (
                    <li key={`${message.id}-academic-${index}`} className="academic-result">
                      <span className="academic-result-title">{result.title}</span>
                      {result.authors || result.year ? (
                        <span className="academic-result-meta">
                          {[result.authors, result.year].filter(Boolean).join(" · ")}
                        </span>
                      ) : null}
                      {result.url ? (
                        <a
                          className="academic-result-source"
                          href={result.url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {result.venue || result.url}
                        </a>
                      ) : result.venue ? (
                        <span className="academic-result-source">{result.venue}</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
              {message.role === "assistant" &&
              message.math_results &&
              message.math_results.length > 0 ? (
                <ul className="math-results" aria-label="Computed results">
                  {message.math_results.map((result, index) => (
                    <li key={`${message.id}-math-${index}`} className="math-result">
                      <code className="math-result-expression">{result.expression}</code>
                      {result.result ? (
                        <span className="math-result-value">
                          = {result.result}
                          {result.source === "wolfram_alpha" ? (
                            <span className="math-result-source"> (via Wolfram Alpha)</span>
                          ) : null}
                        </span>
                      ) : (
                        <span className="math-result-error" aria-label={`Error: ${result.error}`}>
                          {result.error}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              ) : null}
              {message.role === "assistant" &&
              message.library_sources &&
              message.library_sources.length > 0 ? (
                <p className="library-sources-note">
                  📚 used your library:{" "}
                  {message.library_sources.map((source) => source.document).join(", ")}
                </p>
              ) : null}
              {message.role === "assistant" &&
              message.workflow_steps &&
              message.workflow_steps.length > 0 ? (
                <details className="workflow-steps">
                  <summary>Workflow: {message.workflow_steps.length} step(s)</summary>
                  <ol className="workflow-step-list">
                    {message.workflow_steps.map((step, index) => (
                      <li
                        key={`${message.id}-workflow-${index}`}
                        className="workflow-step"
                        data-status={step.status}
                      >
                        <span className="workflow-step-category">{step.category}</span>
                        <p className="workflow-step-instruction">{step.instruction}</p>
                        <p className="workflow-step-meta">
                          {step.model || "?"}
                          {step.status === "failed" ? " · failed" : ""}
                          {formatCost(step.cost_usd) ? ` · ~${formatCost(step.cost_usd)}` : ""}
                        </p>
                      </li>
                    ))}
                  </ol>
                </details>
              ) : null}
              {message.role === "assistant" && message.pending_action ? (
                <div className="pending-action" data-status={message.action_status ?? "pending"}>
                  <p className="pending-action-summary">{message.pending_action.summary}</p>
                  <pre className="pending-action-payload">
                    {JSON.stringify(message.pending_action.payload, null, 2)}
                  </pre>
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

        {streaming && streamState ? (
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
              {streamState.questionAudio && streamState.questionAudio.length > 0 ? (
                <ul className="message-files" aria-label="Attached audio">
                  {streamState.questionAudio.map((clip, index) => (
                    <li key={`stream-audio-${index}`}>
                      🎙️ {clip.filename}
                      {formatAudioDuration(clip.duration_seconds)
                        ? ` (${formatAudioDuration(clip.duration_seconds)})`
                        : ""}
                    </li>
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
                  <pre className="pending-action-payload">
                    {JSON.stringify(streamState.pending_action.payload, null, 2)}
                  </pre>
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
              {streamState.code_results && streamState.code_results.length > 0 ? (
                <div className="code-results">
                  {streamState.code_results.map((result, index) => (
                    <details key={`stream-code-${index}`} className="code-result">
                      <summary>Ran code</summary>
                      <CodeBlock>
                        <code>{result.code}</code>
                      </CodeBlock>
                      {result.logs ? <pre className="code-result-logs">{result.logs}</pre> : null}
                      {result.images && result.images.length > 0 ? (
                        <div className="code-result-images">
                          {result.images.map((src, imageIndex) => (
                            <img
                              key={`stream-code-${index}-image-${imageIndex}`}
                              src={src}
                              alt="Code output"
                            />
                          ))}
                        </div>
                      ) : null}
                      {result.files && result.files.length > 0 ? (
                        <ul className="code-result-files" aria-label="Generated files">
                          {result.files.map((file, fileIndex) => (
                            <li key={`stream-code-${index}-file-${fileIndex}`}>
                              <a
                                href={file.data}
                                download={file.filename}
                                className="code-result-file-link"
                              >
                                <FileDown size={16} aria-hidden="true" /> {file.filename}
                              </a>
                            </li>
                          ))}
                        </ul>
                      ) : null}
                      {result.file_warnings && result.file_warnings.length > 0 ? (
                        <ul className="code-result-warnings" aria-label="Files that could not be attached">
                          {result.file_warnings.map((warning, warningIndex) => (
                            <li key={`stream-code-${index}-warning-${warningIndex}`}>
                              ⚠️ {warning}
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </details>
                  ))}
                </div>
              ) : null}
              {streamState.fact_checks && streamState.fact_checks.length > 0 ? (
                <ul className="fact-checks" aria-label="Fact checks">
                  {streamState.fact_checks.map((result, index) => (
                    <li key={`stream-fact-${index}`} className="fact-check">
                      {result.rating ? (
                        <span className="fact-check-rating" aria-label={`Rating: ${result.rating}`}>
                          {result.rating}
                        </span>
                      ) : null}
                      <span className="fact-check-claim">{result.claim}</span>
                      {result.url ? (
                        <a
                          className="fact-check-source"
                          href={result.url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {result.publisher || result.url}
                        </a>
                      ) : result.publisher ? (
                        <span className="fact-check-source">{result.publisher}</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
              {streamState.academic_results && streamState.academic_results.length > 0 ? (
                <ul className="academic-results" aria-label="Academic search results">
                  {streamState.academic_results.map((result, index) => (
                    <li key={`stream-academic-${index}`} className="academic-result">
                      <span className="academic-result-title">{result.title}</span>
                      {result.authors || result.year ? (
                        <span className="academic-result-meta">
                          {[result.authors, result.year].filter(Boolean).join(" · ")}
                        </span>
                      ) : null}
                      {result.url ? (
                        <a
                          className="academic-result-source"
                          href={result.url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {result.venue || result.url}
                        </a>
                      ) : result.venue ? (
                        <span className="academic-result-source">{result.venue}</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
              {streamState.math_results && streamState.math_results.length > 0 ? (
                <ul className="math-results" aria-label="Computed results">
                  {streamState.math_results.map((result, index) => (
                    <li key={`stream-math-${index}`} className="math-result">
                      <code className="math-result-expression">{result.expression}</code>
                      {result.result ? (
                        <span className="math-result-value">
                          = {result.result}
                          {result.source === "wolfram_alpha" ? (
                            <span className="math-result-source"> (via Wolfram Alpha)</span>
                          ) : null}
                        </span>
                      ) : (
                        <span className="math-result-error" aria-label={`Error: ${result.error}`}>
                          {result.error}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              ) : null}
              {streamState.library_sources && streamState.library_sources.length > 0 ? (
                <p className="library-sources-note">
                  📚 used your library:{" "}
                  {streamState.library_sources.map((source) => source.document).join(", ")}
                </p>
              ) : null}
              {streamState.workflow_steps && streamState.workflow_steps.length > 0 ? (
                <details className="workflow-steps" open>
                  <summary>Workflow: {streamState.workflow_steps.length} step(s)</summary>
                  <ol className="workflow-step-list">
                    {streamState.workflow_steps.map((step, index) => (
                      <li key={`stream-workflow-${index}`} className="workflow-step" data-status={step.status}>
                        <span className="workflow-step-category">{step.category}</span>
                        <p className="workflow-step-instruction">{step.instruction}</p>
                        <p className="workflow-step-meta">
                          {step.model || "?"}
                          {step.status === "failed" ? " · failed" : ""}
                          {formatCost(step.cost_usd) ? ` · ~${formatCost(step.cost_usd)}` : ""}
                        </p>
                      </li>
                    ))}
                  </ol>
                </details>
              ) : streamState.workflowProgress && streamState.workflowProgress.length > 0 ? (
                <ul className="workflow-step-list workflow-step-list-live" aria-label="Workflow progress">
                  {streamState.workflowProgress.map((step, index) => (
                    <li key={`stream-progress-${index}`} className="workflow-step" data-status={step.status}>
                      <span className="workflow-step-category">{step.category}</span>
                      {step.status === "running" ? " working…" : ` ${step.status}`}
                    </li>
                  ))}
                </ul>
              ) : null}
            </article>
          </>
        ) : null}

        {!streaming &&
        unansweredNotice?.conversationId === selectedConversationId &&
        messages.length > 0 &&
        messages[messages.length - 1]?.role === "user" ? (
          <div className="unanswered-notice" role="alert">
            This question didn't get an answer: {unansweredNotice.note}
          </div>
        ) : null}

        {canRegenerate ? (
          <div className="regenerate-bar">
            <button
              className="secondary-button"
              onClick={regenerate}
              disabled={busy}
              title="Uses paid API tokens/credits"
            >
              $ ↻ Regenerate
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

      {showJumpToBottom ? (
        <button
          type="button"
          className="jump-to-bottom"
          onClick={() => messagesEndRef.current?.scrollIntoView({ block: "end", behavior: "smooth" })}
        >
          ↓ Jump to latest
        </button>
      ) : null}
    </>
  );
}
