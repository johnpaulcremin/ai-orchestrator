# AI Orchestrator

A local AI workbench that routes every request to the cheapest model that can handle it. A tiny classifier model looks at each question and dispatches it to a **fast** tier (quick facts, chat, summaries, reformatting) or a **smart** tier (coding, debugging, reasoning, planning, math, analysis) — so you stop paying flagship-model prices for questions a mini model answers just as well. Conversations are saved to SQLite with automatic titling, answers stream token-by-token over SSE, and a fallback chain keeps requests succeeding even when the primary model errors. A React UI sits on top; the whole thing runs on your machine with one API key.

## Architecture

```mermaid
flowchart TD
    UI["React UI<br/>Vite dev server :5173"] -- "/api/* proxied to :8000" --> API["FastAPI backend<br/>app/main.py"]
    API --> MODE{"mode?"}
    MODE -- "fast" --> FAST["Fast model<br/>OPENAI_MODEL_FAST"]
    MODE -- "smart" --> SMART["Smart model<br/>OPENAI_MODEL_SMART"]
    MODE -- "auto" --> CLS["AI classifier<br/>OPENAI_MODEL_ROUTER"]
    CLS -- "simple task" --> FAST
    CLS -- "smart category or<br/>high complexity" --> SMART
    CLS -. "classifier unavailable" .-> HEUR["Keyword heuristic"]
    HEUR --> FAST
    HEUR --> SMART
    FAST -. "API error" .-> FB["Fallback chain<br/>OPENAI_MODEL_FALLBACK &rarr; FAST &rarr; OPENAI_MODEL"]
    SMART -. "API error" .-> FB
    FAST --> ANS["Answer + routing notes"]
    SMART --> ANS
    FB --> ANS
    ANS --> DB[("SQLite<br/>conversations + messages")]
    ANS -- "SSE stream / JSON" --> UI
```

Request lifecycle for a conversation ask: the user message is persisted first, the last 12 messages are folded into a context prompt, the router picks a model, the answer streams back (or returns as JSON), and the assistant message is persisted with its routing metadata before the terminal event is sent.

## Features

- **AI-based routing** — a cheap classifier model (`OPENAI_MODEL_ROUTER`) categorises each request and picks the fast or smart tier; a keyword heuristic takes over if the classifier is unavailable, so `auto` mode never blocks on the router.
- **Task-based model selection** — set `MODEL_<CATEGORY>` (e.g. `MODEL_CODING=claude-sonnet-5`, `MODEL_MATH=gemini/gemini-flash-latest`) and `auto` mode sends each task category to the model you've named best for it, across any provider. Unset categories fall back to the fast/smart tier; the tier still sets the token budget and reasoning effort.
- **Runtime-editable model map** — a **Settings** panel (and the `/v1/settings` API) lets you re-point any tier or task category to a different model live, without restarting: a saved value overrides the matching env var, and clearing it reverts to the env/default. The panel shows each category's effective model, where it came from (override / env / default), and warns when a chosen model's credential isn't set. Global map; set `ALLOW_SETTINGS_WRITE=false` to make it read-only on shared deployments.
- **Multi-provider** — any tier (`OPENAI_MODEL_FAST` / `_SMART` / `_FALLBACK`) can point at an OpenAI model, a Claude model (any name starting with `claude`), or any **LiteLLM** provider-prefixed model (`gemini/…`, `bedrock/…`, `mistral/…`, `groq/…`, and 100+ others). OpenAI goes through the native Responses API and Anthropic through the native Messages API; everything else is dispatched through LiteLLM. Set that provider's standard credential (`GEMINI_API_KEY`, `MISTRAL_API_KEY`, AWS creds for Bedrock, …). The `auto` router itself stays on OpenAI.
- **Model fallback chain** — if the primary model call fails with an API error, the orchestrator retries through `OPENAI_MODEL_FALLBACK`, then `OPENAI_MODEL_FAST`, then `OPENAI_MODEL` (duplicates and the failed model removed) and tags the result `->fallback`.
- **SSE streaming** — answers stream incrementally over `text/event-stream` with a strict `meta` / `delta` / `done` / `error` event contract.
- **Conversation persistence + auto-titling** — conversations and messages live in SQLite; the first question of a generically-titled conversation becomes its title (trimmed to 70 chars).
- **Optional auth + per-user data** — a static bearer token (`API_AUTH_TOKEN`) and/or username/password accounts with JWTs (`JWT_SECRET` + `/v1/auth/register` & `/v1/auth/login`, with a login/logout UI); either credential grants access, and both are off by default for a zero-friction local setup. When a user is logged in via JWT, their conversations are private to them; with auth off (or a static token) conversations live in a shared bucket, so existing setups are unchanged.
- **Optional rate limiting** — set `RATE_LIMIT` (e.g. `60/minute`) to throttle the ask endpoints per client IP; unset leaves them unthrottled.
- **Per-tier budgets** — separate max-output-token limits and reasoning-effort levels for the fast and smart tiers, so quick answers stay quick and hard problems get room to think.
- **Cost & token tracking** — every answer reports input/output tokens and an estimated USD cost (per built-in, overridable price list), shown per message and as a running per-conversation total in the UI — so the savings from routing cheap tasks to cheap models are visible.
- **Response caching** — an identical prompt (same mode + model config) returns instantly and for free, with no model call — not even the classifier. The cache key folds in a signature of the model map, so editing a tier/category or a routing env var auto-invalidates stale entries; TTL and max-entry eviction are configurable, cached answers are badged in the UI, and `no_cache` on a request forces a fresh answer.
- **Regenerate / switch-model** — re-run a conversation's last answer (always fresh, bypassing the cache), optionally forcing a specific model or tier instead of the routed one. The old answer is replaced in place. A forced model bypasses the classifier and the cache entirely.
- **Telemetry** — every request gets a UUID request id and elapsed-ms timing, surfaced in the response `notes` and in structured logs.
- **OpenTelemetry tracing** — set `OTEL_EXPORTER_OTLP_ENDPOINT` to export request spans (enriched with the routing decision) to any OTLP collector — SigNoz, Grafana Tempo, Jaeger, etc. Off by default, zero overhead when unset.

