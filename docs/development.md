[← back to README](../README.md)

# Development

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

Configured in `.pre-commit-config.yaml`: `ruff` lint + format (`app/`, `tests/`, `evals/`), `mypy` type-check of `app/`, and `eslint` for the frontend. The `mypy` and `eslint` hooks run from your environment, so install the dev deps (`pip install -r requirements-dev.txt`) and the frontend deps first.

### Type checking & dependency audit

```bash
venv/Scripts/python.exe -m mypy                     # static types for app/ (config: mypy.ini)
venv/Scripts/python.exe -m pip_audit -r requirements.txt
```

`mypy` runs in a separate CI step; `pip-audit` runs as its own **Security** job so a
dependency CVE is visually distinct from a code failure.
[Dependabot](.github/dependabot.yml) opens weekly update PRs for pip, npm, and GitHub
Actions.

### End-to-end smoke test (Playwright)

```bash
cd e2e
npm install
npx playwright install --with-deps chromium   # first time only
npx playwright test
```

One straight-line pass through the seams the backend/frontend unit suites can't
reach on their own: a real Chromium browser against a **built** frontend served
through Vite's proxy (`vite preview`, not the dev server), real SSE over the
wire, and a real JWT register/login round-trip — all against a real `uvicorn`
backend process. The one non-real ingredient is the model provider: `e2e/stub_provider.py`
is a tiny stand-in for the OpenAI Responses API (both the plain-JSON and SSE-streaming
shapes, built from the `openai` SDK's own Pydantic models so the wire schema can't drift
from what the SDK expects), and the backend is pointed at it via `OPENAI_BASE_URL`
— every model tier is also force-pinned to it and every other provider's API
key is cleared, so a routing mistake can't silently fall through to a real,
billed provider. Runs as its own CI job (`.github/workflows/ci.yml`), after the
backend/frontend unit-test jobs pass, using dedicated ports (8010/4183/8999) so
it never collides with a `npm run dev` instance you might already have running
locally.

## Database migrations

`app/database.py`'s original schema setup — `CREATE TABLE IF NOT EXISTS` plus a
block of `PRAGMA table_info` + conditional `ALTER TABLE ADD COLUMN` checks — is
idempotent and additive-only, and still runs on every `init_db()` call exactly
as it always has. It only ever *adds a nullable column*, though: it has no way
to rename or drop a column, change a type, or backfill data, and no way to run
a step exactly once with a recorded before/after.

Anything beyond a plain additive column now goes through a small versioned
migration system instead, tracked via SQLite's own `PRAGMA user_version` (an
integer in the database file's header — no separate tracking table needed):

- Each migration is a numbered, ordered, run-once step: `_migration_NNN_description(conn)`
  plus an entry in `_MIGRATIONS`, one version number higher than the last.
- On startup, `init_db()` compares the database's `user_version` against the
  highest defined migration and applies whatever's missing, in order — each
  step committed (and its version recorded) individually, so a later step
  failing can't silently un-record one that already succeeded.
- Before applying anything, it takes a real on-disk backup
  (`<DATABASE_PATH>.bak-v<from_version>-<UTC timestamp>`) — but only when
  there's actually pending work, never on a normal startup where nothing
  changes. The first migration ever run against an existing database backs up
  the exact pre-migration state, so a bad migration is always recoverable by
  restoring that file.
- A brand-new database gets every migration applied on its very first
  `init_db()` call (there's no reason to replay history for a fresh install —
  the baseline `CREATE TABLE` statements already reflect the current schema).

The first migration (`idx_conversations_owner`/`idx_templates_owner`) is a real
fix, not just a demonstration: both tables are filtered `WHERE owner = ?` on
nearly every list/search query and neither had a supporting index.

## Project structure

