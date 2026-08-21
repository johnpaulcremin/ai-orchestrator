[← back to README](../README.md)

## Routing deep-dive

### Categories

In `auto` mode, the router model classifies each request into one category plus a complexity (`low` / `medium` / `high`) and a short reason. The classifier uses the Responses API's **structured output** (a strict JSON schema with the category constrained to a known enum), so it can't return unparseable text or an out-of-set category; a model that rejects the format param falls back to free-form prompting with tolerant parsing.

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
tier = "smart"   if category in SMART_CATEGORIES or complexity == "high"
       "budget"  elif complexity == "low" and OPENAI_MODEL_BUDGET is set
       "fast"    otherwise
```

So even a fast-category request (say, a summarization of a dense legal document that the classifier marks `complexity: high`) escalates to the smart tier — and, when a budget tier is configured, a genuinely trivial fast-category request (`complexity: low`) drops to it, leaving `fast` for the medium ones.

### Heuristic fallback

If the classifier call fails or returns unparseable output, routing falls back to keywords: the request goes **smart** if it is longer than 220 characters or contains any of:

`compare`, `tradeoff`, `design`, `architecture`, `plan`, `strategy`, `debug`, `error`, `why`, `explain`, `step-by-step`, `implement`, `refactor`, `optimize`, `security`, `threat`, `database`, `schema`

— otherwise **fast**. The `notes` field tells you which path ran (`AI router: ...` vs `Heuristic fallback selected ...`).

### Web search retrieval

With `WEB_SEARCH=true`, the classifier's structured output includes a third signal alongside category/complexity: `needs_live_data` — true only when the answer depends on real-world information that changes over time (news, prices, scores, weather, "latest"/"current" events), never for references to the user's own code/documents ("the current file", "the latest commit" are explicitly excluded in the classifier prompt). If the classifier is down, a small, deliberately narrow keyword fallback catches the unambiguous cases (`"weather today"`, `"who won"`, `"stock price"`, …) — it does **not** include bare words like "current"/"latest"/"now", which are far too common in ordinary dev questions.

The signal only ever takes effect when **all three** are true: `WEB_SEARCH=true`, the signal fired, and the *resolved* model is served by a provider with a hosted search tool wired up here — OpenAI or Anthropic (a Gemini/Bedrock/Mistral/other LiteLLM-routed model never gets it, even for a clearly time-sensitive question — there's no equivalent tool wired up for those). When it engages, the resolved provider's own hosted search tool — OpenAI's Responses API `web_search`, or Anthropic's Messages API `web_search_20250305` — grounds the answer in live results and any citations come back as `sources: [{"title", "url"}]` on the response (and persist with the message), the same shape regardless of which provider searched. A model that rejects the tool param still answers — just without a search — rather than the whole request failing. Web-searched answers are never written to the response cache, since a cached "current" answer would go stale on replay.

**Research mode** — the 🔎 button in the composer forces this same web-search path on for one question, bypassing the auto-mode classifier's freshness judgment entirely (`AskRequest.research: bool`). For "actually look this up" questions the heuristic might not flag as needing live data. Gated exactly the same as the automatic signal — no search if `WEB_SEARCH` isn't enabled or the resolved model isn't OpenAI-/Anthropic-served, rather than erroring — but, unlike the automatic signal, a denied override **says which gate stopped it** in the answer's `notes`. Research mode is an explicit instruction, and dropping one without a word is what made the app read as having no internet access at all: nothing reported the refusal, and the answering model, never told a search had been withheld, would agree it couldn't browse. Like any other web-searched answer, a successful one is never cached.

### Actions/webhooks (propose-then-confirm)

With `ACTIONS_WEBHOOK_URL` and/or `ACTIONS_WEBHOOKS` set, the model is offered a `propose_action` function tool it can call when the user actually asks for something to be done in the outside world (send an email, add a row to a sheet, post a message, ...). Calling the tool never executes anything by itself — it only records a proposal: `{"action": str, "summary": str, "payload": object}`, surfaced as `pending_action` on the answer (and persisted with the assistant message, with `action_status: "pending"`). The UI shows the `summary` with Confirm/Decline controls.

Nothing fires until the client explicitly calls `POST /v1/conversations/{id}/messages/{message_id}/action` with `{"confirm": true}` — only then is `{"action": str, "payload": object}` POSTed to the URL that `action` resolves to.

**Routing:** `ACTIONS_WEBHOOKS` (a JSON map, `{"send_email": "https://hooks.example/email", "update_sheet": "https://hooks.example/sheet"}`) sends each named action type to its own webhook — a Zap/Make scenario per action, rather than every action type landing on one shared hook the receiving side has to branch on. `ACTIONS_WEBHOOK_URL` serves as the catch-all fallback for any action name `ACTIONS_WEBHOOKS` doesn't cover, or as the only route at all if that's the whole setup (the original, single-webhook behavior — fully backward compatible). When `ACTIONS_WEBHOOKS` has any entries, the tool's `action` parameter is restricted to an enum of those names, so the model can only ever propose an action type that actually has somewhere to go — never a freeform name that silently falls through to the fallback (or nowhere, if there isn't one). With no `ACTIONS_WEBHOOKS` at all, `action` stays a freeform string, unchanged from the original design.

`action_status` becomes `"confirmed"` (the resolved webhook returned 2xx) or `"failed"` (request errored/non-2xx — safe to retry by calling the endpoint again); `{"confirm": false}` sets it to `"declined"` without any HTTP call. An already-resolved action returns `409` on a second call. Since every destination is fixed by the operator ahead of time and never supplied by the model or caller (the model picks a *name* from a closed set, never a URL), there is no SSRF surface — only the JSON payload sent to that URL is model-influenced.

### Image generation

With `IMAGE_GENERATION=true`, the model can generate images. Which backend handles it is picked by `IMAGE_GENERATION_MODEL`'s prefix — the same "prefix selects the provider" convention used for every other model setting in this app (`OPENAI_MODEL_FAST=gemini/...` already works the same way):

- **OpenAI** (default, `gpt-image-1`) — when the resolved TEXT model is OpenAI-served, it is offered the Responses API's hosted `image_generation` tool. Unlike web search there's no separate classifier signal — same as actions, the model itself decides when an image is actually warranted (an explicit request like "draw me..."/"generate an image of..."), so nothing changes for ordinary questions.
- **Gemini/Imagen** (`gemini/imagen-4.0-generate-001` or similar) — routed through LiteLLM, billed through your existing `GEMINI_API_KEY`. Gemini has no equivalent of a tool a chat model can call itself, so this backend always uses the standalone call below.

**The standalone call** covers everything the hosted tool cannot — the Gemini backend, and the OpenAI backend on any turn the router sent to a non-OpenAI model. That second case matters more than it sounds: an image request classifies as `creative_writing`, which routes to the smart tier, so on a Claude smart tier the hosted tool is never offered and the standalone call is the only path there is. Because it is a direct image call rather than a tool the resolved model invokes, it fires **regardless of which model answers the text**, and it survives a cross-provider fallback (the flags are recomputed for whichever model actually answers).

It is triggered by a deliberately high-precision heuristic checked directly against the question — a small grammar, not a list of literal phrases. A picture-verb (`draw`/`redraw`/`sketch`/`paint`/`illustrate`/`doodle`) carries the request on its own ("draw me a cat"), unless its object is abstract ("draw a conclusion", "draw up a plan", "what conclusions do you draw"); a maker-verb (`generate`/`create`/`make`/`produce`/`render`/`show me`/...) counts only with a picture-noun behind it (`image`, `diagram`, `mockup`, `poster`, `icon`, `illustration`, ...), so "create a mockup" fires and "create a function" does not. Chart/graph/plot are excluded on purpose, and a request naming any structural drawing (diagram, flowchart, schematic, architecture, wireframe) skips the image call entirely **when code execution is available to the answering model** — verified live: asked for a diagram of this app, Claude wrote SVG programmatically and produced a real hub-and-spoke drawing with legible labels, where an image model returns an impression of one with the text garbled. A diagram is a drawing of a STRUCTURE, and structure survives being drawn by code in a way it does not survive being imagined. Without code execution the image path is still taken, since a mediocre diagram beats none. The bias is still toward missing an unusually-phrased request over firing an extra paid call on an ordinary question.

Either way, generated images come back as `images: ["data:image/png;base64,..."]` on the answer and persist with the assistant message; the UI renders them inline. If the model (or the standalone call) produces an image but the reply has no other text, a short caption ("Here's the image you asked for.") is synthesized so it isn't dropped by the empty-answer guard.

`IMAGE_GENERATION_QUALITY` (default `high`, OpenAI-only) and `IMAGE_GENERATION_SIZE` (default `auto`) configure the call. Cost isn't token-based, so it's tracked separately: `IMAGE_GENERATION_COST_USD` (or a built-in per-quality estimate) is added to the answer's `cost_usd` and the spend log per generated image. `DAILY_BUDGET_USD`'s pre-dispatch check counts it too — whenever the image-generation tool is offered or the standalone heuristic fires, the gate assumes one image at the worst-case price on top of the token estimate, the same "price the worst case, not just what actually happens" philosophy it already applies to output tokens. A message with generated images is never written to the response cache either way (it has no column to store them).

### Image input / vision

Attach up to 4 images to a question — the 📎 button in the UI (reads the file(s) client-side into `data:image/{png,jpeg,gif,webp};base64,...` URLs, no upload endpoint involved), or `images: [...]` directly on `AskRequest`. Unlike the tool-based features above, this needs **no opt-in flag** and **no new key**: it's threaded to whichever model the request resolves to, across all three provider paths (OpenAI Responses API, Anthropic Messages API, and LiteLLM for everything else), each translated to that API's own image-content shape. A model that doesn't actually support vision either errors (triggering the normal cross-vendor fallback chain — unlike the other tool extras, attachments are deliberately kept on the fallback call too, since vision isn't provider-specific) or silently ignores the image (LiteLLM's `drop_params`).

Validation happens at the request boundary: at most 4 images, each capped in size (~9MB raw), and each must be a `data:image/...;base64,...` URL — a bare `http(s)://` URL is rejected outright, since passing one through as `image_url` would have the *provider's* servers fetch it on your behalf (an SSRF vector via a third party). Attached images persist with the user's message (`images` on `MessageOut`, same field the model's own generated images use — `role` disambiguates which is which) and render inline in the chat; regenerating a turn automatically reuses whatever images that turn was originally asked with. A request with attached images is never served from or written to the response cache, since the key is question text only and the answer's correctness depends on the image content.

