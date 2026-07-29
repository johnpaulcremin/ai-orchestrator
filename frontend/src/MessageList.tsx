import { type ComponentPropsWithoutRef, type Dispatch, type RefObject, type SetStateAction, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { formatTimestamp, formatCost } from "./format";
import type { Conversation, Message, StreamState } from "./types";

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
}: Props) {
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
                <button
                  type="button"
                  className="secondary-button speak-button"
                  onClick={() => void copyMessageLink(message)}
                  title={copiedLinkMessageId === message.id ? "Link copied!" : "Copy link to this message"}
                  aria-label={copiedLinkMessageId === message.id ? "Link copied!" : "Copy link to this message"}
                >
                  {copiedLinkMessageId === message.id ? "✓" : "🔗"}
                </button>
                <button
                  type="button"
                  className={`secondary-button speak-button bookmark-button${message.bookmarked ? " active" : ""}`}
                  onClick={() => void toggleMessageBookmark(message)}
                  title={message.bookmarked ? "Remove bookmark" : "Bookmark this message"}
                  aria-label={
                    message.bookmarked
                      ? `Remove bookmark from ${message.role} message from ${formatTimestamp(message.created_at)}`
                      : `Bookmark ${message.role} message from ${formatTimestamp(message.created_at)}`
                  }
                  aria-pressed={Boolean(message.bookmarked)}
                >
                  {message.bookmarked ? "🔖" : "🏷️"}
                </button>
                {message.role === "assistant" ? (
                  <button
                    type="button"
                    className="secondary-button speak-button"
                    onClick={() => void toggleSpeak(message)}
                    disabled={synthesizingMessageId === message.id}
                    title={
                      speakingMessageId === message.id
                        ? "Stop speaking"
                        : "Read this answer aloud — AI voice, uses paid API tokens/credits"
                    }
                    aria-label={speakingMessageId === message.id ? "Stop speaking" : "Read this answer aloud"}
                  >
                    {synthesizingMessageId === message.id
                      ? "…"
                      : speakingMessageId === message.id
                        ? "⏹"
                        : "$ 🔊"}
                  </button>
                ) : null}
                {message.role === "assistant" ? (
                  <button
                    type="button"
                    className="secondary-button speak-button"
                    onClick={() => toggleFreeSpeak(message)}
                    title={
                      freeSpeakingMessageId === message.id
                        ? "Stop the free text-to-speech"
                        : "Free text-to-speech using your browser's built-in voice — on-device, lower quality"
                    }
                    aria-label={
                      freeSpeakingMessageId === message.id
                        ? "Stop the free text-to-speech"
                        : "Free text-to-speech for this message"
                    }
                  >
                    {freeSpeakingMessageId === message.id ? "⏹" : "🗣️"}
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
                  onClick={() => void branchFromMessage(message)}
                  disabled={branchingMessageId === message.id}
                  title="Branch a new conversation from this point"
                  aria-label={`Branch a new conversation from the ${message.role} message from ${formatTimestamp(message.created_at)}`}
                >
                  {branchingMessageId === message.id ? "…" : "🌿"}
                </button>
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
              message.math_results &&
              message.math_results.length > 0 ? (
                <ul className="math-results" aria-label="Computed results">
                  {message.math_results.map((result, index) => (
                    <li key={`${message.id}-math-${index}`} className="math-result">
                      <code className="math-result-expression">{result.expression}</code>
                      {result.result ? (
                        <span className="math-result-value">= {result.result}</span>
                      ) : (
                        <span className="math-result-error" aria-label={`Error: ${result.error}`}>
                          {result.error}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
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
              {streamState.math_results && streamState.math_results.length > 0 ? (
                <ul className="math-results" aria-label="Computed results">
                  {streamState.math_results.map((result, index) => (
                    <li key={`stream-math-${index}`} className="math-result">
                      <code className="math-result-expression">{result.expression}</code>
                      {result.result ? (
                        <span className="math-result-value">= {result.result}</span>
                      ) : (
                        <span className="math-result-error" aria-label={`Error: ${result.error}`}>
                          {result.error}
                        </span>
                      )}
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