```
ai-orchestrator/
├── app/
│   ├── main.py          # FastAPI app assembly: lifespan, CORS, router registration
│   ├── routers/          # APIRouter modules, split by domain (see below)
│   │   ├── deps.py       # shared router instances + cross-domain helpers
│   │   ├── system.py     # /, /health, /v1/status (unauthenticated)
│   │   ├── auth.py       # register/login/logout/refresh/me
│   │   ├── conversations.py # conversation CRUD, search, summarize
│   │   ├── messages.py   # message CRUD + ask/regenerate/edit/continue/streaming, context prompt builder
│   │   ├── ask.py        # stateless /v1/ask, /v1/compare, /v1/estimate
│   │   ├── compat.py     # OpenAI-compatible /v1/chat/completions
│   │   ├── settings.py   # settings + cache/semantic-cache/model-catalog admin
│   │   ├── media.py      # /v1/transcribe, /v1/speak
│   │   ├── templates.py  # saved prompt templates
│   │   └── usage.py      # /v1/usage
│   ├── orchestrator.py  # run_orchestrator/stream_orchestrator: the two top-level entry points
│   ├── orchestrator_calls.py # provider-dispatch chain: get_client, _call_model/_stream_model, fallback
│   ├── orchestrator_extract.py # post-process a raw provider response (text/citations/images/code)
│   ├── orchestrator_tools.py # web-search/action/image-gen/code-exec tool definitions
│   ├── orchestrator_cache.py # exact + semantic response-cache hit/miss helpers
│   ├── orchestrator_spend.py # best-effort spend + avoided-cost logging
│   ├── orchestrator_summarize.py # conversation history summarization
│   ├── context_summary.py # folds older conversation turns into a memory summary
│   ├── context_builder.py # assembles the ask/regenerate/edit prompt: system+summary+history+question
│   ├── ask_support.py   # small ask/regenerate helpers: title generation, model-pin resolution, memory recall
│   ├── providers.py     # Anthropic + LiteLLM (Gemini/Bedrock/Mistral/…) calls
│   ├── usage.py         # token capture + estimated-cost pricing table
│   ├── budget.py        # daily spend cap (kill-switch) over the spend log
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
│   ├── security.py      # password hashing (bcrypt) + JWT issue/verify (PyJWT)
│   └── revocation.py    # in-memory JWT revocation list (logout)
├── frontend/
│   ├── src/App.tsx      # top-level state/handlers + shell layout, composes the components below
│   ├── src/Sidebar.tsx  # conversation list: search, tags, filters, bulk actions
│   ├── src/MessageList.tsx # message rendering: markdown, sources, code results, streaming state
│   ├── src/Composer.tsx # question input: auto-growing textarea, attach/mic/research icons, ask/stop
│   ├── src/HeaderOverflowMenu.tsx # the chat header's "⋯ More actions" menu (focus-trapped, Escape-closable)
│   ├── src/Button.tsx   # shared button component: two sizes, icon-only variant, used app-wide
│   ├── src/types.ts     # shared TS types (Conversation, Message, StreamState, ...)
│   ├── src/Settings.tsx # model-map settings modal (edit tiers + task categories)
│   ├── src/useTheme.ts  # light/dark/system theme hook (state + localStorage + document attribute)
│   ├── src/useNotificationPreferences.ts # background-reply notification/sound toggle hook
│   ├── src/useModalFocus.ts # modal focus-trap hook
│   ├── src/sse.ts       # incremental Server-Sent Events parser
│   ├── src/format.ts    # local-time timestamp formatting
│   ├── src/*.test.ts(x) # Vitest unit + component tests
│   ├── src/App.css
│   ├── vite.config.ts   # proxies /api/* -> http://127.0.0.1:8000
│   └── vitest.config.ts # test runner config (jsdom)
├── tests/               # pytest suite (no real API calls)
├── e2e/                 # Playwright smoke test (real browser, real HTTP/SSE, stubbed provider)
├── evals/               # routing-accuracy eval (dataset + harness + CLI)
├── Dockerfile           # backend image (uvicorn)
├── docker-compose.yml   # backend + nginx-served frontend
├── .github/workflows/   # CI: ruff, mypy, pytest, pip-audit, eslint, vitest, build
├── .github/dependabot.yml # weekly dependency-update PRs (pip, npm, actions)
├── .pre-commit-config.yaml
├── mypy.ini             # static type-check config (targets app/)
├── .env.example         # configuration template — copy to .env
├── requirements.txt     # runtime deps
├── requirements-dev.txt # runtime + ruff/mypy/pytest/pip-audit/pre-commit
├── check_env.py         # quick sanity check of your environment config
└── AGENTS.md            # prompt template for constrained agent runs (see Design notes)
```

## Design notes