### Automatic image cost reduction

Two automatic, no-toggle-required transforms (`app/image_processing.py`) run on attached images right before they're sent to a model — never touching what's persisted with the message, only what the model actually receives:

- **Downscaling** — a large image is resized down to a bounded resolution (`IMAGE_DOWNSCALE_MAX_DIMENSION`, default 1024px longest edge) before sending, since vision APIs tokenize images roughly proportional to pixel count. Skipped if the image is already at/under the cap.
- **OCR replacement** — the attachment is run through a local Tesseract OCR pass (`pytesseract`, no API call); if the extracted text is both confident (mean word confidence ≥ 60) and dense enough (≥ 40 characters), that plain text is sent instead of the image entirely — normally far cheaper than image tokens for the same content, and lets the model reason over clean text rather than "read" a picture. Requires the `tesseract` binary installed locally (set `TESSERACT_CMD` if it's not on `PATH`); silently no-ops if it isn't.

Both fail safe: any decode/library error just sends the original image unchanged. Both are also skipped entirely — full-quality image sent, unmodified — whenever the question implies the user needs to actually see fine detail ("read the small text", "zoom in", "exact wording", "pixel", "illegible", ...), the same narrow phrase-heuristic pattern used elsewhere in this app (e.g. the image-request/freshness heuristics). When OCR replaces an image, the extracted text is folded into the question sent to the model (not the persisted message); either transform firing is recorded in the answer's `notes` (`image_preprocessing: ocr_replaced=N, downscaled=N`) so the substitution is inspectable rather than silent.

Unlike the opt-in tool flags above, `IMAGE_DOWNSCALE` and `OCR_REPLACEMENT` default to **on** — they only ever reduce what a vision call costs, gated by their own heuristics, so there's no reason to make a user turn them on. Both are editable at runtime from the Settings panel, same as the other feature flags.

### Document input

Attach up to 4 documents (PDF or plain text) to a question — the same 📎 button in the UI, or `files: [{"filename", "data"}]` directly on `AskRequest` (`data` a `data:{application/pdf,text/plain};base64,...` URL). Same design as vision: **no opt-in flag**, **no new key**, threaded to whichever model the request resolves to across all three provider paths, each translated to that API's own document-content shape — OpenAI's `input_file` (`{"filename", "file_data"}`), Anthropic's `document` block, LiteLLM's `file` block (normalized across Gemini/Bedrock/etc.). One quirk worth knowing if you're calling the API directly rather than going through the UI: Claude's plain-text document source wants the *raw* decoded text, not base64, unlike every other content type here — the backend handles that conversion for you, it only affects how `providers.py` builds the block internally.

Validation happens at the request boundary: at most 4 files, each capped in size (~15MB raw), and each must be a `data:{application/pdf,text/plain};base64,...` URL — the same exact-match mime allowlist and SSRF reasoning as images (a bare remote URL is rejected outright). Attached files persist with the user's message (`files` on `MessageOut`; always `null` on assistant messages — the model can read a document, never produce one) and render as filename chips in the chat; regenerating a turn automatically reuses whatever files that turn was originally asked with. A request with attached files is never served from or written to the response cache.

### Voice input

Click the 🎤 button to dictate a question instead of typing it: the browser's `MediaRecorder` records a clip, which is sent to `POST /v1/transcribe` (a `data:audio/{webm,wav,mp3,mpeg,mp4,m4a,ogg};base64,...` URL in, `{"text": str}` out) and the transcribed text is inserted into the question box — appended after anything already typed — for you to review, edit, and send like any other question. `TRANSCRIPTION_MODEL` picks the OpenAI transcription model (default `gpt-4o-mini-transcribe`; `gpt-4o-transcribe` for higher quality, `whisper-1` for the classic model).

Unlike every other feature on this page, transcription is deliberately **not** threaded through the routing/fallback/provider-dispatch machinery: it's a discrete, explicitly user-triggered action (click record, speak, click stop) rather than something the chat flow or the model decides to do, so it's a plain synchronous OpenAI-only call. A failure returns a real `502`/`422` HTTP error rather than the `/v1/ask` convention of a `200` with an empty `answer` and an explanatory `notes` — there's no tier/fallback story to narrate through `notes` here.

### Voice output

Click the 🔊 button next to any assistant message to have it read aloud. The client POSTs the message's `content` to `POST /v1/speak`, gets raw `audio/mpeg` bytes back, and plays them via the browser's `Audio` API; the button becomes ⏹ while playing (click to stop) and only one clip plays at a time. `SPEECH_MODEL`/`SPEECH_VOICE` pick the OpenAI TTS model/voice (defaults `gpt-4o-mini-tts`/`alloy`). Same design as voice input: no opt-in flag, not threaded through routing/fallback, a real HTTP error on failure. Answers longer than OpenAI's 4096-character TTS input cap are truncated (a partial reading is more useful than none) rather than rejected.

### `mode_used` values

| Value | Meaning |
| --- | --- |
| `budget` | Caller forced the budget tier (`"mode": "budget"`) |
| `fast` | Caller forced the fast tier (`"mode": "fast"`) |
| `smart` | Caller forced the smart tier (`"mode": "smart"`) |
| `auto->budget` | Auto mode; a low-complexity fast-category task (or a bare greeting) went to the budget tier (only when `OPENAI_MODEL_BUDGET` is set) |
| `auto->fast` | Auto mode; the classifier (or heuristic) chose the fast tier |
| `auto->smart` | Auto mode; the classifier (or heuristic) chose the smart tier |
| `auto->smart:coding` | Auto mode; a per-category model (`MODEL_CODING`) handled the request (the `:category` suffix names which). The tier before the colon still set the budget/effort |
| `forced:<model>` | Caller forced an exact model (`"model": "<model>"`, e.g. via regenerate / switch-model), bypassing routing and the cache |
| `...->fallback` | Suffix appended when the primary model failed with an API error and a fallback model produced the answer (e.g. `auto->smart->fallback`) |

Authentication errors deliberately do **not** trigger the fallback chain — a bad key wouldn't be fixed by another model — and return an empty answer with an explanatory `notes`. Rate-limit / quota errors **do** fail over, but only to a **different provider** (the same throttled key would just be rejected again); with no cross-provider fallback configured they return the empty answer + note.
