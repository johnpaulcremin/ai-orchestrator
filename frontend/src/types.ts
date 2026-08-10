export type Mode = "auto" | "budget" | "fast" | "smart" | "workflow";

export type Conversation = {
  id: number;
  title: string;
  owner?: string | null;
  pinned_model?: string | null;
  system_prompt?: string | null;
  favorite?: boolean;
  archived?: boolean;
  tags?: string[];
  created_at: string;
  updated_at: string;
  message_count?: number;
};

export type SearchResult = Conversation & {
  snippet: string;
};

export type Source = {
  title: string;
  url: string;
};

export type PendingAction = {
  action: string;
  summary: string;
  payload: Record<string, unknown>;
};

export type CodeFile = {
  filename: string;
  mime_type: string;
  data: string;
};

// POST /v1/spreadsheet-preview's response — a first ~50 rows x ~20 columns
// glance at a generated .xlsx/.csv file, parsed server-side (see
// app/spreadsheet_ingestion.py) so the frontend never bundles a spreadsheet
// dependency of its own. `total_rows`/`total_cols` are the file's REAL
// dimensions (not `rows`'), which is what lets the UI say "showing first 50
// of 312 rows" instead of truncating silently; `truncated` is true when they
// exceed what's actually in `rows`. `sheet_name` is the worksheet's own
// title for an .xlsx and null for a .csv, which has no sheets.
export type SpreadsheetPreview = {
  rows: string[][];
  total_rows: number;
  total_cols: number;
  truncated: boolean;
  sheet_name?: string | null;
};

// A recorded/uploaded audio clip attached for server-side transcription —
// see app/audio_ingestion.py. The transcript itself is folded into the
// message's `files` as a plain-text document; this is metadata only, never
// the audio bytes.
export type AudioAttachment = {
  filename: string;
  data: string;
  duration_seconds?: number | null;
};

export type AudioMeta = {
  filename: string;
  duration_seconds?: number | null;
};

export type CodeResult = {
  code: string;
  logs?: string | null;
  images?: string[] | null;
  files?: CodeFile[] | null;
  // One line per generated file the sandbox reported but couldn't be
  // attached (unsupported type, oversized, or a failed download) — never
  // silently dropped.
  file_warnings?: string[] | null;
};

export type FactCheckResult = {
  claim: string;
  rating?: string | null;
  publisher?: string | null;
  url?: string | null;
};

export type AcademicResult = {
  title: string;
  authors?: string | null;
  year?: number | null;
  venue?: string | null;
  citation_count?: number | null;
  url?: string | null;
  abstract_snippet?: string | null;
};

export type MathResult = {
  operation: string;
  expression: string;
  variable: string;
  result?: string | null;
  error?: string | null;
  source?: string | null;
};

export type LibrarySource = {
  document: string;
  snippet_count: number;
};

export type MemorySource = {
  conversation_title: string;
  created_at: string;
};

export type WorkflowStep = {
  category: string;
  instruction: string;
  model: string;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cost_usd?: number | null;
  status: string;
};

export type ActionStatus = "pending" | "confirmed" | "declined" | "failed";

export type FileAttachment = {
  filename: string;
  data: string;
};

