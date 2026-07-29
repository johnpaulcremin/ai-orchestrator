# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/) once there's a public API contract
worth pinning to — until then, treat a MINOR bump as "notable new capability"
and a PATCH bump as "fix/polish."

## [Unreleased]

### Added

- Per-stage latency telemetry (`app/telemetry.py`'s new `StageTimer`): the
  ask path now stacks several independent stages before any token streams
  back — cross-conversation memory embedding, the exact/semantic response
  cache lookups, routing/classification, moderation, budget reservation,
  the model call itself — and there was no way to see which ONE of those is
  actually slow, only the total request time. `run_orchestrator`/
  `stream_orchestrator`'s completion log line (`request.ok`/`stream.ok`)
  now includes a `stages=[cache=Nms semantic_cache=Nms routing=Nms
  moderation=Nms budget=Nms model_call=Nms post_processing=Nms]` breakdown.
  A new optional `pre_stage_timings` param folds in stages measured by the
  caller before the orchestrator was ever invoked — currently
  `memory_embed`, timed in `routers/messages.py`'s `_recall_memory` (only
  surfaced when `CROSS_CONVERSATION_MEMORY` is actually on, so the common
  case doesn't get a noisy `memory_embed=0ms` on every request). Purely
  additive to the log line — no behavior change, no new endpoint.
- Rotating periodic database backups (`DB_BACKUP`, default **on**): copies
  the whole SQLite database file (after `PRAGMA wal_checkpoint(TRUNCATE)`)
  and keeps the most recent `DB_BACKUP_MAX_COUNT` (default 7), deleting
  older ones. No background scheduler — same "cheap staleness check on a
  naturally-frequent request path" design as `MODEL_CATALOG_SYNC`: `GET
  /v1/conversations` (hit every time the sidebar loads) checks whether a
  backup is due (`DB_BACKUP_INTERVAL_HOURS`, default 24h) and only actually
  copies + rotates on the rare call where it is. New `app/db_backup.py`. A
  local file copy, never a network call, so — unlike `MODEL_CATALOG_SYNC`/
  `FACT_CHECK` — this defaults on, the same reasoning as
  `IMAGE_DOWNSCALE`/`OCR_REPLACEMENT`. Backups are named
  `<db file>.backup-<UTC timestamp>`, distinct from `database.py`'s existing
  one-off `<db file>.bak-v<version>-<timestamp>` migration backup, so
  rotation here never touches (or counts) that one.
- Coverage-threshold CI gates, backend and frontend: both had coverage
  measured (not enforced) since day one, deliberately waiting for a real
  baseline before picking a number. With that baseline now in (94% backend,
  90.75%/81.02%/88.99% frontend statements/branches/functions), set ratchets
  with headroom: `fail_under = 90` in `pyproject.toml`'s
  `[tool.coverage.report]` (picked up automatically by plain `pytest --cov`,
  no CI workflow change needed), and `thresholds: {statements: 85, lines: 85,
  functions: 80, branches: 75}` in `frontend/vitest.config.ts`, with
  `perFile: false` since vitest's default per-file enforcement would fail
  outright on `types.ts`/`ErrorBoundary.tsx` (0% — a type-only file and an
  error boundary that needs a contrived thrown error to exercise) without
  reflecting a real regression. Both verified to actually fail when the bar
  is set unreachably high, not just measured to pass at the real number. The
  stale "no fail-under threshold yet" comments this used to explain waiting
  for are now accurate again.
- Semantic-cache precision eval (`evals/semantic_cache_run.py`): the routing
  eval predates semantic caching, cross-conversation memory, `math_solve`,
  and moderation — of those, a wrong semantic-cache MATCH is the one
  genuinely new failure mode that can silently serve a confidently wrong
  answer to a different question, so it gets a dedicated eval on the same
  footing as routing accuracy. 20 labeled `(stored, query, should_match)`
  pairs (true paraphrases + topically-adjacent near-misses), scored via real
  embeddings against this app's actual `SEMANTIC_CACHE_THRESHOLD`. Reports
  overall accuracy, paraphrase hit rate, and — the number that matters —
  false-positive rate; `--max-false-positive-rate` defaults to `0`, so any
  near-miss that wrongly clears the threshold fails the run by default.
  `math_solve` (no heuristic trigger to evaluate — the model decides) and
  moderation (checked unconditionally, no gate) don't have an equivalent
  gap; see `evals/README.md` for the reasoning.
- Read-only conversation share links (`POST`/`GET`/`DELETE
  /v1/conversations/{id}/share`, public `GET /v1/shared/{token}`): generate a
  link anyone can open to view a snapshot of a conversation — no account or
  API token needed. Deliberately narrower than the owner's own view (no
  cost/tokens/model/notes/pending-action fields), at most one live link per
  conversation (regenerating invalidates the previous one), optional expiry
  enforced in SQL against `CURRENT_TIMESTAMP`, cascades on conversation
  delete. New `share_tokens` table. The public view is genuinely
  unauthenticated (on `public_router`, bypassing the static-token/JWT
  dependency every other `/v1` route requires) and rate-limited via the
  always-on `auth_limiter`. Frontend: a **🔗 Share** button/modal in the chat
  header, and a standalone, dynamically-imported `SharedConversation` page
  (no router dependency — `main.tsx` checks the URL once at startup).
  Fixed a real bug surfaced while wiring this up: `auth_limiter`'s default
  `key_style="url"` keys each rate-limit bucket off the literal resolved
  request path, so a path-parameterized route like `/v1/shared/{token}`
  effectively had no working rate limit at all — every distinct token value
  got its own bucket, and an attacker enumerating tokens would never trip it.
  Switched `auth_limiter` to `key_style="endpoint"` (keyed by view-function
  identity instead), with no behavior change for the existing fixed-path auth
  routes.
- Shared embedding cache: `app/semantic_cache.py`'s `embed()` (used by both
  Semantic caching and Cross-conversation memory) now caches the embedding
  vector itself, keyed on (embedding model, exact text) — asking the
  identical question twice, or having both features embed the same turn,
  costs one embeddings-API call instead of two. Capped at 2,000 entries
  (oldest evicted first), no opt-in — it's just how `embed()` works now. New
  `embedding_cache` table.
- Token-based checkpoint-fold trigger: the long-conversation-memory
  checkpoint fold (`app/routers/messages.py`) now triggers on either the
  verbatim recent window passing 24 messages (as before) OR its approximate
  token size (chars/4, matching the existing `_SUMMARY_INPUT_CHARS`
  convention — not a real tokenizer) passing 6,000, whichever comes first. A
  handful of very long messages (a pasted log, a large diff) could
  previously stay unfolded well past a reasonable context size since the
  count-based trigger alone wouldn't fire until 24 messages accumulated.
- Optional Wolfram Alpha fallback for precision math
  (`WOLFRAM_ALPHA_APP_ID`): when SymPy fails to parse or compute an
  expression that has already passed `math_solve`'s three safety layers,
  and this key is set, `solve_math()` falls back to Wolfram Alpha's Short
  Answers API instead of just reporting an error. Never offered a
  security-rejected expression — nothing further to sanitize before it
  reaches an external API. Entirely optional; `math_solve` behaves exactly
  as before with this unset. New `source` field (`"sympy"` or
  `"wolfram_alpha"`) on `MathResult` records which engine actually produced
  the result.
- Optional precision math (`MATH_SOLVE=true`): the model gets a `math_solve`
  tool for an exact, verified algebra/calculus result (solve/simplify/
  differentiate/integrate/evaluate) from SymPy instead of computing one
  itself and risking an error. Free, local, zero LLM tokens, no external
  API or key. Cross-provider from the start (OpenAI function tool,
  Anthropic custom tool-use, same shared schema) — unlike the earlier
  code-execution/actions parity work, this didn't start OpenAI-only. Unlike
  `propose_action`, a call is executed IMMEDIATELY (no confirmation step,
  since the computation has no real-world side effects) and folded straight
  into the answer, the same "auto-run, result inline" shape as
  `CODE_EXECUTION` just without a hosted sandbox — this app's own process
  runs the computation in-process. `expression` is untrusted model output,
  so it passes through three independent defense layers before ever
  reaching the parser: a strict character allowlist (no quotes/brackets/
  backticks/semicolons — the entire string-literal-based injection surface
  a real math expression never needs), a keyword denylist (`import`/`exec`/
  `eval`/`os.`/`__`/...), and an evaluation namespace with Python's own
  builtins explicitly stripped. New `math_results` field threaded through
  the full message-persistence surface (add/restore/duplicate/branch/import
  a conversation) the same way `code_results`/`fact_checks` already are. New
  `sympy` dependency. Off by default, editable at runtime from the Settings
  panel.
- Optional fact-check lookup (`FACT_CHECK=true` + `GOOGLE_FACT_CHECK_API_KEY`):
  a claim-verification question ("fact check: ...", "is it true that...",
  "debunk...") triggers a lookup against Google's Fact Check Tools API,
  surfacing up to 5 published fact-checks (Snopes, PolitiFact, Reuters Fact
  Check, ...) as `fact_checks: [{"claim", "rating", "publisher", "url"}]` on
  the answer, persisted with the message. Same "standalone call gated by a
  phrase heuristic" design as the Gemini/Imagen image-generation path,
  independent of which model answers — neither OpenAI nor Anthropic offers
  a hosted tool for this, and this app has no client-side tool-execution
  loop to hand a model a tool that isn't hosted server-side by the
  provider. Genuinely different from web search: this queries a structured
  database of claims already reviewed by professional fact-checkers,
  returning a claim/rating/publisher/url per hit rather than raw page
  content the model has to interpret itself. New schema field threaded
  through the full message-persistence surface (add/restore/duplicate/
  branch/import a conversation) the same way `code_results` already is.
  Off by default, no LLM tokens involved, editable at runtime from the
  Settings panel.
- Optional cross-conversation memory (`CROSS_CONVERSATION_MEMORY=true`): a
  new turn on any conversation can recall relevant exchanges from the same
  owner's OTHER conversations via OpenAI embedding similarity, folding up
  to `MEMORY_TOP_K` of them into the prompt as extra context. Reuses
  `app/semantic_cache.py`'s "no vector DB, brute-force cosine scan" design
  directly (its `embed`/`_cosine_similarity` helpers), but is a genuinely
  different mechanism: semantic caching serves a cached ANSWER outright for
  a context-free near-duplicate question; this only injects past exchanges
  as context for the model's own judgment, the same way a conversation's
  own history-summary already works, just reaching across conversation
  boundaries. A looser match threshold than semantic caching's (0.75 vs
  0.96) follows from that — a false positive here is a materially cheaper
  mistake. Scoped to the main ask/ask-stream endpoints only, per-owner
  entry cap (oldest evicted first), `GET`/`DELETE /v1/memory` for
  status/clear, off by default, editable at runtime from the Settings
  panel.
- Optional moderation safety net (`MODERATION=true`): the incoming question
  is checked against OpenAI's moderation endpoint before any budget
  reservation or model call. This is a genuinely new capability, not another
  provider-parity extension — every other tool in this app (web search,
  actions, code execution) is offered TO the answering model; this instead
  runs independently of it, checking what the user sent rather than what a
  model decides to say. A flagged question is refused immediately (empty
  answer, flagged categories in `notes`, nothing spent). OpenAI-only, no new
  key, no extra token cost, off by default, editable at runtime from the
  Settings panel like any other feature flag.
- Code execution (`CODE_EXECUTION=true`) now reaches Anthropic-served
  models too, not just OpenAI — a `claude-*` model can run Python via
  Anthropic's beta `code_execution` tool, the same opt-in/propose-nothing
  pattern as OpenAI's `code_interpreter`. This closes the last deliberately
  deferred item from the earlier cross-provider tool parity work (web
  search, then action proposals, now code execution). Anthropic's tool is
  still beta-gated (reached via the SDK's `client.beta.messages` namespace
  with an explicit beta header, not the stable `client.messages`) and
  several dated tool-type variants exist; the most broadly documented one
  (`code_execution_20250825`) is used here. One real asymmetry versus
  OpenAI's path: Claude's generated files come back only as a file-id
  reference, not inline base64 image data — each one is now downloaded via
  a separate Anthropic Files API round trip (metadata, to filter to actual
  images, then content) before landing in `code_results[].images`, so a
  generated plot renders the same way regardless of which provider produced
  it. A non-image generated file or a failed download is silently skipped
  (nothing in the UI renders anything else there).
- Action proposals (`ACTIONS_WEBHOOK_URL`/`ACTIONS_WEBHOOKS`) now reach
  Anthropic-served models, not just OpenAI — a `claude-*` model can propose
  a webhook action via Anthropic's native custom tool-use, same JSON schema
  (and same named-route enum restriction) as the OpenAI `function` tool
  already offered. Web search retrieval got this same cross-provider
  treatment earlier; action proposals were the other half of "cross-provider
  tool parity" deliberately deferred at the time (see the 0.1.0 entry
  below).

### Changed

- Conversation import (`POST /v1/conversations/import`) and single-message
  restore (`POST /v1/conversations/{id}/messages/restore`) now round-trip
  attachments (images/files) instead of dropping them — validated through
  the same count/size/mime checks a freshly-attached upload goes through,
  so a malformed or oversized attachment fails the whole request (`422`)
  rather than being silently omitted.

## [0.1.0] - 2026-07-29

First tagged release. The project had been under active development for a
while before this point (see `git log` for the full commit history) but
never had a version tag or changelog — this entry marks where that starts,
and describes the feature set as it stood at the tag rather than listing
every prior commit chronologically.

### Highlights

- **Cost-aware routing** — an AI classifier (with a keyword-heuristic
  fallback) sends each request to a budget/fast/smart tier, with
  per-category model overrides, a cross-vendor fallback chain, and
  zero-cost local models via Ollama.
- **Multi-provider** — OpenAI (native Responses API), Anthropic (native
  Messages API), and 100+ others via LiteLLM (Gemini, Bedrock, Mistral,
  Groq, ...), plus an OpenAI-compatible `/v1/chat/completions` endpoint so
  any external tool can route through this app instead of talking to a
  provider directly.
- **Cost controls** — a global and per-owner daily spend cap (atomic
  reservation, not check-then-spend), exact + semantic response caching, a
  live pre-send cost preview, and a Usage dashboard with a tokens-per-dollar
  efficiency KPI.
- **Tool use** — optional, opt-in web search (OpenAI + Anthropic), image
  generation (OpenAI + Gemini/Imagen), code execution (OpenAI), and
  propose-then-confirm action webhooks (Zapier/Make-style), plus vision and
  document (PDF/plain-text) input across every provider.
- **Conversation management** — persistence, auto-titling, long-conversation
  memory (checkpoint-based summarization), edit/regenerate/branch/duplicate/
  summarize, bookmarks, saved prompt templates, tags, favorites, archive,
  full export/import fidelity (minus attachments), and deep-linkable
  conversations/messages.
- **Auth** — an optional static bearer token and/or JWT username/password
  accounts with per-user conversation ownership.
- **Frontend** — a React UI split into focused components (Sidebar/Composer/
  MessageList plus per-feature modals, each lazy-loaded), dark mode,
  keyboard shortcuts, and accessibility conventions (focus trapping,
  `role="alert"`, explicit `aria-label`s).
- **Backend architecture** — `app/main.py` and `app/orchestrator.py` split
  into focused `app/routers/*.py` and `app/orchestrator_*.py` modules;
  versioned SQLite schema migrations with a pre-migration backup; a
  Playwright E2E smoke suite against a stubbed provider; ~94% backend and
  ~91% frontend line coverage.

See [docs/features.md](docs/features.md) for the full feature list, and
[docs/api-reference.md](docs/api-reference.md) for every endpoint.