**Route-then-answer pays for itself.** The counterintuitive part of putting an extra model call in front of every request is that it makes the common case both cheaper *and* faster. A nano-class classifier adds well under a second and a negligible cost, but it lets simple requests skip the flagship model entirely: in local measurements, a quick factual question answered via `gpt-5-mini` completes in about 3 seconds end-to-end (classifier included), while sending the same question through full `gpt-5` reasoning takes 4.5 seconds or more — at several times the token price. Meanwhile hard tasks lose nothing: anything the classifier marks as a smart category or high complexity gets the full-quality model with the larger token budget and higher reasoning effort. The router only has to be right most of the time to win, and when it cannot run at all, the keyword heuristic keeps `auto` mode working.

**About `AGENTS.md`.** That file is a prompt template used to run constrained coding-agent sessions against this repository (scoped instructions, allowance-saving rules). It is not documentation of the application — this README is.

**Accessibility conventions.** Every modal (Settings/Usage/Compare/Shortcuts help) traps focus and restores it on close via the shared `useModalFocus` hook — a new modal should call it too, not reinvent focus handling. Error messages inside those modals use `role="alert"` so a screen reader announces them the moment they appear, without the user needing to already be focused on that text. Icon-only buttons always carry an explicit `aria-label`, and a heading (`<h1>`–`<h3>`) is used for section titles that aren't actually labeling a specific form field, rather than an unassociated `<label>`.

**Mobile layout.** Below 850px the sidebar and chat panel stack into one column; the sidebar caps at 45vh with its own scroll so a long conversation list doesn't push the actual conversation an arbitrary distance down the page, and the composer's textarea is reordered ahead of the attach/mic buttons so typing is the first thing you reach rather than the last. Below 640px, compact utility buttons (theme/notify/favorite/bookmark/etc.) grow to a ~40–44px touch target. Message text uses `overflow-wrap: anywhere` so a long unbroken URL or token can't force horizontal page scroll — code blocks are unaffected, they get their own horizontal scrollbar instead.

**Frontend performance.** The six modal panels (Settings/Usage/Compare/Bookmarks/Templates/Summarize) are `React.lazy`-loaded behind a shared `Suspense` boundary instead of bundled into the initial `index.js` — none is needed for the first paint of the chat view, so splitting them out shrank the main bundle by ~7% (measured: 458KB → 426KB pre-gzip) with each panel's own small chunk (2–10KB) fetched only the first time it's opened. `ShortcutsHelp` stays eagerly bundled — small enough that splitting it wouldn't move the needle, and its "?"-triggered reference popup should feel instant, not wait on a chunk fetch. Message bubbles get `content-visibility: auto` (+ `contain-intrinsic-size` to keep the scrollbar stable): the browser skips layout/paint for a message once it's scrolled well off-screen, without removing it from the DOM — unlike a JS windowing library, nothing becomes unreachable to Ctrl+F, a screen reader, or this app's own in-conversation Find, and unsupported browsers just ignore the property and render normally. `/v1/compare` dispatches its 2–4 models concurrently (a small `ThreadPoolExecutor`, not sequentially) — safe because `budget.reserve()`'s SQLite reservation (`BEGIN IMMEDIATE`) already serializes concurrent spend checks correctly; that same atomicity is what already lets ordinary concurrent requests to this app coexist safely.

## Releasing

This project uses [Semantic Versioning](https://semver.org/) and a
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/)-formatted
[CHANGELOG.md](../CHANGELOG.md) at the repo root:

1. Move `CHANGELOG.md`'s `## [Unreleased]` entries under a new
   `## [X.Y.Z] - YYYY-MM-DD` heading (leave `[Unreleased]` empty above it,
   ready for whatever comes next).
2. Bump the version in both places it's declared — `app/main.py`'s
   `FastAPI(version=...)` and `frontend/package.json`'s `"version"` — to the
   same value. They're released together, so they stay in lockstep rather
   than drifting into two independently-versioned halves of one app.
3. Commit, then tag: `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`.

Nothing here currently reads or depends on the version string (no
version-gated API behavior) — it exists purely so a running instance's
`/v1/status` response and a git tag/CHANGELOG entry can be correlated when
someone asks "what version is this?" or "what changed since I last pulled?"
