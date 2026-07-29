export type Mode = "auto" | "budget" | "fast" | "smart";

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

export type CodeResult = {
  code: string;
  logs?: string | null;
  images?: string[] | null;
  files?: CodeFile[] | null;
};

export type FactCheckResult = {
  claim: string;
  rating?: string | null;
  publisher?: string | null;
  url?: string | null;
};

export type MathResult = {
  operation: string;
  expression: string;
  variable: string;
  result?: string | null;
  error?: string | null;
  source?: string | null;
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
  pending_action?: PendingAction | null;
  action_status?: ActionStatus | null;
  // For an assistant message: images the model generated. For a user
  // message: images the user attached (vision input).
  images?: string[] | null;
  // Documents (PDF/plain text) the user attached; always absent on assistant
  // messages — the model can read a file, never produce one.
  files?: FileAttachment[] | null;
  bookmarked?: boolean;
  // True when the provider stopped this answer early (hit the token budget)
  // rather than actually finishing — see the Continue action.
  truncated?: boolean;
  // Code the model ran via the code_interpreter tool, in order.
  code_results?: CodeResult[] | null;
  // Published fact-checks surfaced for a claim-verification question.
  fact_checks?: FactCheckResult[] | null;
  // Exact symbolic/numeric results computed via the math_solve tool.
  math_results?: MathResult[] | null;
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
  code_results?: CodeResult[] | null;
  fact_checks?: FactCheckResult[] | null;
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
  pending_action?: PendingAction | null;
  images?: string[] | null;
  code_results?: CodeResult[] | null;
  fact_checks?: FactCheckResult[] | null;
  math_results?: MathResult[] | null;
  // Images/files the user attached to THIS question, distinct from `images`
  // above which is the model's generated output.
  questionImages?: string[] | null;
  questionFiles?: FileAttachment[] | null;
};
