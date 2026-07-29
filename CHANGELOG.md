# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/) once there's a public API contract
worth pinning to — until then, treat a MINOR bump as "notable new capability"
and a PATCH bump as "fix/polish."

## [Unreleased]

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