export type Message = {
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
  // The actual search-query text the web_search tool issued, distinct from
  // `sources` (the RESULTS a search returned).
  search_queries?: string[] | null;
  pending_action?: PendingAction | null;
  action_status?: ActionStatus | null;
  // For an assistant message: images the model generated. For a user
  // message: images the user attached (vision input).
  images?: string[] | null;
  // Documents (PDF/plain text) the user attached; always absent on assistant
  // messages — the model can read a file, never produce one.
  files?: FileAttachment[] | null;
  // Audio clips the user attached, transcribed server-side — metadata only
  // (the transcript itself lives in `files`); always absent on assistant
  // messages.
  audio?: AudioMeta[] | null;
  bookmarked?: boolean;
  // True when the provider stopped this answer early (hit the token budget)
  // rather than actually finishing — see the Continue action.
  truncated?: boolean;
  // The output-token ceiling this answer was generated under, so a truncated
  // answer can name the limit it actually hit and the re-route control can
  // tell which of its options have more headroom than it. Null for a workflow
  // answer (no single ceiling — each step has its own) and for anything
  // persisted before the column existed; the UI omits the number rather than
  // guessing one from the current configuration, which would be a different
  // fact about a different attempt.
  max_output_tokens?: number | null;
  // True when the answer was cut off before ANY of it was written — the whole
  // ceiling went on a tool call's arguments or private reasoning — so this
  // message's content is the app's explanation rather than a partial answer.
  // Always accompanies `truncated`, and narrows what it licenses: there is
  // nothing to resume, so Continue is not offered (the backend refuses it
  // too), while the ceiling notice and "Retry as workflow" still apply.
  no_output?: boolean;
  // Code the model ran via the code_interpreter tool, in order.
  code_results?: CodeResult[] | null;
  // Published fact-checks surfaced for a claim-verification question.
  fact_checks?: FactCheckResult[] | null;
  // Scholarly works surfaced for a research-literature question.
  academic_results?: AcademicResult[] | null;
  // Exact symbolic/numeric results computed via the math_solve tool.
  math_results?: MathResult[] | null;
  // Documents from the owner's RAG document library drawn on for this
  // answer. Deliberately absent from SharedMessage (see schemas.py's
  // SharedMessage docstring) — never exposed to an anonymous share-link
  // recipient.
  library_sources?: LibrarySource[] | null;
  // Cross-conversation memory recalled into this answer's context: which
  // past conversation(s) and when, never the recalled question/answer text
  // itself. Deliberately absent from SharedMessage — same reasoning as
  // library_sources: it would reveal the titles of the owner's OTHER,
  // unshared conversations to an anonymous share-link recipient.
  memory_sources?: MemorySource[] | null;
  // Per-step breakdown for an opt-in multi-step workflow answer (mode=
  // "workflow"); null for every ordinary answer. Deliberately absent from
  // SharedMessage — same reasoning as library_sources (see schemas.py's
  // SharedMessage docstring): it names which models answered which
  // sub-instruction and what each step cost.
  workflow_steps?: WorkflowStep[] | null;
  // The literal model that answered (e.g. "gpt-5", "gemini/gemini-flash-
  // latest"); null for a user message or a message persisted before this
  // field existed.
  model?: string | null;
  // This caller's own 👍/👎 (1/-1) on an assistant message, or null if
  // never rated/cleared. A pure marker like `bookmarked` — never affects
  // the conversation's updated_at. Deliberately absent from SharedMessage —
  // a rating is this caller's own private signal, not something an
  // anonymous share-link recipient should see.
  feedback?: number | null;
  feedback_reason?: string | null;
  created_at: string;
};

export type ShareStatus = {
  active: boolean;
  token?: string | null;
  expires_at?: string | null;
};

export type SharedMessage = {
  role: string;
  content: string;
  created_at: string;
  images?: string[] | null;
  files?: FileAttachment[] | null;
  sources?: Source[] | null;
  search_queries?: string[] | null;
  code_results?: CodeResult[] | null;
  fact_checks?: FactCheckResult[] | null;
  academic_results?: AcademicResult[] | null;
  math_results?: MathResult[] | null;
};

export type SharedConversationData = {
  title: string;
  created_at: string;
  messages: SharedMessage[];
};

export type StreamState = {
  conversationId: number;
  question: string;
  answer: string;
  sources?: Source[] | null;
  search_queries?: string[] | null;
  pending_action?: PendingAction | null;
  images?: string[] | null;
  code_results?: CodeResult[] | null;
  fact_checks?: FactCheckResult[] | null;
  academic_results?: AcademicResult[] | null;
  math_results?: MathResult[] | null;
  library_sources?: LibrarySource[] | null;
  memory_sources?: MemorySource[] | null;
  workflow_steps?: WorkflowStep[] | null;
  // Progress events for an in-flight workflow answer (mode="workflow"),
  // updated as "step" SSE events arrive — separate from workflow_steps
  // above, which is only ever set once, from the terminal "done" event.
  workflowProgress?: WorkflowStep[] | null;
  // Images/files the user attached to THIS question, distinct from `images`
  // above which is the model's generated output.
  questionImages?: string[] | null;
  questionFiles?: FileAttachment[] | null;
  questionAudio?: AudioMeta[] | null;
};
