[← back to README](../README.md)

## API reference

Base URL: `http://127.0.0.1:8000` (or `/api` through the Vite proxy). When auth is enabled, send `Authorization: Bearer <token>` on every `/v1` endpoint except `/v1/status`, `/v1/auth/register`, `/v1/auth/login`, and `/v1/shared/{token}`; `/` and `/health` are always open.

### Service

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `GET` | `/` | — | `{"status": "ok", "service": "ai-orchestrator"}` |
| `GET` | `/health` | — | `{"status": "ok"}` |
| `GET` | `/v1/status` | — | `{"status": "ok", "service": "ai-orchestrator", "version": "0.1.0", "auth_enabled": bool, "jwt_enabled": bool, "registration_allowed": bool, "models": {"router": str, "budget": str, "fast": str, "smart": str, "fallback": str}, "budget": {"enabled": bool, ...}}` (never requires auth; `models` reflects the **effective** tier models — a saved override wins over the env var — and never includes the API key; `models.budget` is `""` when the budget tier is unconfigured, distinct from the top-level `budget` object, which is the spend-cap status and reports only `{"enabled": bool}` — live spend figures are withheld from this public endpoint) |

### Security headers

Every response from this backend carries `Content-Security-Policy: default-src 'none'` (exempted on `/docs`/`/redoc`/`/openapi.json`), `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, and `X-Content-Type-Options: nosniff` (`app/security_headers.py`). `GET /v1/shared/{token}` additionally carries `X-Robots-Tag: noindex`, on both a `200` and a `404`.

### Auth (active only when `JWT_SECRET` is set)

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `POST` | `/v1/auth/register` | `{"username": str, "password": str}` | `201` `{"id": int, "username": str, "created_at": str}`; `409` if taken, `403` if registration disabled, `400` if JWT auth off |
| `POST` | `/v1/auth/login` | `{"username": str, "password": str}` | `{"access_token": str, "token_type": "bearer", "must_change_password": bool}`; `401` on bad credentials or a deactivated account |
| `POST` | `/v1/auth/logout` | — (send the token as `Authorization: Bearer <token>`) | Logs the user out **everywhere** — bumps their session epoch so *all* of their tokens (including any refreshed onto a fresh id) stop working; `200` `{"status": "logged_out"}`, `401` if the token is missing/invalid, `400` if JWT auth is off |
| `POST` | `/v1/auth/refresh` | — (send the token) | Trades a still-valid token for a fresh one, **rotating** it (the presented token is revoked, so a leaked token can't be replayed after a refresh); `{"access_token": str, "token_type": "bearer"}`, `401` if the token is expired/revoked |
| `GET` | `/v1/auth/me` | — | `{"username": str \| null, "is_admin": bool, "must_change_password": bool}` — the caller's identity (username when logged in via JWT, else null), whether it's in `ADMIN_USERNAMES`, and whether it must still set its own password (see `change-password` below) |
| `POST` | `/v1/auth/change-password` | `{"current_password": str, "new_password": str}` (`new_password` 8–128 chars) | Sets a new password and clears `must_change_password` — the step an admin-created/reset account is steered into before anything else, but also the ordinary "change my password" path for any account; `{"username": str, "is_admin": bool, "must_change_password": false}`, `401` if `current_password` is wrong, `400` if not logged in |

Send the returned token as `Authorization: Bearer <access_token>` on the protected endpoints. `register`/`login` never require auth themselves. Conversations created while logged in are owned by that user and are invisible (404) to others; conversations created with auth off or a static token have no owner and are shared.

### Admin user management (active only when `ADMIN_USERNAMES` is non-empty)

Every endpoint below requires an admin account (a JWT subject in `ADMIN_USERNAMES`) regardless of `ALLOW_REGISTRATION` — `403` otherwise, for both an anonymous caller too (no bearer token at all fails the ordinary API-auth gate with `401` first, same as every other `/v1` endpoint) and a logged-in non-admin. With `ADMIN_USERNAMES` empty, every endpoint here `403`s unconditionally — the feature simply isn't usable until an operator opts in. See `app/routers/users.py`.

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `GET` | `/v1/users` | — | `[{"id": int, "username": str, "created_at": str, "is_active": bool, "must_change_password": bool, "last_login_at": str \| null}, ...]`, newest first |
| `POST` | `/v1/users` | `{"username": str}` (3–64 chars) | `201` `{"user": {...same shape as GET...}, "temporary_password": str}` — the temporary password is returned **exactly once**, here, and never logged; the account is flagged `must_change_password`; `409` if the username is taken |
| `POST` | `/v1/users/{username}/reset-password` | — | `{"temporary_password": str}` — same one-time, never-logged contract as create; flags `must_change_password` and revokes the account's outstanding sessions immediately; `404` if the username doesn't exist |
| `POST` | `/v1/users/{username}/deactivate` | — | The updated user row (same shape as `GET`, `is_active: false`) — the account can no longer sign in and its outstanding sessions are revoked immediately, but its conversations are untouched and reappear as-is on reactivation; `404` if the username doesn't exist |
| `POST` | `/v1/users/{username}/reactivate` | — | The updated user row (`is_active: true`); `404` if the username doesn't exist |

### Shared conversation view (public, no auth)

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `GET` | `/v1/shared/{token}` | — | `{"title": str, "created_at": str, "messages": [{"role": str, "content": str, "created_at": str, "images": [str]\|null, "files": [{"filename": str, "data": str}]\|null, "sources": [{"title": str, "url": str}]\|null, "code_results": [...]\|null, "fact_checks": [...]\|null, "academic_results": [...]\|null, "math_results": [...]\|null}, ...]}` — never requires auth, even when `API_AUTH_TOKEN`/`JWT_SECRET` is set: this is the read-only view a share link (see `POST .../share` below) resolves to. Deliberately narrower than a normal message object — no `cost_usd`/tokens/`mode_used`/`notes`/`pending_action`/`action_status`/`bookmarked`/`model`/`feedback`/`feedback_reason`/`library_sources`/`workflow_steps`. `404` for an unknown OR expired token (identical response either way). Rate-limited via the always-on `auth_limiter`, same as the auth endpoints above. Token is `secrets.token_urlsafe(32)` (256 bits); the response carries `X-Robots-Tag: noindex` (see Security headers below). |

### One-shot ask

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `POST` | `/v1/ask` | `{"question": str, "mode": "auto"\|"budget"\|"fast"\|"smart", "no_cache": bool, "model": str\|null, "images": [str]\|null, "files": [{"filename": str, "data": str}]\|null, "research": bool}` (`mode` defaults to `"auto"`, `no_cache` and `research` to `false`) | `{"answer": str, "mode_used": str, "notes": str, "input_tokens": int\|null, "output_tokens": int\|null, "cost_usd": float\|null, "cached": bool, "sources": [{"title": str, "url": str}]\|null, "pending_action": {"action": str, "summary": str, "payload": object}\|null, "images": [str]\|null, "code_results": [{"code": str, "logs": str\|null, "images": [str]\|null}]\|null, "truncated": bool}` |
| `POST` | `/v1/conversations/{id}/messages/{message_id}/continue` | — | Resumes a truncated assistant message exactly where it left off and appends the result to it — tokens/cost accumulate on top of what was already recorded. Returns the updated message (same shape as `GET .../messages`); `404` if the conversation/message isn't found, `400` if `message_id` isn't an assistant message or wasn't actually truncated, `502` if the continuation itself came back empty |
| `POST` | `/v1/compare` | `{"question": str, "models": [str, ...]}` (2–4 distinct, validated model names) | `{"question": str, "results": [{"model": str, "answer": str, "mode_used": str, "notes": str, "input_tokens": int\|null, "output_tokens": int\|null, "cost_usd": float\|null, "elapsed_ms": int}, ...]}`, one result per requested model, in the order given; `422` on validation failure |
| `POST` | `/v1/estimate` | `{"question": str, "mode": "auto"\|"budget"\|"fast"\|"smart"}` (`mode` defaults to `"auto"`) | `{"model": str, "mode_used": str, "input_tokens_estimate": int, "output_tokens_estimate": int, "cost_usd_estimate": float\|null}` — a worst-case token/cost preview for a question **before sending it**, computed for free (routes with no classifier call, even in `auto` mode — see Live cost preview below); never calls a model, never persists anything, `422` on validation failure |
| `POST` | `/v1/transcribe` | `{"audio": str}` — a `data:audio/{webm,wav,mp3,mpeg,mp4,m4a,ogg};base64,...` URL | `{"text": str}`; `502` if the provider call fails, `422` if `audio` fails validation |
| `POST` | `/v1/speak` | `{"text": str}` (1–50,000 chars) | Raw `audio/mpeg` bytes (not JSON) for the client to play directly; `502` if the provider call fails, `422` if `text` is empty/oversized |

`notes` always carries the routing explanation, the request id, and elapsed milliseconds, e.g. `AI router: task=coding complexity=medium -> SMART model gpt-5 | request_id=... | ms=4211`. On unrecoverable errors (bad API key, rate limiting with no cross-provider fallback configured, exhausted fallbacks) the endpoint still returns `200` with an empty `answer` and an explanatory `notes`. `cached` is `true` when the answer was served from the response cache (then `cost_usd` is `0` and no model was called); set `no_cache: true` to force a fresh answer. Set `model` to force that exact model, bypassing routing and the cache (`mode` then only picks the token budget / reasoning effort). `images` on the *request* is vision input — up to 4 `data:image/{png,jpeg,gif,webp};base64,...` URLs; `files` is document input — up to 4 `{"filename", "data"}` objects, `data` a `data:{application/pdf,text/plain};base64,...` URL (see Image input / vision and Document input below). A request-side image or file always disables the response cache for that call.

### OpenAI-compatible chat completions

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `POST` | `/v1/chat/completions` | `{"model": str, "messages": [{"role": "system"\|"user"\|"assistant", "content": str}, ...], "stream": bool}` (`model` defaults to `"auto"`, `stream` to `false`; the last message must have `role: "user"`) | Non-streaming: `{"id": str, "object": "chat.completion", "created": int, "model": str, "choices": [{"index": 0, "message": {"role": "assistant", "content": str}, "finish_reason": "stop"\|"length"}], "usage": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}}`. Streaming (`stream: true`): standard OpenAI `chat.completion.chunk` SSE frames, terminated by `data: [DONE]`. `422` if the last message isn't from the user, `400` if `model` isn't a mode keyword and fails model-name validation |

Point any tool built against the OpenAI SDK/wire format (LangChain, an IDE plugin, a `curl` script) at this endpoint and it inherits this app's routing, caching, and daily-budget behavior instead of talking to OpenAI directly — set `base_url` to this server's `/v1` and any API key (it's ignored; this app's own auth applies instead, same as every other endpoint). `model` is either a routing-mode keyword (`auto`/`budget`/`fast`/`smart`, same meaning as `/v1/ask`'s `mode`) or any other value, which forces that exact model — bypassing routing and the cache — exactly like a conversation's model pin. Stateless like `/v1/ask`: nothing is persisted, and the full message history must be resent every call (no conversation id, no server-side memory) — the `messages` array is folded into the same context-prompt/history-summarization machinery a saved conversation's turns go through, system messages becoming the instructions block.

### Conversations

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `GET` | `/v1/conversations?include_archived=bool` | — | `[{"id": int, "title": str, "pinned_model": str\|null, "system_prompt": str\|null, "favorite": bool, "archived": bool, "tags": [str], "created_at": str, "updated_at": str, "message_count": int}, ...]` (favorited conversations first, most recently updated first within each group; archived conversations are excluded unless `include_archived=true`, default `false`; `message_count` is only populated here, other conversation-returning endpoints default it to `0`) |
| `GET` | `/v1/search?q=str` | — | `[{"id": int, "title": str, "pinned_model": str\|null, "created_at": str, "updated_at": str, "snippet": str}, ...]` (conversations whose title or any message content matches, most recently updated first; `snippet` is the matched message text, or the title itself for a title-only match; `q` is 1–200 chars, `422` if empty) |
| `GET` | `/v1/bookmarks` | — | `[message, ...]` where each `message` is a full message object (as returned in `GET /v1/conversations/{id}/messages`) plus `"conversation_title": str` — every bookmarked message across this owner's conversations, newest first |
| `GET` | `/v1/templates` | — | `[{"id": int, "name": str, "content": str, "created_at": str, "updated_at": str}, ...]` — this owner's saved prompt templates, most-recently-updated first |
| `POST` | `/v1/templates` | `{"name": str, "content": str}` (name 1-80 chars, content 1-4,000 chars) | The created template; `422` on validation failure |
| `PATCH` | `/v1/templates/{id}` | `{"name": str\|null, "content": str\|null}` (at least one required) | Updates whichever field(s) are given. The updated template; `400` if neither is given, `404` if not found/not owned |
| `DELETE` | `/v1/templates/{id}` | — | `{"status": "deleted", "template_id": int}`; `404` if not found/not owned |
| `GET` | `/v1/usage?days=int` | — | `{"today_usd": float, "days": int, "by_model": [{"model": str, "calls": int, "input_tokens": int, "output_tokens": int, "cost_usd": float \| null}, ...], "by_day": [{"date": str, "cost_usd": float, "tokens": int}, ...], "daily_budget_usd": float \| null, "daily_budget_per_owner_usd": float \| null, "owner_remaining_usd": float \| null, "avoided_cost_today_usd": float, "tokens_per_dollar": float \| null, "window_tokens": int}` — this caller's own spend, sourced from the same `spend_log` table as the daily budget cap; `by_day` covers every day in the window (zero-filled, oldest first) and `by_model` is sorted by cost descending; `days` defaults to 14, range 1–90. A `by_model` row's `cost_usd` is `null` (shown as "Unknown" in the Usage panel) when that model has no known cost at all — an unpriced model (not in `MODEL_PRICING`) — never conflated with a genuinely free one (e.g. local Ollama), which reports `0`. The budget fields report the *configured* cap(s), null when unset; `owner_remaining_usd` is this caller's own per-owner cap minus `today_usd` (floored at 0), null when `DAILY_BUDGET_PER_OWNER_USD` isn't set — never the live global spend total, which stays private to the operator. `avoided_cost_today_usd` is this caller's own total from `avoided_cost_log` today (see Avoided-cost tracking) — always `0.0`, never null. `tokens_per_dollar` is `window_tokens` divided by the window's total spend — the efficiency KPI (see The efficiency KPI above) — `null` when the window spent nothing (no usage at all, or every call was free; `window_tokens` tells those two apart). `by_model`/`by_day` are unioned with `spend_rollup` (see Data retention + DB maintenance in docs/features.md) — a no-op merge unless `RETENTION_DAYS_DETAIL` has actually pruned something out of the requested window. |
| `GET` | `/v1/feedback/summary?days=int` | — | `{"by_model": {model: stat, ...}, "by_category": {category: stat, ...}, "by_lane": {lane: stat, ...}}` where each `stat` is `{"answers_rated": int, "up": int, "down": int, "down_rate": float}` — this caller's own 👍/👎 aggregates from `feedback_log` over the window (`days` defaults to 14, range 1–90, same convention as `/v1/usage`). `lane` is one of `"free"`/`"budget"`/`"fast"`/`"smart"`/`"forced"`, derived from each rated answer's `mode_used`. A model/category with no ratings in the window is simply absent from that dict, not zeroed. Feeds the Usage panel's Quality section. `by_model` is additionally unioned with `feedback_rollup` for continuity past the retention boundary; `by_category`/`by_lane` are not (that rollup is grouped by model only — see Data retention + DB maintenance in docs/features.md). |
| `POST` | `/v1/conversations` | `{"title": str}` (defaults to `"Untitled conversation"`) | The created conversation object |
| `POST` | `/v1/conversations/import` | `{"title": str, "pinned_model": str\|null, "system_prompt": str\|null, "favorite": bool, "tags": [str], "messages": [{"role": "user"\|"assistant", "content": str, "mode_used": str\|null, "notes": str\|null, "input_tokens": int\|null, "output_tokens": int\|null, "cost_usd": float\|null, "cached": bool, "sources": [{"title": str, "url": str}]\|null, "truncated": bool, "code_results": [{"code": str, "logs": str\|null, "images": [str]\|null}]\|null, "images": [str]\|null, "files": [{"filename": str, "data": str}]\|null}, ...]}` (`title` defaults to `"Imported conversation"`; `favorite` defaults to `false`; `tags` defaults to `[]`, same normalization as `PUT .../tags`; 1–500 messages, each content 1–100,000 chars; `pinned_model` validated the same as `PUT .../pin`; `images`/`files` validated the same as `AskRequest`'s — max 4 images/4 files per message, same size caps and `data:` URL/mime requirements) | Re-creates a conversation from these messages, in order, with fresh ids and no model calls — restores the pin, instructions, favorite status, tags, and per-message tokens/cost/cached/sources/truncated/code_results/images/files. The created conversation object; `422` on validation failure (including an attachment that fails the same checks a freshly-attached one would) |
| `PATCH` | `/v1/conversations/{id}` | `{"title": str}` | The updated conversation object; `404` if not found |
| `PUT` | `/v1/conversations/{id}/pin` | `{"model": str}` | Pin a model (or `"budget"`/`"fast"`/`"smart"` tier) to the conversation so every new question uses it; empty string clears the pin. Returns the updated conversation; `404` if not found, `422` if the model name is malformed |
| `PUT` | `/v1/conversations/{id}/system_prompt` | `{"system_prompt": str}` (max 4,000 chars) | Set this conversation's custom instructions, prepended to every question asked in it (ask, regenerate, edit — from the very first message); empty string clears them. Returns the updated conversation; `404` if not found, `422` if over the length limit |
| `PUT` | `/v1/conversations/{id}/favorite` | `{"favorite": bool}` | Star (or unstar) the conversation, pinning it to the top of the sidebar list ahead of unfavorited ones (both groups still sorted by recency). Doesn't touch `updated_at`. Returns the updated conversation; `404` if not found |
| `PUT` | `/v1/conversations/{id}/archive` | `{"archived": bool}` | Archive (or restore) the conversation, hiding it from the default `GET /v1/conversations` list without deleting it. Doesn't touch `updated_at`; the conversation stays fully reachable while archived. Returns the updated conversation; `404` if not found |
| `PUT` | `/v1/conversations/{id}/tags` | `{"tags": [str]}` | Replaces the conversation's tags wholesale (trimmed, deduped, capped at 15 tags of 30 chars each). Doesn't touch `updated_at`. Returns the updated conversation; `404` if not found, `422` if over the tag-count cap |
| `POST` | `/v1/conversations/{id}/duplicate` | — | Copies the conversation (title, pin, instructions, favorite status, tags, every message with full fidelity) into a brand-new one owned by the caller; a pending action is not carried over, and archived status is deliberately reset to unarchived. The created conversation object; `404` if not found |
| `POST` | `/v1/conversations/{id}/messages/{message_id}/branch` | — | Branches a new conversation from this one, copying title (suffixed " (branch)"), pin, instructions, and tags, but only the messages up to and including `message_id` (fresh ids, no model calls). The created conversation object; `404` if the conversation isn't found, or if `message_id` doesn't belong to it |
| `POST` | `/v1/conversations/{id}/summarize` | — | A short, on-demand TL;DR (key topics, decisions, open questions) via one cheap router-model call; never persisted. `{"summary": str}`; `404` if not found, `400` if the conversation has no messages, `502` if the model call fails/returns empty |
| `GET` | `/v1/conversations/{id}/share` | — | `{"active": bool, "token": str\|null, "expires_at": str\|null}` — whether this conversation currently has a live share link, without creating or changing anything. `404` if not found |
| `POST` | `/v1/conversations/{id}/share` | `{"ttl_hours": int\|null}` (1–8760, default `null` = never expires) | (Re-)generates the conversation's share link — any previously issued link for it stops working immediately. Same response shape as `GET .../share`; `404` if not found, `422` if `ttl_hours` is out of range |
| `DELETE` | `/v1/conversations/{id}/share` | — | Revokes the conversation's share link, if any (a no-op, not an error, if there wasn't one). `{"active": false, "token": null, "expires_at": null}`; `404` if not found |
| `DELETE` | `/v1/conversations/{id}` | — | `{"status": "deleted", "conversation_id": int}`; `404` if not found |
| `GET` | `/v1/conversations/{id}/messages` | — | `[{"id": int, "conversation_id": int, "role": str, "content": str, "mode_used": str\|null, "notes": str\|null, "input_tokens": int\|null, "output_tokens": int\|null, "cost_usd": float\|null, "cached": bool, "sources": [{"title": str, "url": str}]\|null, "pending_action": {"action": str, "summary": str, "payload": object}\|null, "action_status": "pending"\|"confirmed"\|"declined"\|"failed"\|null, "images": [str]\|null, "files": [{"filename": str, "data": str}]\|null, "audio": [{"filename": str, "duration_seconds": float\|null}]\|null, "bookmarked": bool, "truncated": bool, "code_results": [{"code": str, "logs": str\|null, "images": [str]\|null}]\|null, "model": str\|null, "feedback": 1\|-1\|null, "feedback_reason": str\|null, "created_at": str}, ...]`; `404` if not found |
| `DELETE` | `/v1/conversations/{id}/messages/{message_id}` | — | Deletes exactly that one message (either role) — nothing else in the conversation is touched. Distinct from regenerate/edit, which both replace or discard a range and produce a fresh answer. `{"status": "deleted", "message_id": int}`; `404` if the conversation/message isn't found |
| `POST` | `/v1/conversations/{id}/messages/restore` | Same shape as one `ImportMessage` (`role`, `content`, `mode_used`, `notes`, `input_tokens`, `output_tokens`, `cost_usd`, `cached`, `sources`, `truncated`, `code_results`, `images`, `files`, `model`, `feedback`, `feedback_reason`) | Recreates one message (fresh id, no model call) in this conversation — the backing endpoint for Undo after deleting a message. The created `MessageOut`; `404` if the conversation isn't found, `422` on validation failure |
| `PUT` | `/v1/conversations/{id}/messages/{message_id}/bookmark` | `{"bookmarked": bool}` | Bookmarks/unbookmarks one message — a marker on a single turn, distinct from favoriting the whole conversation. Doesn't touch the conversation's `updated_at`. Returns the updated message; `404` if the conversation/message isn't found |
| `PUT` | `/v1/conversations/{id}/messages/{message_id}/feedback` | `{"verdict": "up"\|"down"\|null, "reason": str\|null}` (`reason` ≤200 chars) | Rates/clears a caller's 👍/👎 on one assistant answer (see app/feedback.py). Setting the SAME verdict already recorded clears it instead — the same click-again-to-clear contract as the bookmark toggle; an explicit `null` verdict always clears. A pure marker — doesn't touch the conversation's `updated_at` — but every set/change/clear also appends a snapshot row to the `feedback_log` ledger (model/category/mode_used at rating time), which survives the message later being regenerated/edited/deleted. Returns the updated message; `422` if the message isn't an assistant message; `404` if the conversation/message isn't found |
| `POST` | `/v1/conversations/{id}/ask` | Same body as `/v1/ask`, plus an optional `audio: [{"filename": str, "data": "data:audio/{webm,wav,mp3,mpeg,mp4,m4a,ogg};base64,...", "duration_seconds": float\|null}]` (max 2 clips, ~25MB raw each — the transcription API's real limit, not a soft cap) — each clip is transcribed server-side and folded into `files` as a plain-text document attachment before the model ever sees it (see Meeting/audio ingestion in docs/features.md); `audio` is NOT accepted on the stateless `/v1/ask` below, only here and on `.../ask/stream` | Same shape as `/v1/ask`, with `\| context_messages=N` appended to `notes`; `404` if not found; `402`/`502` if a transcription budget refusal/failure occurs before the model is ever called |
| `POST` | `/v1/conversations/{id}/regenerate` | `{"mode": "auto"\|"budget"\|"fast"\|"smart", "model": str\|null}` (both optional) | Re-runs the conversation's last user question (always fresh, no cache), **replacing** the previous answer. Same response shape as `/v1/ask`; `400` if there is no user message, `404` if not found |
| `POST` | `/v1/conversations/{id}/regenerate/stream` | Same body as `/v1/conversations/{id}/regenerate` | Streaming (SSE) variant of regenerate |
| `POST` | `/v1/conversations/{id}/messages/{message_id}/edit` | Same body as `/v1/ask` | Edits a user message's text and re-asks it, **discarding** everything from that turn onward (the old answer and any later turns). Same response shape as `/v1/ask`; `404` if the conversation/message isn't found, `400` if `message_id` isn't a user message |
| `POST` | `/v1/conversations/{id}/messages/{message_id}/edit/stream` | Same body as edit | Streaming (SSE) variant of edit |
| `POST` | `/v1/conversations/{id}/messages/{message_id}/action` | `{"confirm": bool}` | Resolves a message's proposed action (propose-then-confirm — see Actions/webhooks below). `confirm: true` POSTs `{"action", "payload"}` to whichever webhook the action's name resolves to (`ACTIONS_WEBHOOKS`'s named route, else `ACTIONS_WEBHOOK_URL`); `confirm: false` just declines. Returns `{"action_status": "confirmed"\|"failed"\|"declined", "detail": str\|null}`; `404` if the conversation/message isn't found, `409` if the action was already resolved |
| `POST` | `/v1/conversations/{id}/messages/{message_id}/continue?request_id=str` | — (no body — `continue` has never taken one) | Resumes a truncated message (see Truncation detection + Continue in docs/features.md). `request_id` is an optional query param, not a body field |

`AskRequest`/`RegenerateRequest` (every body above except `continue`) accept an optional `request_id: str` — a client-generated idempotency key (see app/request_registry.py and "Disconnect-proof generation + send idempotency" in docs/features.md). A duplicate arrival of the same `request_id` within ~10 minutes is joined to the original call's result instead of dispatching a second model call; omitting it behaves exactly as before.

### Explicit abort

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `POST` | `/v1/requests/{request_id}/cancel` | — | The Stop button's cancellation signal — distinct from a bare disconnect, which no longer stops generation at all (see docs/features.md). Flags the matching in-flight worker to stop between provider-stream events and release its budget reservation. `{"cancelled": bool}` — `false` for an unknown or already-finished `request_id` (never an error); no owner check (`request_id` is an unguessable, single-use, client-generated UUID, the same trust boundary a share-link token relies on). Only meaningfully affects a STREAMING request in practice — a non-streaming call has no natural mid-call checkpoint to abort at. |

A conversation ask persists the user message, builds a context prompt from the last 12 prior messages, runs the orchestrator, then persists the assistant message with its `mode_used` and `notes`. If it is the first message and the conversation still has a generic title, the question becomes the title (auto-titling).

### Streaming ask (SSE)

```
POST /v1/conversations/{id}/ask/stream
Body: {"question": str, "mode": "auto"|"budget"|"fast"|"smart"}
Response: text/event-stream
```

Frames are `event: <name>\ndata: <json>\n\n`. The event sequence is:

1. `meta` — sent once, immediately after routing: `{"request_id": str, "mode_used": str, "model": str, "notes": str}`
2. `delta` — zero or more incremental answer chunks: `{"text": str}`
3. `done` — terminal on success: `{"answer": str, "mode_used": str, "notes": str, "sources": [{"title": str, "url": str}], "pending_action": {"action": str, "summary": str, "payload": object}, "images": [str], "code_results": [{"code": str, "logs": str\|null, "images": [str]\|null}]}` (`sources` present only when `WEB_SEARCH=true` triggered a web search for this answer; `pending_action` present only when the model proposed an action; `images` present only when the model generated one or more images; `code_results` present only when `CODE_EXECUTION=true` and the model ran code). The assistant message is already persisted to the database before this event is emitted, so clients can refetch messages on `done`.
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

Edit the task→model map live without a restart. Only model-selection keys are settable — the six tiers (`OPENAI_MODEL`, `OPENAI_MODEL_ROUTER`, `OPENAI_MODEL_BUDGET`, `OPENAI_MODEL_FAST`, `OPENAI_MODEL_SMART`, `OPENAI_MODEL_FALLBACK`), the eleven `MODEL_<CATEGORY>` keys, and the two free-lane keys (`FREE_TIER_MODELS`, `FREE_TIER_DEFAULT_QUOTA`). Credential keys are **not** settable, so this API can never write or read a secret. A saved value overrides the matching env var; clearing it reverts to the env/default.

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `GET` | `/v1/settings` | — | `{"editable": bool, "admin_gated": bool, "is_admin": bool, "tiers": [item, …], "categories": [item, …], "features": [flag, …], "free_lane": [item, …], "retention": [item, …]}` — `admin_gated` is `true` when `ADMIN_USERNAMES` is non-empty (multi-user mode active); `is_admin` is whether the caller is one of those admins; `editable` already folds in that check (`false` for a locked-out non-admin, same presentation as `ALLOW_SETTINGS_WRITE=false`) — the two extra fields just let the UI's banner and the admin-only Users section tell the reasons apart. Each `item` is `{"key": str, "label": str, "effective_model": str, "source": "override"\|"env"\|"default", "override": str\|null, "env": str\|null, "provider": str, "key_env": str, "key_present": bool\|null, …}` (categories also carry `category`, `tier`, `inherits`); each `flag` is `{"key": str, "label": str, "description": str, "effective_enabled": bool, "source": "override"\|"env"\|"default", "override": str\|null, "env": str\|null, "default": bool}`, one per key in `app/settings.py`'s `FEATURE_FLAG_KEYS` (`WEB_SEARCH`, `IMAGE_GENERATION`, `CODE_EXECUTION`, `MODERATION`, `CROSS_CONVERSATION_MEMORY`, `FACT_CHECK`, `MATH_SOLVE`, `IMAGE_DOWNSCALE`, `OCR_REPLACEMENT`, `CONCISE_MODE`, `SEMANTIC_CACHE`, `MODEL_CATALOG_SYNC`, `DB_BACKUP`, `FREE_TIER_ROUTING`, `RAG_LIBRARY`, `FREE_LANE_SMART`, `ACADEMIC_SEARCH`, `SELF_DESCRIBE`); each `free_lane` item is `{"key": "FREE_TIER_MODELS"\|"FREE_TIER_DEFAULT_QUOTA", "label": str, "effective_value": str, "source": "override"\|"env"\|"default", "override": str\|null, "env": str\|null, "default": str}`; each `retention` item is `{"key": "RETENTION_DAYS_DETAIL"\|"SHARE_EXPIRY_DAYS", "label": str, "effective_value": str, "source": "override"\|"env"\|"default", "override": str\|null, "env": str\|null, "default": str}` (`RETENTION_DAYS_DETAIL` defaults to `"365"`, `SHARE_EXPIRY_DAYS` to `""` — see Data retention + DB maintenance in docs/features.md) |
| `PUT` | `/v1/settings/{key}` | `{"value": str}` | The full settings view (as `GET`). An empty `value` clears the override; for a feature-flag `key`, `value` must be `"true"`/`"false"` (or another common spelling, normalized); `FREE_TIER_MODELS` must be a comma-separated list of valid model-name-shaped strings; `FREE_TIER_DEFAULT_QUOTA` must be a positive whole number; `RETENTION_DAYS_DETAIL` must be `0` or a positive whole number; `SHARE_EXPIRY_DAYS` must be a positive whole number. `400` if `key` isn't settable or `value` is malformed; `403` if `ALLOW_SETTINGS_WRITE=false`, or if the caller isn't an admin — gated whenever `ADMIN_USERNAMES` is non-empty (regardless of `ALLOW_REGISTRATION`), or, with `ADMIN_USERNAMES` empty, only while JWT auth + open registration are both active (legacy behavior, unchanged) |
| `DELETE` | `/v1/settings/{key}` | — | The full settings view, with that key's override cleared; `403` under the same conditions as `PUT` |
| `POST` | `/v1/settings/reset` | — | The full settings view, with every override cleared; `403` under the same conditions as `PUT` |

`key_present` is `true`/`false` when the required credential env var can be named (e.g. `GEMINI_API_KEY`), or `null` when it can't (e.g. Bedrock's AWS credentials). All four endpoints are behind the same auth as the rest of `/v1`.

### Free-first routing lane status

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `GET` | `/v1/free-tier` | — | `{"enabled": bool, "models": [{"model": str, "quota": int, "used": int, "remaining": int}, …]}` — one entry per model in `FREE_TIER_MODELS` order, `used`/`remaining` reset at UTC midnight. `models` is `[]` when the lane isn't configured. Same read used by the Usage panel's "Free lane remaining today" section. |

### Self-description

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `GET` | `/v1/capabilities` | — | `{"version": str, "models": {"tiers": {key: model}, "categories": {category: model}}, "flags": {key: bool}, "limits": {...int}, "budget": {"daily_budget_per_owner_usd": float\|null, "owner_remaining_usd": float\|null}, "free_lane": {"enabled": bool, "models": [...]}}` — this caller's own real self-description snapshot (see `app/self_describe.py`); the same data the `app_capabilities` tool (or, for a LiteLLM-routed model, the phrase-heuristic fallback) folds into an answer for a "what can you do" style question. `budget`/`free_lane` are owner-scoped, never the live global spend. |

### Document library (RAG)

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `GET` | `/v1/library/documents` | — | `[{"id", "filename", "mime_type", "size_bytes", "chunk_count", "created_at"}, …]` — this owner's uploaded library documents, most-recent first. |
| `POST` | `/v1/library/documents` | `{"filename": str, "data": "data:<mime>;base64,..."}` (same PDF/plain-text allowlist and size cap as a per-message attachment) | `201` + the created document — extracts text, chunks it, embeds and stores each chunk. `422` if no extractable text; `402` if the estimated embedding cost would exceed the daily budget; `502` if every chunk's embedding call fails. |
| `POST` | `/v1/library/seed-app-docs` | — | `201` + `[{"id", "filename", "mime_type", ...}, …]` (only the newly-created documents) — ingests this app's own `docs/*.md` into the caller's library (see `app/rag_library.py`'s `app_doc_files`); idempotent per filename, so a doc already present is skipped rather than re-embedded and re-charged. |
| `DELETE` | `/v1/library/documents/{document_id}` | — | `{"status": "deleted", "document_id": int}`, `404` if not found or not owned by the caller. |

### Response cache

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `GET` | `/v1/cache` | — | `{"enabled": bool, "entries": int, "ttl_seconds": int, "max_entries": int}` |
| `DELETE` | `/v1/cache` | — | `{"cleared": int, "enabled": bool, "entries": int, ...}` — empties the cache |
| `GET` | `/v1/semantic-cache` | — | `{"enabled": bool, "entries": int, "threshold": float, "max_entries": int}` |
| `DELETE` | `/v1/semantic-cache` | — | `{"cleared": int, "enabled": bool, "entries": int, ...}` — empties the semantic cache |
| `GET` | `/v1/model-catalog` | — | `{"enabled": bool, "synced_at": str \| null, "model_count": int, "new_models": [str, ...], "stale": bool, "error": str \| null}` — DB-only read UNLESS enabled and stale, in which case this triggers exactly one sync |
| `POST` | `/v1/model-catalog/sync` | — | Same shape as above; forces a sync now (ignoring staleness) — a no-op returning the current status when `MODEL_CATALOG_SYNC` is off |

The cache key is a hash of the prompt, the mode, and a signature of the effective model map (tier + category models, budgets, and reasoning efforts), so any routing change auto-invalidates stale entries. All four endpoints require the same auth as the rest of `/v1`.

The semantic cache (see Optional semantic (paraphrase) caching above) is a separate store, scoped the same way (mode + model-config signature + owner) but matched by embedding similarity instead of an exact key, and only ever populated by a context-free question. Its management endpoints follow the identical shape, deliberately mirroring `/v1/cache`.

The model catalog (see Optional self-updating model catalog above) is a singleton store, not scoped by owner — it's global pricing data, not per-user state. `new_models` lists model names first seen in the *most recent* sync relative to the one before it (always empty on the very first sync); `error` is only set when a sync was just attempted and failed, in which case the previously-cached catalog is left untouched.