## Quickstart

### Backend

```bash
python -m venv venv

# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt          # runtime only
# or, for tests + linting:  pip install -r requirements-dev.txt

# Windows
copy .env.example .env
# macOS / Linux
cp .env.example .env
# then edit .env and set OPENAI_API_KEY

uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>. The Vite dev server proxies `/api/*` to the backend at `http://127.0.0.1:8000` (stripping the `/api` prefix), so no CORS setup is needed for local development.

The UI gives you a conversation sidebar (create / rename / delete), a mode picker (auto / fast / smart), live streaming answers with markdown rendering, dark mode, and an optional token field for when the backend runs with `API_AUTH_TOKEN` set.

### Or run the whole stack with Docker

```bash
cp .env.example .env   # add your OPENAI_API_KEY
docker compose up --build
```

This starts the backend (`:8000`) and an nginx-served production build of the UI at <http://localhost:5173>; nginx proxies `/api` to the backend (streaming-safe, so SSE works), so the browser stays same-origin and no CORS config is needed. The SQLite DB persists in the `orchestrator-data` volume. Backend config comes from your `.env`.

> The Docker setup (`Dockerfile`, `frontend/Dockerfile`, `frontend/nginx.conf`, `docker-compose.yml`) is provided as-is and was not built in the authoring environment — `docker compose up --build` is the intended entry point.

## Configuration

All configuration is via environment variables, loaded from `.env` (gitignored — copy `.env.example` and fill in your key).

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | — (required) | Your OpenAI API key. Validated on the first ask; if it is missing, ask calls return an empty answer with an explanatory `notes` instead of raising. Required even when answering with Claude, because the `auto` router uses an OpenAI classifier. |
| `ANTHROPIC_API_KEY` | unset | Only needed if a tier points at a Claude model. |
| `GEMINI_API_KEY` / `MISTRAL_API_KEY` / `GROQ_API_KEY` / AWS creds / … | unset | Only needed if a tier points at that LiteLLM provider (`gemini/…`, `mistral/…`, `bedrock/…`, …). Bedrock also needs `pip install boto3`. |
| `OPENAI_MODEL` | `gpt-5` | Base/default model. Used when a tier variable below is unset, and as the last entry in the failure fallback chain. |
| `OPENAI_MODEL_ROUTER` | `gpt-5-nano` | Cheap classifier used in `auto` mode to pick a tier. Keep this small — it runs on every auto request. |
| `OPENAI_MODEL_FAST` | `gpt-5-mini` | Fast tier: quick facts, chat, summaries, reformatting. |
| `OPENAI_MODEL_SMART` | `gpt-5` | Smart tier: coding, debugging, reasoning, planning, math, analysis, creative writing. |
| `OPENAI_MODEL_FALLBACK` | `gpt-5-mini` | First candidate tried when the primary model call fails. Should differ from the primary so a model-specific outage can actually fall back. |
| `FAST_MAX_OUTPUT_TOKENS` | `1500` | Output-token cap for the fast tier. Includes model reasoning tokens, so leave headroom. |
| `SMART_MAX_OUTPUT_TOKENS` | `4000` | Output-token cap for the smart tier. |
| `MODEL_PRICING` | built-in | JSON map of `{"model": [usd_per_1M_input, usd_per_1M_output]}` to override/extend the built-in (approximate) price list used for cost estimates. |
| `FAST_REASONING_EFFORT` | `low` | Reasoning effort requested from the fast-tier model. |
| `SMART_REASONING_EFFORT` | `medium` | Reasoning effort requested from the smart-tier model. |
| `MODEL_<CATEGORY>` | unset | Per-task-category model override for `auto` mode, e.g. `MODEL_CODING`, `MODEL_MATH`. When set, that category's requests go to this model (any provider); unset categories use the fast/smart tier. Categories: `quick_fact`, `casual_chat`, `summarization`, `simple_transform`, `coding`, `debugging`, `reasoning`, `planning`, `math`, `analysis`, `creative_writing`. Also editable at runtime via the Settings panel / `/v1/settings` (a saved override wins over this env var). |
| `RESPONSE_CACHE` | `true` | Cache answers so an identical prompt (same mode + model config) returns instantly with no model call. Set `false` to disable. |
| `RESPONSE_CACHE_TTL_SECONDS` | `0` | Cache entry lifetime; `0` means entries never expire. |
| `RESPONSE_CACHE_MAX_ENTRIES` | `1000` | Cap on stored entries before the least-recently-used are evicted (`0` = unbounded). |
| `OPENAI_TIMEOUT_SECONDS` | `120` | Timeout for answer-model calls (the router classifier uses its own short internal timeout). |
| `API_AUTH_TOKEN` | unset | Static bearer token; when set, every `/v1` endpoint requires `Authorization: Bearer <token>` except `/v1/status`, `/v1/auth/register`, and `/v1/auth/login` (`/v1/auth/me` *is* protected). |
| `JWT_SECRET` | unset | Enables username/password accounts (`/v1/auth/register`, `/v1/auth/login`); JWTs it issues are accepted on protected endpoints. Unset = no JWT auth. |
| `JWT_EXPIRE_MINUTES` | `60` | Access-token lifetime in minutes. |
| `ALLOW_REGISTRATION` | `true` | Set `false` to disable `/v1/auth/register`. |
| `ALLOW_SETTINGS_WRITE` | `true` | Set `false` to make the `/v1/settings` map read-only (writes return `403`); the map is global, so lock it down on shared deployments. |
| `ALLOWED_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Comma-separated CORS origins, for serving the UI from somewhere other than the Vite proxy. |
| `RATE_LIMIT` | unset | Per-client-IP limit on the ask endpoints (slowapi syntax, e.g. `60/minute`). Unset = no rate limiting. |
| `TRUST_PROXY_HEADERS` | `false` | Set `true` only behind a trusted proxy that sets `X-Forwarded-For` (e.g. the compose nginx), so rate limits key on the real client IP. Unsafe if the backend is directly reachable. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP/HTTP endpoint for OpenTelemetry traces. Unset = tracing disabled. |
| `OTEL_SERVICE_NAME` | `ai-orchestrator` | Service name attached to exported traces. |
| `DATABASE_PATH` | `ai_orchestrator.db` | SQLite database file path. |

**The tiers must point at genuinely different models.** If `OPENAI_MODEL_FAST` and `OPENAI_MODEL_SMART` resolve to the same model, routing degenerates into a no-op that still pays for a classifier call on every auto request — all cost, no benefit. The same logic applies to `OPENAI_MODEL_FALLBACK`: a fallback identical to the primary cannot rescue a model-specific outage.

## API reference

Base URL: `http://127.0.0.1:8000` (or `/api` through the Vite proxy). When auth is enabled, send `Authorization: Bearer <token>` on every `/v1` endpoint except `/v1/status`, `/v1/auth/register`, and `/v1/auth/login`; `/` and `/health` are always open.

### Service

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `GET` | `/` | — | `{"status": "ok", "service": "ai-orchestrator"}` |
| `GET` | `/health` | — | `{"status": "ok"}` |
| `GET` | `/v1/status` | — | `{"status": "ok", "service": "ai-orchestrator", "version": "0.1.0", "auth_enabled": bool, "jwt_enabled": bool, "registration_allowed": bool, "models": {"router": str, "fast": str, "smart": str, "fallback": str}}` (never requires auth; `models` reflects the **effective** tier models — a saved override wins over the env var — and never includes the API key) |

### Auth (active only when `JWT_SECRET` is set)

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `POST` | `/v1/auth/register` | `{"username": str, "password": str}` | `201` `{"id": int, "username": str, "created_at": str}`; `409` if taken, `403` if registration disabled, `400` if JWT auth off |
| `POST` | `/v1/auth/login` | `{"username": str, "password": str}` | `{"access_token": str, "token_type": "bearer"}`; `401` on bad credentials |
| `GET` | `/v1/auth/me` | — | `{"username": str \| null}` — the caller's identity (username when logged in via JWT, else null) |

Send the returned token as `Authorization: Bearer <access_token>` on the protected endpoints. `register`/`login` never require auth themselves. Conversations created while logged in are owned by that user and are invisible (404) to others; conversations created with auth off or a static token have no owner and are shared.

### One-shot ask

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `POST` | `/v1/ask` | `{"question": str, "mode": "auto"\|"fast"\|"smart", "no_cache": bool, "model": str\|null}` (`mode` defaults to `"auto"`, `no_cache` to `false`) | `{"answer": str, "mode_used": str, "notes": str, "input_tokens": int\|null, "output_tokens": int\|null, "cost_usd": float\|null, "cached": bool}` |

`notes` always carries the routing explanation, the request id, and elapsed milliseconds, e.g. `AI router: task=coding complexity=medium -> SMART model gpt-5 | request_id=... | ms=4211`. On unrecoverable errors (bad API key, rate limiting, exhausted fallbacks) the endpoint still returns `200` with an empty `answer` and an explanatory `notes`. `cached` is `true` when the answer was served from the response cache (then `cost_usd` is `0` and no model was called); set `no_cache: true` to force a fresh answer. Set `model` to force that exact model, bypassing routing and the cache (`mode` then only picks the token budget / reasoning effort).

### Conversations

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `GET` | `/v1/conversations` | — | `[{"id": int, "title": str, "created_at": str, "updated_at": str}, ...]` (most recently updated first) |
| `POST` | `/v1/conversations` | `{"title": str}` (defaults to `"Untitled conversation"`) | The created conversation object |
| `PATCH` | `/v1/conversations/{id}` | `{"title": str}` | The updated conversation object; `404` if not found |
| `DELETE` | `/v1/conversations/{id}` | — | `{"status": "deleted", "conversation_id": int}`; `404` if not found |
| `GET` | `/v1/conversations/{id}/messages` | — | `[{"id": int, "conversation_id": int, "role": str, "content": str, "mode_used": str\|null, "notes": str\|null, "input_tokens": int\|null, "output_tokens": int\|null, "cost_usd": float\|null, "cached": bool, "created_at": str}, ...]`; `404` if not found |
| `POST` | `/v1/conversations/{id}/ask` | Same body as `/v1/ask` | Same shape as `/v1/ask`, with `\| context_messages=N` appended to `notes`; `404` if not found |
| `POST` | `/v1/conversations/{id}/regenerate` | `{"mode": "auto"\|"fast"\|"smart", "model": str\|null}` (both optional) | Re-runs the conversation's last user question (always fresh, no cache), **replacing** the previous answer. Same response shape as `/v1/ask`; `400` if there is no user message, `404` if not found |
| `POST` | `/v1/conversations/{id}/regenerate/stream` | Same body as `/v1/conversations/{id}/regenerate` | Streaming (SSE) variant of regenerate |

A conversation ask persists the user message, builds a context prompt from the last 12 prior messages, runs the orchestrator, then persists the assistant message with its `mode_used` and `notes`. If it is the first message and the conversation still has a generic title, the question becomes the title (auto-titling).

### Streaming ask (SSE)

```
POST /v1/conversations/{id}/ask/stream
Body: {"question": str, "mode": "auto"|"fast"|"smart"}
Response: text/event-stream
```

Frames are `event: <name>\ndata: <json>\n\n`. The event sequence is:

1. `meta` — sent once, immediately after routing: `{"request_id": str, "mode_used": str, "model": str, "notes": str}`
2. `delta` — zero or more incremental answer chunks: `{"text": str}`
3. `done` — terminal on success: `{"answer": str, "mode_used": str, "notes": str}`. The assistant message is already persisted to the database before this event is emitted, so clients can refetch messages on `done`.
4. `error` — terminal on failure: `{"message": str}`. If partial text was streamed, the partial assistant message is persisted (with a note that it was interrupted) before this event; if nothing was streamed, nothing is persisted.

A `404` JSON error (not SSE) is returned if the conversation does not exist. The user message is persisted before streaming begins, and auto-titling applies exactly as in the non-streaming endpoint.

Example stream:

```
event: meta
data: {"request_id": "3f6d2c9a-6f0e-4b57-9c1e-8f2a1d4b5c6d", "mode_used": "auto->fast", "model": "gpt-5-mini", "notes": "AI router: task=quick_fact complexity=low (short factual lookup) -> FAST model gpt-5-mini"}

event: delta
data: {"text": "The speed of light in a vacuum "}

event: delta
data: {"text": "is 299,792,458 metres per second."}

event: done
data: {"answer": "The speed of light in a vacuum is 299,792,458 metres per second.", "mode_used": "auto->fast", "notes": "AI router: task=quick_fact complexity=low (short factual lookup) -> FAST model gpt-5-mini | request_id=3f6d2c9a-6f0e-4b57-9c1e-8f2a1d4b5c6d | ms=2840"}
```

### Settings (the runtime model map)

Edit the task→model map live without a restart. Only model-selection keys are settable — the five tiers (`OPENAI_MODEL`, `OPENAI_MODEL_ROUTER`, `OPENAI_MODEL_FAST`, `OPENAI_MODEL_SMART`, `OPENAI_MODEL_FALLBACK`) and the eleven `MODEL_<CATEGORY>` keys. Credential keys are **not** settable, so this API can never write or read a secret. A saved value overrides the matching env var; clearing it reverts to the env/default.

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `GET` | `/v1/settings` | — | `{"editable": bool, "tiers": [item, …], "categories": [item, …]}` where each `item` is `{"key": str, "label": str, "effective_model": str, "source": "override"\|"env"\|"default", "override": str\|null, "env": str\|null, "provider": str, "key_env": str, "key_present": bool\|null, …}` (categories also carry `category`, `tier`, `inherits`) |
| `PUT` | `/v1/settings/{key}` | `{"value": str}` | The full settings view (as `GET`). An empty `value` clears the override. `400` if `key` isn't settable or `value` is malformed; `403` if `ALLOW_SETTINGS_WRITE=false` |
| `DELETE` | `/v1/settings/{key}` | — | The full settings view, with that key's override cleared; `403` if writes are disabled |
| `POST` | `/v1/settings/reset` | — | The full settings view, with every override cleared; `403` if writes are disabled |

`key_present` is `true`/`false` when the required credential env var can be named (e.g. `GEMINI_API_KEY`), or `null` when it can't (e.g. Bedrock's AWS credentials). All four endpoints are behind the same auth as the rest of `/v1`.

### Response cache

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `GET` | `/v1/cache` | — | `{"enabled": bool, "entries": int, "ttl_seconds": int, "max_entries": int}` |
| `DELETE` | `/v1/cache` | — | `{"cleared": int, "enabled": bool, "entries": int, ...}` — empties the cache |

The cache key is a hash of the prompt, the mode, and a signature of the effective model map (tier + category models, budgets, and reasoning efforts), so any routing change auto-invalidates stale entries. Both endpoints require the same auth as the rest of `/v1`.

## Routing deep-dive

### Categories

In `auto` mode, the router model classifies each request into one category plus a complexity (`low` / `medium` / `high`) and a short reason.

| Fast tier (`FAST_CATEGORIES`) | Smart tier (`SMART_CATEGORIES`) |
| --- | --- |
| `quick_fact` — short factual lookup or definition | `coding` — write or modify code |
| `casual_chat` — greetings, small talk, opinions | `debugging` — diagnose errors or unexpected behaviour |
| `summarization` — condense or restate provided text | `reasoning` — multi-step logic, tradeoffs, deep explanation |
| `simple_transform` — reformat, translate, extract, rewrite | `planning` — designs, architectures, strategies, plans |
| | `math` — calculations, proofs, quantitative problems |
| | `analysis` — compare options, evaluate data or documents |
| | `creative_writing` — stories, poems, marketing copy |

### Decision rule

```
tier = "smart"  if category in SMART_CATEGORIES or complexity == "high"
       else "fast"
```

So even a fast-category request (say, a summarization of a dense legal document that the classifier marks `complexity: high`) escalates to the smart tier.

### Heuristic fallback

If the classifier call fails or returns unparseable output, routing falls back to keywords: the request goes **smart** if it is longer than 220 characters or contains any of:

`compare`, `tradeoff`, `design`, `architecture`, `plan`, `strategy`, `debug`, `error`, `why`, `explain`, `step-by-step`, `implement`, `refactor`, `optimize`, `security`, `threat`, `database`, `schema`

— otherwise **fast**. The `notes` field tells you which path ran (`AI router: ...` vs `Heuristic fallback selected ...`).

### `mode_used` values

| Value | Meaning |
| --- | --- |
| `fast` | Caller forced the fast tier (`"mode": "fast"`) |
| `smart` | Caller forced the smart tier (`"mode": "smart"`) |
| `auto->fast` | Auto mode; the classifier (or heuristic) chose the fast tier |
| `auto->smart` | Auto mode; the classifier (or heuristic) chose the smart tier |
| `auto->smart:coding` | Auto mode; a per-category model (`MODEL_CODING`) handled the request (the `:category` suffix names which). The tier before the colon still set the budget/effort |
| `forced:<model>` | Caller forced an exact model (`"model": "<model>"`, e.g. via regenerate / switch-model), bypassing routing and the cache |
| `...->fallback` | Suffix appended when the primary model failed with an API error and a fallback model produced the answer (e.g. `auto->smart->fallback`) |

Authentication and rate-limit errors deliberately do **not** trigger the fallback chain — a different model would fail identically — and instead return an empty answer with an explanatory `notes`.

## Testing

**Backend** (pytest):

```bash
# Windows
venv/Scripts/python.exe -m pytest tests -q

# macOS / Linux
python -m pytest tests -q
```

The suite covers routing decisions (explicit modes, classifier parsing, heuristic fallback), the model fallback chain (sync and streaming), the missing-key path, conversation persistence and auto-titling, the SSE event contract, and optional bearer auth. Tests stub the OpenAI client — no real API calls are made.

**Frontend** (Vitest + Testing Library):

```bash
cd frontend
npm test          # run once
npm run test:watch
```

Covers the SSE frame parser (chunk boundaries, CRLF, multi-line data, split frames), local-time timestamp formatting, and component flows (conversation list rendering, a streamed answer, and the bearer-token header) — no dev server or network needed.

Both suites also run in CI (`.github/workflows/ci.yml`) on every push and pull request.

**Routing accuracy eval** — `evals/` scores the `auto` router against a labeled
55-prompt dataset (5 per task category), reporting both **tier accuracy** (fast
vs smart) and **per-category classification accuracy** (`python -m evals.run`,
needs `OPENAI_API_KEY`). The scoring logic is unit-tested offline; see
[evals/README.md](evals/README.md). A recent run scored **55/55 tier (100%)** and
**49/55 category (89%)** — perfect tier routing, with `reasoning` prompts often
classified as `analysis` (both smart-tier, so routing is unaffected).

### Pre-commit hooks (optional)

```bash
pip install pre-commit
pre-commit install          # enable hooks for this repo
pre-commit run --all-files  # run them on demand
```

Configured in `.pre-commit-config.yaml`: `ruff` lint + format for `app/` and `tests/`, and `eslint` for the frontend.

## Project structure

```
ai-orchestrator/
├── app/
│   ├── main.py          # FastAPI endpoints, context prompt builder, auto-titling, SSE streaming
│   ├── orchestrator.py  # model calls (streaming + fallback chain), provider dispatch
│   ├── providers.py     # Anthropic + LiteLLM (Gemini/Bedrock/Mistral/…) calls
│   ├── usage.py         # token capture + estimated-cost pricing table
│   ├── ratelimit.py     # optional slowapi per-IP rate limiter
│   ├── routing.py       # AI classifier router + keyword heuristic fallback
│   ├── categories.py    # task-category constants (shared by routing + settings)
│   ├── settings.py      # runtime model map: DB-override > env > default resolution
│   ├── cache.py         # response cache: key = prompt + mode + model-config signature
│   ├── database.py      # sqlite3 persistence (conversations, messages, settings, cache)
│   ├── schemas.py       # Pydantic request/response models
│   ├── telemetry.py     # request ids + elapsed-ms timing
│   ├── observability.py # optional OpenTelemetry tracing (OTLP export)
│   ├── auth.py          # static-token + JWT auth guard + per-user ownership
│   └── security.py      # password hashing (bcrypt) + JWT issue/verify (jose)
├── frontend/
│   ├── src/App.tsx      # single-component React UI (streaming, markdown, dark mode, login)
│   ├── src/Settings.tsx # model-map settings modal (edit tiers + task categories)
│   ├── src/sse.ts       # incremental Server-Sent Events parser
│   ├── src/format.ts    # local-time timestamp formatting
│   ├── src/*.test.ts(x) # Vitest unit + component tests
│   ├── src/App.css
│   ├── vite.config.ts   # proxies /api/* -> http://127.0.0.1:8000
│   └── vitest.config.ts # test runner config (jsdom)
├── tests/               # pytest suite (no real API calls)
├── evals/               # routing-accuracy eval (dataset + harness + CLI)
├── Dockerfile           # backend image (uvicorn)
├── docker-compose.yml   # backend + nginx-served frontend
├── .github/workflows/   # CI: ruff, pytest, eslint, vitest, build
├── .pre-commit-config.yaml
├── .env.example         # configuration template — copy to .env
├── requirements.txt     # runtime deps
├── requirements-dev.txt # runtime + ruff/pytest/pre-commit
├── check_env.py         # quick sanity check of your environment config
└── AGENTS.md            # prompt template for constrained agent runs (see Design notes)
```

## Design notes

**Route-then-answer pays for itself.** The counterintuitive part of putting an extra model call in front of every request is that it makes the common case both cheaper *and* faster. A nano-class classifier adds well under a second and a negligible cost, but it lets simple requests skip the flagship model entirely: in local measurements, a quick factual question answered via `gpt-5-mini` completes in about 3 seconds end-to-end (classifier included), while sending the same question through full `gpt-5` reasoning takes 4.5 seconds or more — at several times the token price. Meanwhile hard tasks lose nothing: anything the classifier marks as a smart category or high complexity gets the full-quality model with the larger token budget and higher reasoning effort. The router only has to be right most of the time to win, and when it cannot run at all, the keyword heuristic keeps `auto` mode working.

**About `AGENTS.md`.** That file is a prompt template used to run constrained coding-agent sessions against this repository (scoped instructions, allowance-saving rules). It is not documentation of the application — this README is.

## License

MIT — see [LICENSE](LICENSE). © 2026 John-Paul Cremin.
