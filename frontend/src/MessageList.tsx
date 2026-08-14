import {
  type ComponentPropsWithoutRef,
  type Dispatch,
  type RefObject,
  type SetStateAction,
  type SyntheticEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { supportsRegexLookbehind } from "./markdownSupport";
import { TEXT_ENTRY_ASSISTS } from "./textEntry";

// See markdownSupport.ts: remark-gfm crashes rendering on Safari < 16.4.
// Dropping it there degrades to plain CommonMark instead of a blank screen.
const gfmPluginsIfSupported = supportsRegexLookbehind ? [remarkGfm] : [];
import {
  Bookmark,
  BookmarkCheck,
  Check,
  Copy,
  FileDown,
  GitBranch,
  Link2,
  MoreHorizontal,
  Pencil,
  ThumbsDown,
  ThumbsUp,
  Trash2,
  Volume2,
  VolumeX,
} from "lucide-react";
import { Button } from "./Button";
import { formatTimestamp, formatCost, modelBadgeLabel } from "./format";
import {
  collectGeneratedFiles,
  generatedFileLink,
  generatedImageFilename,
  preserveSandboxUrls,
} from "./generatedFileLinks";
import type { CodeFile, Conversation, Message, SpreadsheetPreview, StreamState } from "./types";

// The two generated-file mime types POST /v1/spreadsheet-preview can parse
// (see app/routers/media.py) — every other generated file (.docx, .pdf, an
// image) only ever gets the plain download link.
const PREVIEWABLE_SPREADSHEET_MIMES = new Set([
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "text/csv",
]);

// What the preview grid is NOT showing, said outright — never a silent
// truncation. Both axes can be capped independently (see
// spreadsheet_ingestion.py's _PREVIEW_MAX_ROWS/_PREVIEW_MAX_COLS), so this
// names whichever actually were, and returns null when the grid is the
// whole file.
function capNotice(preview: SpreadsheetPreview): string | null {
  const shownRows = preview.rows.length;
  const shownCols = preview.rows[0]?.length ?? 0;
  const parts: string[] = [];
  if (shownRows < preview.total_rows) {
    parts.push(`first ${shownRows} of ${preview.total_rows} rows`);
  }
  if (shownCols < preview.total_cols) {
    parts.push(`first ${shownCols} of ${preview.total_cols} columns`);
  }
  if (parts.length === 0) {
    return null;
  }
  return `Showing ${parts.join(" and ")} — download the file for all of it.`;
}

// Inline preview for a generated .xlsx/.csv file, alongside its existing
// download link — lazy: nothing is fetched until the disclosure is first
// opened, and any failure (network error, malformed/oversized/corrupt file)
// degrades silently to "use the download link above", never a broken
// message (see POST /v1/spreadsheet-preview's docstring for the same
// contract from the backend's side).
//
// The grid scrolls INSIDE this panel on both axes and never widens the
// message card that contains it (see App.css's .spreadsheet-preview* rules
// for the containment chain). A scrollbar alone is not a sufficient
// affordance — it's invisible until you scroll on a touch device, where an
// unscrolled wide table reads as "the data is truncated" — so a right-edge
// fade marks "there is more this way", and is driven from real measurements
// here rather than CSS, which cannot ask whether an element is scrolled.
function SpreadsheetPreviewBlock({
  file,
  fetchPreview,
}: {
  file: CodeFile;
  fetchPreview: (file: CodeFile) => Promise<SpreadsheetPreview | null>;
}) {
  const [preview, setPreview] = useState<SpreadsheetPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const [canScrollRight, setCanScrollRight] = useState(false);
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) {
      return;
    }
    // 1px of slack: fractional layout widths make an exact equality test
    // flicker the fade on at the far right of a fully-scrolled table.
    const update = () => {
      const maxScroll = el.scrollWidth - el.clientWidth;
      setCanScrollRight(maxScroll > 1 && el.scrollLeft < maxScroll - 1);
    };
    update();
    el.addEventListener("scroll", update, { passive: true });
    // The table's width depends on its content and the card's width, so
    // neither mount nor a scroll event alone is enough — a resized window
    // (or a sidebar opening) changes whether there's anything to scroll to.
    const observer =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    observer?.observe(el);
    return () => {
      el.removeEventListener("scroll", update);
      observer?.disconnect();
    };
  }, [preview]);

  async function handleToggle(event: SyntheticEvent<HTMLDetailsElement>) {
    if (!event.currentTarget.open || attempted) {
      return;
    }
    setAttempted(true);
    setLoading(true);
    const result = await fetchPreview(file);
    setPreview(result);
    setLoading(false);
  }

  const [headerRow, ...bodyRows] = preview?.rows ?? [];
  const notice = preview ? capNotice(preview) : null;

  return (
    <details className="spreadsheet-preview" onToggle={(event) => void handleToggle(event)}>
      <summary>Preview: {file.filename}</summary>
      {loading ? (
        <p className="spreadsheet-preview-status">Loading preview…</p>
      ) : preview ? (
        <>
          <p className="spreadsheet-preview-meta">
            <span className="spreadsheet-preview-sheet">
              {preview.sheet_name || file.filename}
            </span>
            <span className="spreadsheet-preview-shape">
              {preview.total_rows.toLocaleString()}{" "}
              {preview.total_rows === 1 ? "row" : "rows"} ×{" "}
              {preview.total_cols.toLocaleString()}{" "}
              {preview.total_cols === 1 ? "column" : "columns"}
            </span>
          </p>
          {notice ? <p className="spreadsheet-preview-truncated">{notice}</p> : null}
          {headerRow ? (
            <div
              className="spreadsheet-preview-scroller"
              data-can-scroll-right={canScrollRight ? "true" : undefined}
            >
              <div className="spreadsheet-preview-table-wrap" ref={scrollerRef} tabIndex={0}>
                <table className="spreadsheet-preview-table">
                  {/* The first row is treated as the header row: it's what a
                      generated spreadsheet virtually always is, and it's what
                      makes the sticky header meaningful when the grid
                      scrolls vertically. */}
                  <thead>
                    <tr>
                      {headerRow.map((cell, cellIndex) => (
                        <th key={cellIndex} scope="col">
                          {cell}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {bodyRows.map((row, rowIndex) => (
                      <tr key={rowIndex}>
                        {row.map((cell, cellIndex) => (
                          <td key={cellIndex}>{cell}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : (
            <p className="spreadsheet-preview-status">This file has no rows to preview.</p>
          )}
        </>
      ) : attempted ? (
        <p className="spreadsheet-preview-status">
          Preview unavailable — use the download link above.
        </p>
      ) : null}
    </details>
  );
}

function formatAudioDuration(seconds?: number | null): string | null {
  if (seconds == null || !Number.isFinite(seconds)) {
    return null;
  }
  const total = Math.round(seconds);
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

const SPEAK_ENGINE_STORAGE_KEY = "ai-workbench:speak-engine";

// Defaults to the paid AI voice, matching the behaviour before this was
// persisted — only an explicit, stored "free" changes it. Reads defensively:
// localStorage throws in a sandboxed iframe and under some privacy modes, and
// a voice preference is not worth failing a render over.
function readSpeakEngine(): "paid" | "free" {
  try {
    return window.localStorage.getItem(SPEAK_ENGINE_STORAGE_KEY) === "free" ? "free" : "paid";
  } catch {
    return "paid";
  }
}

function storeSpeakEngine(engine: "paid" | "free"): void {
  try {
    window.localStorage.setItem(SPEAK_ENGINE_STORAGE_KEY, engine);
  } catch {
    // A preference that cannot be saved still applies for this session.
  }
}

const SUMMARIZE_TRANSCRIPT_PROMPT =
  "Summarize the meeting transcript above, with clear action items and owners if mentioned.";

type OutputTokenCaps = { budget?: number; fast?: number; smart?: number };

// Which tier's ceiling this number IS, for naming the limit a truncated answer
// hit ("the 4,000-token smart-tier ceiling"). Matched by value rather than
// parsed out of mode_used, because mode_used doesn't always say: a forced model
// records "forced:<model>" and borrows some tier's budget without naming it.
// Returns "" when the number matches no tier, or matches more than one (an
// operator is free to configure two tiers to the same ceiling) — the caller
// then states the number without claiming a tier for it.
function tierNameForCap(cap: number, caps: OutputTokenCaps): string {
  const matches = (["budget", "fast", "smart"] as const).filter(
    (tier) => caps[tier] === cap,
  );
  return matches.length === 1 ? matches[0] : "";
}

// The ceiling a re-route option would answer under, or null when it can't be
// known. "re-route (auto)" is deliberately null: auto picks a tier per request,
// so no promise about its ceiling can be made in advance. A forced model
// borrows a tier's budget according to the CURRENT composer mode — the same
// mapping app/routing.py's forced_model branch applies.
function capForRegenChoice(
  choice: string,
  caps: OutputTokenCaps,
  composerMode: string,
): number | null {
  if (choice.startsWith("mode:")) {
    const tier = choice.slice("mode:".length);
    return tier === "budget" || tier === "fast" || tier === "smart"
      ? (caps[tier] ?? null)
      : null;
  }
  if (choice.startsWith("model:")) {
    if (composerMode === "fast" || composerMode === "budget") {
      return caps[composerMode] ?? null;
    }
    return caps.smart ?? null;
  }
  return null;
}

// The suffix option E adds to a re-route option that cannot fix a truncation:
// its own ceiling is no higher than the one that just cut an answer off, so
// picking it re-runs the same failure at the same limit. Only ever a suffix —
// the option stays selectable, since a user may want a cheaper tier for
// reasons that have nothing to do with length. Returns "" whenever the
// comparison can't be made (ceiling unknown, previous attempt not truncated),
// so an unannotated option never implies it has room.
function noRoomSuffix(
  choice: string,
  caps: OutputTokenCaps,
  composerMode: string,
  truncatedCap: number | null,
): string {
  if (truncatedCap == null) {
    return "";
  }
  const cap = capForRegenChoice(choice, caps, composerMode);
  if (cap == null || cap > truncatedCap) {
    return "";
  }
  return ` — ${cap.toLocaleString()} cap, no more room`;
}

// Whether an answer came from workflow mode. `workflow_steps` is the only
// reliable marker: mode_used can read "workflow(5 steps)" OR
// "auto->workflow(5 steps)", and a re-routed retry rewrites it — the
// per-step breakdown is present exactly when a workflow produced the answer.
function isWorkflowAnswer(message: Message): boolean {
  return !!message.workflow_steps && message.workflow_steps.length > 0;
}

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
  unansweredNotice: { conversationId: number; note: string; details?: string } | null;
  selectedConversationId: number | null;
  canRegenerate: boolean;
  regenerate: () => Promise<void>;
  // Re-answer the last turn as a multi-step workflow — offered only on the
  // truncation notice, where a tier change may have no headroom to offer.
  retryAsWorkflow: () => Promise<void>;
  isPinned: boolean;
  regenChoice: string;
  setRegenChoice: Dispatch<SetStateAction<string>>;
  budgetTierEnabled: boolean;
  // Each tier's output-token ceiling (see App.tsx's outputTokenCaps). Empty
  // until /v1/status has answered; every use below degrades to saying nothing.
  outputTokenCaps: { budget?: number; fast?: number; smart?: number };
  // The composer's current mode, which decides which tier's ceiling a FORCED
  // model borrows — auto/smart/workflow take the smart ceiling, fast and budget
  // their own (app/routing.py's forced_model branch).
  composerMode: string;
  forcedModelOptions: string[];
  messagesEndRef: RefObject<HTMLDivElement | null>;
  messagesContainerRef: RefObject<HTMLDivElement | null>;
  showJumpToBottom: boolean;
  insertIntoComposer: (text: string) => void;
  fetchSpreadsheetPreview: (file: CodeFile) => Promise<SpreadsheetPreview | null>;
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
  retryAsWorkflow,
  isPinned,
  regenChoice,
  setRegenChoice,
  budgetTierEnabled,
  outputTokenCaps,
  composerMode,
  forcedModelOptions,
  messagesEndRef,
  messagesContainerRef,
  showJumpToBottom,
  insertIntoComposer,
  fetchSpreadsheetPreview,
}: Props) {
  // Which engine the merged per-message speak button uses -- a pure UI
  // preference shared across every message (mirrors Composer.tsx's mic
  // engine choice), not lifted to App.tsx since nothing outside this
  // component needs it.
  // Persisted, unlike the session-scoped cost confirmation in App.tsx's
  // toggleSpeak. Those two answer different questions: the confirmation is
  // "do you want to spend on THIS clip, right now", which should be asked
  // again on a fresh load; this is "which voice do you use", a standing
  // preference that was previously forgotten on every reload — so a user who
  // deliberately chose the free voice was quietly put back on the paid one.
  const [speakEngine, setSpeakEngine] = useState<"paid" | "free">(readSpeakEngine);

  // Which message's 👎 reason popover is currently open, if any -- a click
  // on 👎 for a message not yet rated down opens this instead of rating
  // immediately, so the optional reason has somewhere to go without a
  // modal. Escape/click-away below still records the 👎 with no reason.
  const [reasonPopoverFor, setReasonPopoverFor] = useState<number | null>(null);

  // Which message's secondary action bar (everything but Copy/Bookmark) is
  // expanded on a narrow screen -- below ~850px (App.css), .message-actions
  // is always visible (no :hover to reveal it) and its ~9 icon buttons no
  // longer fit on one or two reasonable rows, so all but the two most-used
  // collapse behind a single "More actions" toggle per message. No-op above
  // that width, where CSS keeps .message-actions-overflow-toggle hidden and
  // .message-actions-secondary always shown regardless of this state.
  const [expandedActionsFor, setExpandedActionsFor] = useState<number | null>(null);

  // The last message in the conversation, if any. A workflow retry re-answers
  // the LAST turn (that is all POST .../regenerate can target), so the notice
  // only offers it on a truncated answer that is still the last message —
  // offering it on an older one would silently re-answer a different question.
  // Continue has no such limitation: it appends to the message it names.
  const lastMessage = messages.length > 0 ? messages[messages.length - 1] : null;

  // The ceiling the most recent answer was cut off at, or null when the last
  // answer wasn't truncated or didn't record one (a workflow, or a message
  // written before the column existed). Drives option E's annotations in the
  // re-route dropdown below.
  const truncatedCap =
    lastMessage?.role === "assistant" && lastMessage.truncated
      ? (lastMessage.max_output_tokens ?? null)
      : null;

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
                  Click <strong>New conversation</strong> above the sidebar to start your first
                  conversation.
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
                {message.role === "assistant" && modelBadgeLabel(message.model, message.mode_used) ? (
                  // Which model actually answered. `mode_used` names a TIER
                  // ("auto->fast"), and the tier→model map is configurable, so
                  // this is the only place the real answer shows -- except for
                  // the two routing shapes that already embed it, which
                  // modelBadgeLabel suppresses. Full id kept in the title, so
                  // truncating the label loses nothing.
                  <span className="model-badge" title={message.model ?? undefined}>
                    {modelBadgeLabel(message.model, message.mode_used)}
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
                  {/* Mobile-only (App.css's ~850px breakpoint hides this
                      entirely above it): Copy + Bookmark above stay inline
                      as the two most-used actions; everything below this
                      point is a "secondary" action, hidden on a narrow
                      screen until this toggle expands it -- see
                      expandedActionsFor's own docstring. */}
                  <Button
                    iconOnly
                    size="sm"
                    variant="ghost"
                    className="message-actions-overflow-toggle"
                    onClick={() =>
                      setExpandedActionsFor((current) => (current === message.id ? null : message.id))
                    }
                    aria-label={`More actions for the ${message.role} message from ${formatTimestamp(message.created_at)}`}
                    aria-expanded={expandedActionsFor === message.id}
                    icon={<MoreHorizontal size={16} />}
                  />
                  <Button
                    iconOnly
                    size="sm"
                    variant="ghost"
                    className={`message-actions-secondary-item${expandedActionsFor === message.id ? " expanded" : ""}`}
                    onClick={() => void copyMessageLink(message)}
                    title={copiedLinkMessageId === message.id ? "Link copied!" : "Copy link to this message"}
                    aria-label={
                      copiedLinkMessageId === message.id ? "Link copied!" : "Copy link to this message"
                    }
                    icon={copiedLinkMessageId === message.id ? <Check size={16} /> : <Link2 size={16} />}
                  />
                  {message.role === "assistant" ? (
                    <div
                      className={`feedback-control message-actions-secondary-item${expandedActionsFor === message.id ? " expanded" : ""}`}
                    >
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
                    <div
                      className={`speak-control message-actions-secondary-item${expandedActionsFor === message.id ? " expanded" : ""}`}
                    >
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
                        onChange={(event) => {
                          const engine = event.target.value as "paid" | "free";
                          setSpeakEngine(engine);
                          storeSpeakEngine(engine);
                        }}
                        title="Choose the voice-output engine — the AI voice bills per character, your browser's is free"
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
                      className={`message-actions-secondary-item${expandedActionsFor === message.id ? " expanded" : ""}`}
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
                    className={`message-actions-secondary-item${expandedActionsFor === message.id ? " expanded" : ""}`}
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
                    className={`message-actions-secondary-item${expandedActionsFor === message.id ? " expanded" : ""}`}
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
                  {/* `a` is overridden so a file this answer NAMES in its prose
                      resolves to the file it actually carries — see
                      generatedFileLinks.tsx for why no href a model writes can
                      ever work on its own. */}
                  <ReactMarkdown
                    remarkPlugins={gfmPluginsIfSupported}
                    urlTransform={preserveSandboxUrls}
                    components={{
                      pre: CodeBlock,
                      a: generatedFileLink(collectGeneratedFiles(message.code_results)),
                    }}
                  >
                    {message.content}
                  </ReactMarkdown>
                </div>
              ) : null}
              {message.role === "assistant" && message.truncated ? (
                <div className="truncated-notice" role="status">
                  {/* Name the ceiling that was hit, not just the fact of it: without
                      the number, "try a different tier" looks like advice, and for a
                      smart-tier answer every tier in the re-route dropdown is capped
                      at or below the one that just failed. The number comes from the
                      message's own record of it (see AskResponse.max_output_tokens),
                      so it describes THIS attempt rather than today's configuration;
                      when it's absent the notice says what it always said. */}
                  <span>
                    {message.max_output_tokens
                      ? `⚠️ Response was cut off at the ${message.max_output_tokens.toLocaleString()}-token ${
                          tierNameForCap(message.max_output_tokens, outputTokenCaps)
                            ? `${tierNameForCap(message.max_output_tokens, outputTokenCaps)}-tier `
                            : ""
                        }ceiling.`
                      : "⚠️ Response was cut off before it finished."}
                  </span>
                  {/* A workflow answer carries `truncated` for a STEP that hit
                      its ceiling, not for this text — the synthesis finished.
                      So neither action below applies: Continue would append a
                      resumption to a complete answer (billed, and recovering
                      nothing the step lost), and "Retry as workflow" would
                      offer a workflow for something that already is one. The
                      notice itself stays — it is the only thing explaining a
                      short file. */}
                  {isWorkflowAnswer(message) ? (
                    <span className="truncated-notice-detail">
                      One step of this workflow hit that ceiling, so its output
                      is incomplete. Re-run the request, or raise the ceiling
                      that step hit.
                    </span>
                  ) : message.no_output ? (
                    /* Cut off before any of the answer was written, so there is
                       nothing to continue — the button would bill a call to
                       resume the app's own explanation. "Retry as workflow"
                       below is the remedy that actually applies, since it is
                       not bounded by any one tier's ceiling. */
                    <span className="truncated-notice-detail">
                      Nothing was written before the cut-off, so there is
                      nothing to continue.
                    </span>
                  ) : (
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => void continueMessage(message)}
                      disabled={continuingMessageId === message.id}
                      title="Uses paid API tokens/credits"
                    >
                      {continuingMessageId === message.id ? "Continuing…" : "$ Continue"}
                    </button>
                  )}
                  {message.id === lastMessage?.id && !isWorkflowAnswer(message) ? (
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => void retryAsWorkflow()}
                      disabled={busy}
                      title="Re-answers in several capped steps, so the total isn't bounded by one tier's ceiling. Uses paid API tokens/credits."
                    >
                      $ Retry as workflow
                    </button>
                  ) : null}
                </div>
              ) : null}
              {message.role !== "assistant" ? (
                editingMessageId === message.id ? (
                  <div className="edit-message-form">
                    <textarea
                      value={editDraft}
                      onChange={(event) => setEditDraft(event.target.value)}
                      aria-label="Edit question"
                      {...TEXT_ENTRY_ASSISTS}
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
              {message.role === "assistant" &&
              ((message.search_queries && message.search_queries.length > 0) ||
                (message.sources && message.sources.length > 0)) ? (
                <details className="tool-card">
                  <summary>Web search</summary>
                  {message.search_queries && message.search_queries.length > 0 ? (
                    <ul className="web-search-queries" aria-label="Search queries">
                      {message.search_queries.map((query, index) => (
                        <li key={`${message.id}-query-${index}`}>{query}</li>
                      ))}
                    </ul>
                  ) : null}
                  {message.sources && message.sources.length > 0 ? (
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
                </details>
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
                          {result.images.map((src, imageIndex) => {
                            const filename = generatedImageFilename(src, imageIndex);
                            return (
                              <figure
                                className="code-result-image"
                                key={`${message.id}-code-${index}-image-${imageIndex}`}
                              >
                                <img src={src} alt="Code output" />
                                <figcaption>
                                  <a
                                    href={src}
                                    download={filename}
                                    className="code-result-file-link"
                                  >
                                    <FileDown size={16} aria-hidden="true" /> {filename}
                                  </a>
                                </figcaption>
                              </figure>
                            );
                          })}
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
                              {PREVIEWABLE_SPREADSHEET_MIMES.has(file.mime_type) ? (
                                <SpreadsheetPreviewBlock
                                  file={file}
                                  fetchPreview={fetchSpreadsheetPreview}
                                />
                              ) : null}
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
                <details className="tool-card">
                  <summary>Fact-checked</summary>
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
                </details>
              ) : null}
              {message.role === "assistant" &&
              message.academic_results &&
              message.academic_results.length > 0 ? (
                <details className="tool-card">
                  <summary>Academic search</summary>
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
                </details>
              ) : null}
              {message.role === "assistant" &&
              message.math_results &&
              message.math_results.length > 0 ? (
                <details className="tool-card">
                  <summary>Computed (math_solve)</summary>
                  <ul className="math-results" aria-label="Computed results">
                    {message.math_results.map((result, index) => (
                      <li key={`${message.id}-math-${index}`} className="math-result">
                        <code className="math-result-expression">{result.expression}</code>
                        {result.result ? (
                          <span className="math-result-value">
                            = {result.result}
                            {result.source ? (
                              <span className="math-result-source">
                                {" "}
                                (via {result.source === "wolfram_alpha" ? "Wolfram Alpha" : "SymPy"})
                              </span>
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
                </details>
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
              message.memory_sources &&
              message.memory_sources.length > 0 ? (
                <details className="tool-card memory-indicator">
                  <summary>🧠 Used memory from {message.memory_sources.length} past conversation{message.memory_sources.length > 1 ? "s" : ""}</summary>
                  <ul className="memory-sources-list" aria-label="Recalled conversations">
                    {message.memory_sources.map((source, index) => (
                      <li key={`${message.id}-memory-${index}`} className="memory-source">
                        {source.conversation_title}
                        {source.created_at ? (
                          <span className="memory-source-date"> · {formatTimestamp(source.created_at)}</span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </details>
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
              {(streamState.search_queries && streamState.search_queries.length > 0) ||
              (streamState.sources && streamState.sources.length > 0) ? (
                <details className="tool-card" open>
                  <summary>Web search</summary>
                  {streamState.search_queries && streamState.search_queries.length > 0 ? (
                    <ul className="web-search-queries" aria-label="Search queries">
                      {streamState.search_queries.map((query, index) => (
                        <li key={`stream-query-${index}`}>{query}</li>
                      ))}
                    </ul>
                  ) : null}
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
                </details>
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
                              {PREVIEWABLE_SPREADSHEET_MIMES.has(file.mime_type) ? (
                                <SpreadsheetPreviewBlock
                                  file={file}
                                  fetchPreview={fetchSpreadsheetPreview}
                                />
                              ) : null}
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
                <details className="tool-card" open>
                  <summary>Fact-checked</summary>
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
                </details>
              ) : null}
              {streamState.academic_results && streamState.academic_results.length > 0 ? (
                <details className="tool-card" open>
                  <summary>Academic search</summary>
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
                </details>
              ) : null}
              {streamState.math_results && streamState.math_results.length > 0 ? (
                <details className="tool-card" open>
                  <summary>Computed (math_solve)</summary>
                  <ul className="math-results" aria-label="Computed results">
                    {streamState.math_results.map((result, index) => (
                      <li key={`stream-math-${index}`} className="math-result">
                        <code className="math-result-expression">{result.expression}</code>
                        {result.result ? (
                          <span className="math-result-value">
                            = {result.result}
                            {result.source ? (
                              <span className="math-result-source">
                                {" "}
                                (via {result.source === "wolfram_alpha" ? "Wolfram Alpha" : "SymPy"})
                              </span>
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
                </details>
              ) : null}
              {streamState.library_sources && streamState.library_sources.length > 0 ? (
                <p className="library-sources-note">
                  📚 used your library:{" "}
                  {streamState.library_sources.map((source) => source.document).join(", ")}
                </p>
              ) : null}
              {streamState.memory_sources && streamState.memory_sources.length > 0 ? (
                <details className="tool-card memory-indicator" open>
                  <summary>🧠 Used memory from {streamState.memory_sources.length} past conversation{streamState.memory_sources.length > 1 ? "s" : ""}</summary>
                  <ul className="memory-sources-list" aria-label="Recalled conversations">
                    {streamState.memory_sources.map((source, index) => (
                      <li key={`stream-memory-${index}`} className="memory-source">
                        {source.conversation_title}
                        {source.created_at ? (
                          <span className="memory-source-date"> · {formatTimestamp(source.created_at)}</span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </details>
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
            {unansweredNotice.details ? (
              <details className="message-notes">
                <summary>details</summary>
                <small>{unansweredNotice.details}</small>
              </details>
            ) : null}
          </div>
        ) : null}

        {canRegenerate ? (
          <div className="regenerate-bar">
            <button
              className="secondary-button"
              onClick={regenerate}
              disabled={busy}
              title="Always a fresh answer — skips the response cache. Uses paid API tokens/credits"
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
              {/* Every option keeps its plain label until the previous answer was cut
                  off; from then on the ones with no more headroom than the ceiling it
                  hit say so. Unannotated is never a promise of room — an option whose
                  ceiling can't be known (re-route, or a tier /v1/status hasn't
                  reported) is left alone rather than guessed at. */}
              <option value="">re-route (auto)</option>
              {budgetTierEnabled ? (
                <option value="mode:budget">
                  budget tier
                  {noRoomSuffix("mode:budget", outputTokenCaps, composerMode, truncatedCap)}
                </option>
              ) : null}
              <option value="mode:fast">
                fast tier{noRoomSuffix("mode:fast", outputTokenCaps, composerMode, truncatedCap)}
              </option>
              <option value="mode:smart">
                smart tier{noRoomSuffix("mode:smart", outputTokenCaps, composerMode, truncatedCap)}
              </option>
              {forcedModelOptions.length > 0 ? (
                <optgroup label="force model">
                  {forcedModelOptions.map((model) => (
                    <option key={model} value={`model:${model}`}>
                      {model}
                      {noRoomSuffix(`model:${model}`, outputTokenCaps, composerMode, truncatedCap)}
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
