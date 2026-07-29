# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/) once there's a public API contract
worth pinning to — until then, treat a MINOR bump as "notable new capability"
and a PATCH bump as "fix/polish."

## [Unreleased]

### Added

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
