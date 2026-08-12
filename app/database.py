"""Every SQLite read and write in this app, and the schema they run against —
one module, no ORM, no second storage engine.

The schema evolves in place. init_db() creates what is missing and then adds
each newer column guarded by a PRAGMA table_info check, never a bare ALTER
TABLE, so a database created by any older version upgrades on the next
startup instead of erroring. Anything that cannot be expressed that way goes
through _run_migrations, which snapshots the file first.

Money and quality signals are append-only ledgers (spend_log,
avoided_cost_log, feedback_log, retry_log), not counters. A counter can only
tell you the total; a ledger can still answer a question nobody had thought
to ask when the row was written — which is what lets the Usage panel and the
weekly self-report re-aggregate the same history by model, category, lane
and day. app/retention.py later folds old detail rows into monthly
aggregates and prunes them, so every window that might span that boundary
unions the rollups back in rather than silently reporting less history than
really happened.

Owner scoping is a WHERE clause on nearly every query here, with `owner IS
NULL` meaning the shared bucket (auth off, or a static token) rather than
"no owner" — see app/auth.py's current_owner. Getting that clause wrong
leaks one user's data to another, so the pattern is kept identical
everywhere rather than being written fresh per query.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .telemetry import logger


def _db_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", "ai_orchestrator.db"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                mode_used TEXT,
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
            ON messages(conversation_id)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER NOT NULL DEFAULT 1,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                last_login_at TEXT
            )
            """
        )

        # Persisted JWT revocation state (see app/revocation.py, which owns
        # the semantics; these tables are just where it survives a restart).
        # Two shapes on purpose: revoked_tokens retires ONE token (refresh
        # rotation), user_epochs retires EVERY token a user holds (logout-
        # everywhere) — a per-jti list alone cannot express the second, since
        # it never saw a token that was rotated onto a fresh jti. This state
        # was in-memory until the app's own self-critique pointed out that a
        # restart therefore un-revoked; new tables need no PRAGMA guard, so
        # an older database gains them on its next startup.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS revoked_tokens (
                jti TEXT PRIMARY KEY,
                expires_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_epochs (
                username TEXT PRIMARY KEY,
                epoch INTEGER NOT NULL
            )
            """
        )
        # The prune sweeps (revoked_token_add's lazy one, retention.py's
        # periodic one) filter on expires_at, which the jti PRIMARY KEY
        # can't serve — without this each sweep is a full scan of however
        # large a refresh burst let the table grow.
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_revoked_tokens_expires
            ON revoked_tokens(expires_at)
            """
        )

        # One row, written once: this database file's stable random identity
        # (see deployment_id below for why the DB, not the process, is the
        # thing worth identifying). id CHECK-pinned to 1 so it can never
        # accidentally become a second row.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deployment_identity (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                token TEXT NOT NULL
            )
            """
        )

        # Runtime-editable settings (the task->model map). Global: one row per
        # settable key. See app/settings.py for the resolution precedence.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Single-row marker for app/retention.py's maintenance_if_due()
        # weekly staleness check — deliberately a separate table from
        # `settings` above rather than a row in it: `settings` is the
        # operator-facing override map (SETTABLE_KEYS-gated, surfaced by
        # describe_settings()), and this is purely internal bookkeeping with
        # no override/env/default resolution of its own.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS maintenance_runs (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_run_at TEXT NOT NULL
            )
            """
        )

        # Per-owner marker for app/self_report.py's weekly self-report
        # staleness check — same shape as maintenance_runs above, but keyed
        # by owner (NOT a single CHECK(id = 1) row) since the report itself
        # is owner-scoped: each caller gets their own report, on their own
        # weekly clock, from their own last-generated timestamp. owner is
        # stored as '' for the unowned/shared caller, same sentinel
        # convention as spend_rollup/avoided_cost_rollup/feedback_rollup
        # (SQLite treats every NULL in a PRIMARY KEY/UNIQUE index as
        # distinct from every other NULL, which would let duplicate unowned
        # rows through a plain owner TEXT PRIMARY KEY).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS self_report_runs (
                owner TEXT PRIMARY KEY,
                last_run_at TEXT NOT NULL
            )
            """
        )

        # Response cache: an identical prompt (same mode + model config) returns
        # the stored answer without any model call. See app/cache.py.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS response_cache (
                key TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                mode TEXT NOT NULL,
                answer TEXT NOT NULL,
                mode_used TEXT,
                notes TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_hit_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                hit_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        # Spend log: one row per billable model call, recorded independently of
        # message persistence so that empty/truncated-but-costly answers (which
        # are deliberately not stored as messages) still count toward the daily
        # budget. See app/budget.py. owner NULL = unowned / static-token caller.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spend_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_spend_log_created_at
            ON spend_log(created_at)
            """
        )
        # Which conversation a billable call belongs to, when it belongs to one
        # (NULL for the stateless /v1/ask endpoints and any internal call).
        # Deliberately NOT a foreign key: spend is an accounting record and must
        # survive the conversation being deleted — the row stays, it simply
        # stops being attributable.
        #
        # Exists because a conversation's displayed cost was summed from its
        # MESSAGES, so a call billed without producing one was invisible in it
        # ($0.1014 shown against $0.5742 billed, in the session this came from).
        # retry_attribution covers part of the same ground per TURN and names
        # this as its residual limit — see record_failed_attempt. NULL on every
        # row written before this column existed, which is why
        # conversation_spend can only ever be as complete as the log.
        spend_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(spend_log)")
        }
        if "conversation_id" not in spend_columns:
            conn.execute("ALTER TABLE spend_log ADD COLUMN conversation_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_spend_log_conversation "
            "ON spend_log(conversation_id)"
        )

        # Avoided-cost log: one row per call that would have hit a model but
        # got served some other way instead — currently only the app's own
        # response-cache hits (see orchestrator._record_avoided_cost). A
        # SEPARATE table from spend_log, deliberately: spend_log backs the
        # daily budget cap, which must only ever sum real spend — mixing in
        # "cost that was NOT incurred" rows would corrupt that total unless
        # every reader filtered them back out. `avoided_cost_usd` is what the
        # call would have cost had it gone live (the cache entry's own
        # original cost_usd), null if that original call was itself unpriced.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS avoided_cost_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT,
                model TEXT,
                reason TEXT NOT NULL,
                avoided_cost_usd REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_avoided_cost_log_created_at
            ON avoided_cost_log(created_at)
            """
        )

        # Monthly rollups (see app/retention.py) for spend_log and
        # avoided_cost_log — computed from detail rows BEFORE they age past
        # RETENTION_DAYS_DETAIL and get pruned, so the ledgers' own row-per-
        # call growth is bounded without losing per-model history. Grouped
        # (owner, model, month) — coarser than the detail row (no per-call
        # granularity, no day-of-month), which is the deliberate trade for
        # bounded storage; `owner` is stored as '' for the unowned/shared
        # bucket rather than NULL, so (owner, model, month) can be a UNIQUE
        # constraint an upsert can target (SQLite treats every NULL in a
        # UNIQUE index as distinct from every other NULL, which would let
        # duplicate unowned rows through). A given (owner, model, month)
        # bucket is written to incrementally across many rollup runs (older
        # rows of a still-partially-live month age past the cutoff before
        # newer ones do), so upserts here ADD to any existing row rather than
        # replacing it. cost_usd is NOT NULL here (unlike spend_log's, which
        # is NULL for a genuinely unpriced model) — a rolled-up row for an
        # unpriced model reports 0.0 rather than staying "unknown" past the
        # prune boundary, a deliberate, documented narrowing rather than a
        # nullable column purely to preserve a rare edge case.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spend_rollup (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                model TEXT,
                month TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                UNIQUE (owner, model, month)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS avoided_cost_rollup (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                model TEXT,
                month TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                avoided_cost_usd REAL NOT NULL DEFAULT 0.0,
                UNIQUE (owner, model, month)
            )
            """
        )

        # Cached history summary per conversation, so a long thread's older
        # messages are folded into the summary incrementally (see
        # get_summary_cache/set_summary_cache and build_context_prompt in
        # main.py) instead of being re-summarized from scratch on every
        # single answer. older_count is how many of the conversation's older
        # (pre-recent-window) messages `summary` already covers.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS summary_cache (
                conversation_id INTEGER PRIMARY KEY,
                older_count INTEGER NOT NULL,
                summary TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Semantic (near-duplicate) response cache: a paraphrase-matched
        # answer for a CONTEXT-FREE question only (no conversation history,
        # no custom system prompt behind it — see app/semantic_cache.py for
        # why). scope_key groups entries the same way the exact cache scopes
        # by mode+model-config+owner, WITHOUT folding in the question text,
        # since matching is fuzzy here rather than an exact key lookup.
        # embedding is a JSON-encoded float array; no vector extension, a
        # brute-force cosine scan over (deliberately capped) scope_key rows.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS semantic_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_key TEXT NOT NULL,
                question TEXT NOT NULL,
                embedding TEXT NOT NULL,
                answer TEXT NOT NULL,
                mode_used TEXT,
                notes TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_semantic_cache_scope_key
            ON semantic_cache(scope_key)
            """
        )

        # Free-tier routing usage counters (see app/free_tier.py): one row per
        # (model, UTC date), incremented each time this app dispatches a call
        # to that model via the free-tier routing path. No explicit reset job
        # — "today" simply becomes a new row once the date rolls over, and old
        # rows are just never queried again (left in place; the table stays
        # small enough that pruning isn't worth the complexity).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS free_tier_usage (
                model TEXT NOT NULL,
                date TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (model, date)
            )
            """
        )

        # Read-only conversation share links (see app/routers/shares.py): at
        # most one live token per conversation — creating a new one replaces
        # any existing row for that conversation_id rather than accumulating
        # them, so there's never ambiguity about which link is "the" active
        # one. expires_at is NULL for a link with no expiry; UNIQUE on token
        # both prevents collisions and gives the lookup-by-token query (the
        # public GET /v1/shared/{token} route) a free index.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS share_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                token TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_share_tokens_conversation_id
            ON share_tokens(conversation_id)
            """
        )

        # Embedding cache (see app/semantic_cache.py's embed()): every caller
        # that needs an embedding (semantic response cache, cross-conversation
        # memory) shares this single cache keyed by (model, text) so asking
        # the same or a repeated question twice never re-pays the embeddings
        # API call. cache_key is a sha256 of "model\x1ftext" computed by the
        # caller, not raw text, so a duplicate row is impossible regardless of
        # which feature populated it first.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_cache (
                cache_key TEXT PRIMARY KEY,
                embedding TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Cross-conversation memory (see app/memory.py): one row per answered
        # turn in a conversation, an embedding of the QUESTION only (the
        # answer is stored as plain text alongside, not itself embedded) so a
        # later question in a DIFFERENT conversation can be matched against
        # it and injected as extra context. owner-scoped like every other
        # per-user table; NULL for a no-auth deployment, same convention as
        # conversations.owner.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT,
                conversation_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                embedding TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_owner ON memory(owner)
            """
        )

        # RAG document library (see app/rag_library.py): a per-owner set of
        # reference documents the model can automatically draw on, distinct
        # from a per-message attachment (which only exists for that one
        # turn). Two tables: library_documents is the upload's own metadata
        # (what GET /v1/library/documents lists), library_chunks is one row
        # per ~1,000-char chunk of that document's extracted text, each with
        # its own embedding for the brute-force cosine scan (same "no vector
        # DB" approach as memory/semantic_cache). chunk_count on the document
        # row is denormalized (kept in sync by rag_library.py) rather than
        # COUNT()'d per list request.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_library_documents_owner
            ON library_documents(owner)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                owner TEXT,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                embedding TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_library_chunks_owner
            ON library_chunks(owner)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_library_chunks_document
            ON library_chunks(document_id)
            """
        )

        # Self-updating model/pricing catalog (see app/model_catalog.py): a
        # singleton row (id always 1) holding the last successfully synced
        # LiteLLM pricing feed, so a sync failure or a restart never loses
        # the last good catalog. new_models_json is computed AT sync time
        # (the diff against the PREVIOUS model_names_json), not recomputed
        # later, since the previous list is overwritten by the same sync.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_catalog (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                pricing_json TEXT NOT NULL,
                model_names_json TEXT NOT NULL,
                new_models_json TEXT NOT NULL,
                model_count INTEGER NOT NULL
            )
            """
        )

        # Migration: add conversations.owner (NULL = shared / created without a
        # logged-in user) if an older DB predates per-user isolation, and
        # pinned_model (NULL = no pin) if it predates per-conversation model pins.
        conversation_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(conversations)")
        }
        if "owner" not in conversation_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN owner TEXT")
        if "pinned_model" not in conversation_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN pinned_model TEXT")
        # Custom per-conversation instructions (persona/style/rules) prepended to
        # every question in this conversation; NULL = none set.
        if "system_prompt" not in conversation_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN system_prompt TEXT")
        # Sidebar bookmark, independent of pinned_model (which routes a model,
        # not a UI sort order); 0/absent = not favorited.
        if "favorite" not in conversation_columns:
            conn.execute(
                "ALTER TABLE conversations ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0"
            )
        # Hides a conversation from the default sidebar list without deleting
        # it (recoverable, unlike Delete); 0/absent = not archived.
        if "archived" not in conversation_columns:
            conn.execute(
                "ALTER TABLE conversations ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
            )
        # JSON-encoded list of freeform user labels, e.g. '["work","urgent"]'.
        if "tags" not in conversation_columns:
            conn.execute(
                "ALTER TABLE conversations ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'"
            )

        # Migration: add token/cost columns to messages if an older DB predates
        # usage tracking.
        message_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(messages)")
        }
        for column, coltype in (
            ("input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("cost_usd", "REAL"),
            # 1 when this assistant message was served from the response cache.
            ("cached", "INTEGER"),
            # JSON-encoded list of {"title","url"} web citations (web_search
            # retrieval); NULL when the answer used none.
            ("sources", "TEXT"),
            # JSON-encoded list of the actual search-query strings the
            # web_search tool issued — distinct from `sources` (the RESULTS a
            # search returned); NULL when the answer used no web search.
            ("search_queries", "TEXT"),
            # JSON-encoded {"action","summary","payload"} the model proposed
            # (actions/webhooks); NULL when none was proposed.
            ("pending_action", "TEXT"),
            # "pending" | "confirmed" | "declined" | "failed"; NULL when there
            # was never a proposed action on this message.
            ("action_status", "TEXT"),
            # JSON-encoded list of `data:image/png;base64,...` strings (the
            # image_generation tool); NULL when none was generated.
            ("images", "TEXT"),
            # JSON-encoded list of {"filename","data"} document attachments
            # (vision-style file input); NULL when none was attached.
            ("files", "TEXT"),
            # JSON-encoded list of {"code","logs","images"} code_interpreter
            # tool calls; NULL when the answer ran none.
            ("code_results", "TEXT"),
            # JSON-encoded list of {"claim","rating","publisher","url"} results
            # from Google's Fact Check Tools API; NULL when none were surfaced.
            ("fact_checks", "TEXT"),
            # JSON-encoded list of {"title","authors","year","venue",
            # "citation_count","url","abstract_snippet"} results from
            # OpenAlex; NULL when none were surfaced.
            ("academic_results", "TEXT"),
            # JSON-encoded list of {"operation","expression","variable",
            # "result","error"} math_solve tool calls; NULL when none were made.
            ("math_results", "TEXT"),
            # JSON-encoded list of {"document","snippet_count"} RAG document
            # library sources drawn on for this answer; NULL when the library
            # was off, empty, or nothing cleared the similarity threshold.
            ("library_sources", "TEXT"),
            # JSON-encoded list of {"conversation_title","created_at"} —
            # provenance for cross-conversation memory recalled into this
            # answer's context (see app/memory.py); NULL when memory was off
            # or nothing cleared the recall threshold.
            ("memory_sources", "TEXT"),
            # JSON-encoded list of {"category","instruction","model",
            # "input_tokens","output_tokens","cost_usd","status"} — one entry
            # per step of an opt-in multi-step workflow answer (mode=
            # "workflow"); NULL for every ordinary (non-workflow) answer.
            ("workflow_steps", "TEXT"),
            # JSON-encoded list of {"filename","duration_seconds"} — metadata
            # for an audio clip the user attached and this app transcribed
            # server-side (see app/audio_ingestion.py); NULL when none was
            # attached. The audio bytes themselves are never persisted —
            # only this small metadata (for the UI's audio chip) and the
            # transcript itself, which lives in `files` like any other
            # document attachment.
            ("audio", "TEXT"),
        ):
            if column not in message_columns:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {coltype}")

        # A user-facing bookmark on a single message, distinct from favoriting
        # the whole conversation; 0/absent = not bookmarked.
        if "bookmarked" not in message_columns:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN bookmarked INTEGER NOT NULL DEFAULT 0"
            )

        # 1 when the provider stopped this assistant message early (hit
        # max_output_tokens) rather than finishing on its own; lets the UI
        # offer a Continue action instead of silently showing a cut-off answer.
        if "truncated" not in message_columns:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN truncated INTEGER NOT NULL DEFAULT 0"
            )

        # The output-token ceiling this answer was generated under (the tier's
        # budget — see routing.tier_output_caps). NULL for user messages, for
        # workflow answers (no single ceiling: each step has its own), and for
        # anything written before this column existed. Recorded because the
        # ceiling is a fact about the ATTEMPT, not about the app: re-deriving
        # it later from mode_used plus today's env vars is wrong twice over —
        # the caps are runtime-configurable, and "forced:<model>" doesn't say
        # which tier's budget it borrowed. The truncation notice names this
        # number, and the re-route control uses it to mark the options whose
        # own ceiling is no higher than the one that just cut an answer off.
        if "max_output_tokens" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN max_output_tokens INTEGER")

        # 1 when the call hit its ceiling before emitting ANY text of its own,
        # so this message's content is the app's explanation rather than a
        # partial answer. Distinct from `truncated`, which it always
        # accompanies: both mean "cut off", but only this one means there is
        # nothing to resume. Continue is refused for such a message — it would
        # bill a call to continue an apology — while the ceiling notice and
        # "Retry as workflow" (which re-answers in several capped steps) still
        # apply, because those are exactly the right remedies.
        if "no_output" not in message_columns:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN no_output INTEGER NOT NULL DEFAULT 0"
            )

        # A caller's 👍/👎 on a single assistant message: 1, -1, or NULL/absent
        # (never rated, or rated then cleared) — deliberately NULL-default,
        # not 0, so "never rated" and "rated then cleared" both read the same
        # way (0 would be ambiguous with a real "down" verdict if that were
        # ever encoded as 0). A pure marker, same contract as `bookmarked`:
        # setting it never touches the conversation's updated_at (see
        # set_message_feedback). feedback_reason is an optional short note
        # (only really meaningful alongside a -1), independent of feedback
        # itself so a reason without a verdict never blocks the migration.
        if "feedback" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN feedback INTEGER")
        if "feedback_reason" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN feedback_reason TEXT")
        # The literal model that answered (e.g. "gpt-5", "gemini/gemini-flash-
        # latest"), distinct from mode_used's routing DESCRIPTION ("auto->
        # fast", "auto->free:<model>", "forced:<model>") — genuinely absent
        # from the schema until now (AskResponse.model was added for
        # workflow's per-step breakdown but never threaded to persistence).
        # Needed so feedback_log can report real per-model quality stats for
        # ordinary auto-routed traffic, not just the forced/free-lane cases
        # where mode_used happens to embed a concrete model name.
        if "model" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN model TEXT")

        # Migration: add admin-user-management columns to users if an older DB
        # predates that feature. is_active gates login (deactivated accounts
        # keep their conversations, just can't authenticate); must_change_password
        # flags an admin-created/reset account that must set its own password
        # before it's fully provisioned; last_login_at is a cheap "last seen"
        # for the admin user list.
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "is_active" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
            )
        if "must_change_password" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
            )
        if "last_login_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")

        # Reusable prompt snippets, insertable into the composer of any
        # conversation — distinct from a conversation's own Custom
        # Instructions, which are scoped to one conversation and prepended
        # automatically rather than inserted on demand. owner NULL = shared
        # bucket, same convention as conversations.owner.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Append-only quality-feedback ledger (see app/feedback.py) — the
        # analytics source of truth. `messages.feedback` is only the message
        # row's CURRENT state; a 👎 is often immediately followed by
        # regenerate, which deletes the message row (see delete_messages_after)
        # and replaces it with a new one — the ledger is what keeps that
        # signal instead of silently losing it. `message_id` deliberately
        # carries no foreign-key constraint: a row here must survive its
        # message being deleted or replaced. `verdict` is 1 (up), -1 (down),
        # or 0 (a clear event — distinct from `messages.feedback`, which uses
        # NULL for "never rated"/"cleared" since a column has no separate
        # "event" vs "state" distinction). One row per set/change/clear, not
        # just the current state, same "ledger, not a mutable total" design
        # as avoided_cost_log.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT,
                message_id INTEGER,
                model TEXT,
                mode_used TEXT,
                category TEXT,
                verdict INTEGER NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_log_created_at "
            "ON feedback_log(created_at)"
        )

        # Monthly rollup for feedback_log — see spend_rollup's comment above
        # for the general design (owner stored as '', additive upsert across
        # runs). Grouped (owner, model, month) only, same as the other two
        # rollups — coarser than feedback_log's own (model, mode_used,
        # category) detail, so a pruned month's contribution to
        # GET /v1/feedback/summary's by_model breakdown survives, but its
        # by_category/by_lane breakdowns do not extend past the prune
        # boundary (see app/feedback.py's summarize()). verdict=0 (clear)
        # rows are never rolled up, matching feedback_log_entries' own
        # `verdict != 0` filter.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback_rollup (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                model TEXT,
                month TEXT NOT NULL,
                up_count INTEGER NOT NULL DEFAULT 0,
                down_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE (owner, model, month)
            )
            """
        )

        # Client-side crash reports (see frontend/src/crashReporter.ts and
        # POST /v1/client-errors in app/routers/system.py): a browser's
        # window.onerror/onunhandledrejection details, so a device that shows
        # a blank page (with devtools out of reach — e.g. a phone) still
        # leaves a readable error server-side. Bounded, not append-only
        # forever: record_client_error prunes to the newest
        # _CLIENT_ERRORS_MAX_ROWS on every insert, since this is a debugging
        # aid, not an analytics ledger like spend_log/feedback_log.
        # Implicit correction tracking (see app/correction_tracking.py) — a
        # soft, MEASUREMENT-ONLY signal distinct from feedback_log's explicit
        # 👍/👎: when a user's message right after an assistant answer matches
        # a curated correction phrase ("that's not what I asked", "wrong
        # tool", ...), one row is appended here AGAINST THAT PREVIOUS ANSWER.
        # Deliberately carries no message text/reason — only enough to
        # attribute the flag to a model/category/lane, per the explicit
        # "store the flag only" requirement (unlike feedback_log's optional
        # `reason`, which is a user-supplied note on an EXPLICIT rating, not
        # inferred text). `message_id` carries no foreign-key constraint,
        # same reasoning as feedback_log's: the flagged message can later be
        # deleted/replaced by a regenerate/edit without losing the signal.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS correction_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT,
                message_id INTEGER,
                model TEXT,
                mode_used TEXT,
                category TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_correction_log_created_at "
            "ON correction_log(created_at)"
        )

        # Fallback-reason ledger (see app/fallback_reason.py) — one row every
        # time the router's PRIMARY model call fails and a fallback is
        # attempted, classifying WHY: context_length_exceeded/timeout/
        # connection_error/quota_cooldown/tool_unsupported/budget_refusal/
        # provider_error. A SEPARATE table from spend_log deliberately: a
        # primary call that fails before spending any tokens writes no
        # spend_log row at all (see orchestrator_spend._record_spend's "spent
        # nothing -> release, don't record" branch), so spend_log is the
        # wrong place to hang this on — this ledger exists purely to answer
        # "why did we need to fall back", independent of whether the primary
        # attempt burned any tokens. `model` is the PRIMARY (intended) model
        # that failed; `succeeded` is whether SOME fallback candidate went on
        # to answer the request (0 when every candidate also failed/was
        # budget-refused) — see self_report.py's fallback-cause tally.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fallback_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT,
                model TEXT,
                reason TEXT NOT NULL,
                succeeded INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fallback_log_created_at "
            "ON fallback_log(created_at)"
        )

        # Re-run attribution ledger (see app/retry_attribution.py, and
        # app/retry_cost.py for what reads it): one row per ATTEMPT at
        # answering a user turn, written ONLY for turns that actually got
        # retried. It exists because a retry DESTROYS the thing the router
        # needs to be judged on: regenerate deletes the answer it replaces
        # (delete_messages_after) and edit deletes the user turn too
        # (delete_messages_from), so the replaced attempt's mode_used/model/
        # cost_usd are gone from `messages`, and spend_log — which does keep
        # the money — carries no conversation/message/category/tier column to
        # hang it on. Nothing else in the schema links a regeneration to what
        # it replaced (messages.notes says "regenerated" but never what, and
        # never how many times), so this cannot be derived after the fact.
        #
        #   turn_key        the user message id that STARTED this turn's
        #                   attempt chain — the stable identity. Regenerate
        #                   preserves the user row (id > after_id), so it is
        #                   durable there; an edit re-creates that row under
        #                   a new id, which is why user_message_id exists
        #                   alongside it (the id as of THIS attempt), letting
        #                   the chain survive an edit.
        #   attempt_index   1 = the original answer, 2 = first retry, ...
        #   signal          NULL on attempt 1 (it isn't a retry); otherwise
        #                   WHY the retry happened, kept as distinct values
        #                   rather than one counter — see retry_attribution's
        #                   SIGNALS, and its docstring for why collapsing
        #                   "regenerated, unrated" into "regenerated after a
        #                   👎" would mislabel taste as quality failure.
        #   created_at      the moment THAT attempt was answered, not the
        #                   moment this row was written: attempt 1's row is
        #                   backdated to the replaced message's own
        #                   created_at (it is recorded retroactively, at the
        #                   first retry), so a windowed read means the same
        #                   thing here as it does over `messages`.
        #
        # message_id carries no foreign key, same reasoning as feedback_log's
        # and correction_log's: for every attempt but the newest, the row it
        # names has already been deleted. Deliberately NOT pruned by
        # app/retention.py, unlike the five ledgers there — see that module's
        # docstring and retry_cost.summarize for the two reasons: this table
        # grows per RETRY (a handful of rows, not one per billable call), and
        # its denominator is `messages`, which is never pruned either, so
        # pruning attempts alone would silently drop total cost below
        # first-attempt cost for older windows.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS retry_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT,
                conversation_id INTEGER,
                turn_key INTEGER NOT NULL,
                user_message_id INTEGER,
                message_id INTEGER,
                attempt_index INTEGER NOT NULL,
                signal TEXT,
                mode_used TEXT,
                model TEXT,
                category TEXT,
                tier TEXT,
                cost_usd REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_retry_log_created_at "
            "ON retry_log(created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_retry_log_turn_key ON retry_log(turn_key)"
        )

        # Monthly rollup for correction_log — same design as feedback_rollup
        # above (owner stored as '', additive upsert across runs). Grouped
        # (owner, model, month) only, same coarser-than-detail tradeoff as
        # feedback_rollup: a pruned month's contribution to
        # app/correction_tracking.py's by_model breakdown (and its overall
        # total) survives, but its by_category/by_lane breakdowns do not
        # extend past the prune boundary. The "answers" denominator needs no
        # rollup of its own — it's read straight from `messages`
        # (assistant_message_mode_rows), which retention.py never prunes.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS correction_rollup (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                model TEXT,
                month TEXT NOT NULL,
                flagged_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE (owner, model, month)
            )
            """
        )

        # Monthly rollup for fallback_log — same design again, grouped
        # (owner, reason, month): app/fallback_reason.py's tally is already
        # reason-only (no model/category/lane breakdown to preserve), so this
        # is a complete rollup, not a coarsened one — a pruned month's
        # contribution to the "Paid fallback causes" tally survives in full.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fallback_rollup (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                reason TEXT NOT NULL,
                month TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                UNIQUE (owner, reason, month)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                stack TEXT,
                source_url TEXT,
                user_agent TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_client_errors_created_at "
            "ON client_errors(created_at)"
        )

        _run_migrations(conn)


# --- Versioned schema migrations --------------------------------------------
#
# Everything above this point is additive-only (CREATE TABLE IF NOT EXISTS,
# conditional ALTER TABLE ADD COLUMN) and safe to keep re-running on every
# startup exactly as it always has — it predates this mechanism and there's
# no benefit to retroactively converting that history into numbered steps.
#
# Anything from here on that ISN'T a simple "add a nullable column" — a
# rename, a drop, a type change, a data backfill — belongs in _MIGRATIONS
# instead: a numbered, ordered, run-at-most-once step. Progress is tracked
# via SQLite's own `PRAGMA user_version`, a plain integer baked into the
# database file's header, independent of the schema itself — no extra
# tracking table needed, and it survives being queried before any table
# exists.
#
# To add a migration: write a function `_migration_NNN_description(conn)`
# that performs the change, then append `(NNN, "description", function)` to
# _MIGRATIONS below, with NNN one higher than the current last entry. Never
# edit or renumber a migration that has already shipped — a database that
# already ran it tracks that by number, not by content.


def _migration_001_owner_indexes(conn: sqlite3.Connection) -> None:
    """conversations and templates are both listed/searched WHERE owner = ?
    (see list_conversations/search_conversations/list_templates) but neither
    table had a supporting index — the first migration to run through this
    mechanism, chosen because it's genuinely useful on its own, not just a
    demonstration."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_owner ON conversations(owner)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_templates_owner ON templates(owner)")


_MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (
        1,
        "add owner indexes to conversations and templates",
        _migration_001_owner_indexes,
    ),
)


def _backup_db_path(from_version: int) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    db_path = _db_path()
    return db_path.with_name(f"{db_path.name}.bak-v{from_version}-{timestamp}")


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply any _MIGRATIONS steps newer than this database's user_version,
    in order, each committed (and its version bump recorded) individually so
    a later step failing can't silently un-record an earlier step that
    already succeeded. A real on-disk backup is taken once before the batch,
    but only when there's a database file worth protecting and pending work
    to do — never on a normal startup where nothing changes.
    """
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    pending = sorted(
        (m for m in _MIGRATIONS if m[0] > current_version), key=lambda m: m[0]
    )
    if not pending:
        return

    db_path = _db_path()
    if db_path.exists() and db_path.stat().st_size > 0:
        # Fold the WAL into the main file first so the backup is a complete,
        # self-contained copy rather than missing whatever's still only in
        # the -wal sidecar.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        backup_path = _backup_db_path(current_version)
        shutil.copy2(db_path, backup_path)
        logger.info("db.migration_backup path=%s", backup_path)

    for version, description, apply in pending:
        logger.info(
            "db.migration_start version=%s description=%s", version, description
        )
        apply(conn)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        logger.info("db.migration_done version=%s", version)


def get_settings() -> dict[str, str]:
    """All persisted settings as a {key: value} map."""
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def set_setting(key: str, value: str) -> None:
    """Upsert a single setting."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )


def delete_setting(key: str) -> bool:
    """Remove a setting. Returns True if a row was deleted."""
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    return cursor.rowcount > 0


def clear_settings() -> None:
    """Remove every persisted setting (revert the whole map to env/defaults)."""
    with _connect() as conn:
        conn.execute("DELETE FROM settings")


def deployment_id() -> str:
    """This database file's stable random identity, created on first read and
    never changed — the anchor the frontend uses to notice that a DIFFERENT
    deployment has started answering its API port.

    The identity is the DATABASE, deliberately not the process: the dev
    server runs uvicorn --reload and restarts on every file save, so a
    per-process id would cry wolf constantly, while the failure this exists
    to catch — observed live, when a scratch-DB verification instance
    silently co-bound port 8000 (Windows SO_REUSEADDR allows it, no error)
    and fed seeded figures into the real UI's header — is precisely "a
    different database's numbers are on screen". A random token rather than
    a hash of the file path: the value travels in API responses, and a path
    hash could be confirmed by guessing paths.
    """
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO deployment_identity (id, token) VALUES (1, ?)",
            (secrets.token_hex(8),),
        )
        row = conn.execute(
            "SELECT token FROM deployment_identity WHERE id = 1"
        ).fetchone()
    return str(row["token"])


def last_maintenance_run_at() -> str | None:
    """The last recorded maintenance run's timestamp (created_at-format
    string), or None if maintenance has never run on this database."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_run_at FROM maintenance_runs WHERE id = 1"
        ).fetchone()
    return str(row["last_run_at"]) if row else None


def record_maintenance_run() -> None:
    """Upsert the single maintenance_runs row to CURRENT_TIMESTAMP."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO maintenance_runs (id, last_run_at)
            VALUES (1, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE SET last_run_at = CURRENT_TIMESTAMP
            """
        )


def last_self_report_run_at(owner: str | None) -> str | None:
    """This owner's last recorded weekly-self-report run (created_at-format
    string), or None if one has never been generated for them."""
    owner_key = owner or ""
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_run_at FROM self_report_runs WHERE owner = ?", (owner_key,)
        ).fetchone()
    return str(row["last_run_at"]) if row else None


def record_self_report_run(owner: str | None) -> None:
    """Upsert this owner's self_report_runs row to CURRENT_TIMESTAMP."""
    owner_key = owner or ""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO self_report_runs (owner, last_run_at)
            VALUES (?, CURRENT_TIMESTAMP)
            ON CONFLICT (owner) DO UPDATE SET last_run_at = CURRENT_TIMESTAMP
            """,
            (owner_key,),
        )


def record_spend(
    owner: str | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float | None,
    conversation_id: int | None = None,
) -> None:
    """Append a spend-log row for one billable model call.

    Recorded for every call that consumed tokens — including empty/truncated
    answers that are not stored as messages — so the daily budget sees all spend.
    `conversation_id` attributes it to a conversation when it belongs to one,
    which is what lets those not-stored-as-messages calls still show up in that
    conversation's own total (see conversation_spend).
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO spend_log
                (owner, model, input_tokens, output_tokens, cost_usd,
                 conversation_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (owner, model, input_tokens, output_tokens, cost_usd, conversation_id),
        )


def conversation_spend(conversation_id: int) -> dict[str, float | int]:
    """What a conversation ACTUALLY cost, from the spend log rather than from
    its saved messages.

    Returns `{"cost_usd", "input_tokens", "output_tokens"}` over every billable
    call attributed to this conversation — including the ones that never became
    a message (a discarded regenerate, a cancelled stream, an answer that came
    back empty). Callers compare it against the per-message totals they already
    have; the difference is spend the conversation incurred with nothing to
    show for it.

    Only ever as complete as the log: calls recorded before spend_log carried a
    conversation_id have NULL and are counted by no conversation.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM spend_log
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
    return {
        "cost_usd": float(row["cost_usd"]),
        "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]),
    }


def record_avoided_cost(
    owner: str | None,
    model: str | None,
    reason: str,
    avoided_cost_usd: float | None,
) -> None:
    """Append an avoided-cost-log row: a call that would have hit a model but
    got served some other way instead (currently: the app's own response-cache
    hits — see orchestrator._record_avoided_cost). Deliberately a separate
    table from spend_log; see its CREATE TABLE comment for why.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO avoided_cost_log (owner, model, reason, avoided_cost_usd)
            VALUES (?, ?, ?, ?)
            """,
            (owner, model, reason, avoided_cost_usd),
        )


def avoided_cost_today(owner: str | None) -> float:
    """This owner's total avoided cost recorded since UTC midnight today
    (0.0 if none) — the flip side of spend_today_usd."""
    owner_clause = "owner IS NULL" if owner is None else "owner = ?"
    owner_params: tuple[str, ...] = () if owner is None else (owner,)
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(avoided_cost_usd), 0.0) AS total
            FROM avoided_cost_log
            WHERE {owner_clause} AND created_at >= date('now')
            """,
            owner_params,
        ).fetchone()
    return float(row["total"] or 0.0)


def avoided_cost_by_reason(owner: str | None, days: int) -> dict[str, dict[str, Any]]:
    """This owner's avoided_cost_log rows over the last `days` days, grouped
    by `reason` ("response_cache_hit", "semantic_cache_hit", "free_tier"),
    as {reason: {"count", "avoided_cost_usd"}} — app/self_report.py's source
    for the weekly digest's cache-hit-rate and free-lane-savings figures.
    Same window convention as usage_summary/feedback_log_entries.
    """
    owner_clause = "owner IS NULL" if owner is None else "owner = ?"
    owner_params: tuple[str, ...] = () if owner is None else (owner,)
    window = f"-{max(days - 1, 0)} days"
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT reason,
                   COUNT(*) AS count,
                   COALESCE(SUM(avoided_cost_usd), 0.0) AS avoided_cost_usd
            FROM avoided_cost_log
            WHERE {owner_clause} AND created_at >= date('now', ?)
            GROUP BY reason
            """,
            (*owner_params, window),
        ).fetchall()
    return {
        row["reason"]: {
            "count": int(row["count"]),
            "avoided_cost_usd": float(row["avoided_cost_usd"] or 0.0),
        }
        for row in rows
    }


def tool_usage_counts(owner: str | None, days: int) -> dict[str, int]:
    """This owner's provider/hosted-tool usage over the last `days` days:
    how many of their messages carry a non-null sources/code_results/
    fact_checks/academic_results/math_results/workflow_steps column — the
    only record of tool usage that exists (no dedicated call-log table).
    Joins through conversations since messages has no owner column of its
    own, same join shape as list_bookmarked_messages.
    """
    owner_clause = "c.owner IS NULL" if owner is None else "c.owner = ?"
    owner_params: tuple[str, ...] = () if owner is None else (owner,)
    window = f"-{max(days - 1, 0)} days"
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE m.sources IS NOT NULL) AS web_search,
                COUNT(*) FILTER (WHERE m.code_results IS NOT NULL) AS code_execution,
                COUNT(*) FILTER (WHERE m.fact_checks IS NOT NULL) AS fact_check,
                COUNT(*) FILTER (WHERE m.academic_results IS NOT NULL) AS academic_search,
                COUNT(*) FILTER (WHERE m.math_results IS NOT NULL) AS math_solve,
                COUNT(*) FILTER (WHERE m.workflow_steps IS NOT NULL) AS workflow
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE {owner_clause} AND m.created_at >= date('now', ?)
            """,
            (*owner_params, window),
        ).fetchone()
    return {
        "web_search": int(row["web_search"]),
        "code_execution": int(row["code_execution"]),
        "fact_check": int(row["fact_check"]),
        "academic_search": int(row["academic_search"]),
        "math_solve": int(row["math_solve"]),
        "workflow": int(row["workflow"]),
    }


# --- Retention: rollup-before-prune for the ledgers (see app/retention.py) --
#
# Each function aggregates every detail row OLDER than `cutoff` (an
# 'YYYY-MM-DD HH:MM:SS'-comparable string, same format created_at itself
# uses) grouped by (owner, model, month), adds that into the matching rollup
# row (INSERT ... ON CONFLICT ... DO UPDATE SET x = x + excluded.x, so a
# month that's already partially rolled up from an earlier run accumulates
# rather than being overwritten), then deletes exactly the rows that were
# just aggregated. Returns the number of detail rows pruned. A no-op
# (returns 0) when nothing is older than cutoff — the common case once a
# deployment reaches steady state, since only the sliver of rows that just
# aged past the retention window needs rolling on any given run.


def rollup_and_prune_spend(cutoff: str) -> int:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(owner, '') AS owner, model,
                   strftime('%Y-%m', created_at) AS month,
                   COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cost_usd), 0.0) AS cost_usd
            FROM spend_log
            WHERE created_at < ?
            GROUP BY owner, model, month
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO spend_rollup
                    (owner, model, month, calls, input_tokens, output_tokens, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (owner, model, month) DO UPDATE SET
                    calls = calls + excluded.calls,
                    input_tokens = input_tokens + excluded.input_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    cost_usd = cost_usd + excluded.cost_usd
                """,
                (
                    row["owner"],
                    row["model"],
                    row["month"],
                    row["calls"],
                    row["input_tokens"],
                    row["output_tokens"],
                    row["cost_usd"],
                ),
            )
        cursor = conn.execute("DELETE FROM spend_log WHERE created_at < ?", (cutoff,))
        return cursor.rowcount


def rollup_and_prune_avoided_cost(cutoff: str) -> int:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(owner, '') AS owner, model,
                   strftime('%Y-%m', created_at) AS month,
                   COUNT(*) AS calls,
                   COALESCE(SUM(avoided_cost_usd), 0.0) AS avoided_cost_usd
            FROM avoided_cost_log
            WHERE created_at < ?
            GROUP BY owner, model, month
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO avoided_cost_rollup
                    (owner, model, month, calls, avoided_cost_usd)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (owner, model, month) DO UPDATE SET
                    calls = calls + excluded.calls,
                    avoided_cost_usd = avoided_cost_usd + excluded.avoided_cost_usd
                """,
                (
                    row["owner"],
                    row["model"],
                    row["month"],
                    row["calls"],
                    row["avoided_cost_usd"],
                ),
            )
        cursor = conn.execute(
            "DELETE FROM avoided_cost_log WHERE created_at < ?", (cutoff,)
        )
        return cursor.rowcount


def rollup_and_prune_feedback(cutoff: str) -> int:
    """Same rollup-then-delete shape as the other two, but only ever rolls
    verdict != 0 rows (clears carry no quality signal — see
    feedback_log_entries) — a clear older than cutoff is pruned outright,
    uncounted."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(owner, '') AS owner, model,
                   strftime('%Y-%m', created_at) AS month,
                   SUM(CASE WHEN verdict = 1 THEN 1 ELSE 0 END) AS up_count,
                   SUM(CASE WHEN verdict = -1 THEN 1 ELSE 0 END) AS down_count
            FROM feedback_log
            WHERE created_at < ? AND verdict != 0
            GROUP BY owner, model, month
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO feedback_rollup (owner, model, month, up_count, down_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (owner, model, month) DO UPDATE SET
                    up_count = up_count + excluded.up_count,
                    down_count = down_count + excluded.down_count
                """,
                (
                    row["owner"],
                    row["model"],
                    row["month"],
                    row["up_count"] or 0,
                    row["down_count"] or 0,
                ),
            )
        cursor = conn.execute(
            "DELETE FROM feedback_log WHERE created_at < ?", (cutoff,)
        )
        return cursor.rowcount


def rollup_and_prune_correction(cutoff: str) -> int:
    """Same rollup-then-delete shape as rollup_and_prune_feedback, grouped
    (owner, model, month) — see correction_rollup's CREATE TABLE comment for
    why by_category/by_lane don't get an equivalent."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(owner, '') AS owner, model,
                   strftime('%Y-%m', created_at) AS month,
                   COUNT(*) AS flagged_count
            FROM correction_log
            WHERE created_at < ?
            GROUP BY owner, model, month
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO correction_rollup (owner, model, month, flagged_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (owner, model, month) DO UPDATE SET
                    flagged_count = flagged_count + excluded.flagged_count
                """,
                (row["owner"], row["model"], row["month"], row["flagged_count"]),
            )
        cursor = conn.execute(
            "DELETE FROM correction_log WHERE created_at < ?", (cutoff,)
        )
        return cursor.rowcount


def rollup_and_prune_fallback(cutoff: str) -> int:
    """Same rollup-then-delete shape again, grouped (owner, reason, month) —
    see fallback_rollup's CREATE TABLE comment."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(owner, '') AS owner, reason,
                   strftime('%Y-%m', created_at) AS month,
                   COUNT(*) AS count
            FROM fallback_log
            WHERE created_at < ?
            GROUP BY owner, reason, month
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO fallback_rollup (owner, reason, month, count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (owner, reason, month) DO UPDATE SET
                    count = count + excluded.count
                """,
                (row["owner"], row["reason"], row["month"], row["count"]),
            )
        cursor = conn.execute(
            "DELETE FROM fallback_log WHERE created_at < ?", (cutoff,)
        )
        return cursor.rowcount


def prune_free_tier_usage(cutoff_date: str) -> int:
    """Delete free_tier_usage rows older than `cutoff_date` ('YYYY-MM-DD').
    No rollup: this table is already a compact (model, date) -> count
    counter, not a per-call ledger, so there's nothing to aggregate — a row
    past its quota's relevance is just dead weight."""
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM free_tier_usage WHERE date < ?", (cutoff_date,)
        )
        return cursor.rowcount


def spend_rollup_by_model(
    owner: str | None, window_start_month: str
) -> list[dict[str, Any]]:
    """Rolled-up spend, by model, for every month >= `window_start_month`
    ('YYYY-MM') — the rollup-side half of a detail ∪ rollup union (see
    app/database.py's usage_summary and app/retention.py)."""
    owner_key = "" if owner is None else owner
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT model,
                   COALESCE(SUM(calls), 0) AS calls,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cost_usd), 0.0) AS cost_usd
            FROM spend_rollup
            WHERE owner = ? AND month >= ?
            GROUP BY model
            """,
            (owner_key, window_start_month),
        ).fetchall()
    return [dict(row) for row in rows]


def spend_rollup_by_month(
    owner: str | None, window_start_month: str
) -> list[dict[str, Any]]:
    """Rolled-up spend, by calendar month, for every month >=
    `window_start_month` — used to fill in a by_day chart's monthly gaps
    once that month's detail has been pruned (see app/retention.py's
    fold_rollup_into_by_day)."""
    owner_key = "" if owner is None else owner
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT month,
                   COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                   COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
                       AS tokens
            FROM spend_rollup
            WHERE owner = ? AND month >= ?
            GROUP BY month
            """,
            (owner_key, window_start_month),
        ).fetchall()
    return [dict(row) for row in rows]


def feedback_rollup_by_model(
    owner: str | None, window_start_month: str
) -> list[dict[str, Any]]:
    """Rolled-up feedback verdict counts, by model, for every month >=
    `window_start_month` — the rollup-side half of app/feedback.py's
    summarize() union. No by_category/by_lane equivalent exists (see
    feedback_rollup's CREATE TABLE comment)."""
    owner_key = "" if owner is None else owner
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT model,
                   COALESCE(SUM(up_count), 0) AS up_count,
                   COALESCE(SUM(down_count), 0) AS down_count
            FROM feedback_rollup
            WHERE owner = ? AND month >= ?
            GROUP BY model
            """,
            (owner_key, window_start_month),
        ).fetchall()
    return [dict(row) for row in rows]


def correction_rollup_by_model(
    owner: str | None, window_start_month: str
) -> list[dict[str, Any]]:
    """Rolled-up correction-flag counts, by model, for every month >=
    `window_start_month` — the rollup-side half of app/correction_tracking.py's
    summarize() union. No by_category/by_lane equivalent (see
    correction_rollup's CREATE TABLE comment)."""
    owner_key = "" if owner is None else owner
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT model, COALESCE(SUM(flagged_count), 0) AS flagged_count
            FROM correction_rollup
            WHERE owner = ? AND month >= ?
            GROUP BY model
            """,
            (owner_key, window_start_month),
        ).fetchall()
    return [dict(row) for row in rows]


def fallback_rollup_by_reason(
    owner: str | None, window_start_month: str
) -> list[dict[str, Any]]:
    """Rolled-up fallback-reason counts for every month >=
    `window_start_month` — the rollup-side half of
    database.fallback_reason_counts()'s union (see fallback_rollup's CREATE
    TABLE comment: a complete rollup, not a coarsened one)."""
    owner_key = "" if owner is None else owner
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT reason, COALESCE(SUM(count), 0) AS count
            FROM fallback_rollup
            WHERE owner = ? AND month >= ?
            GROUP BY reason
            """,
            (owner_key, window_start_month),
        ).fetchall()
    return [dict(row) for row in rows]


def storage_stats() -> tuple[int, int]:
    """(reclaimable_bytes, total_bytes) for the current database file — the
    measurement app/retention.py's maintenance pass uses to decide whether a
    VACUUM is actually worth its exclusive lock and I/O ("measure first")."""
    with _connect() as conn:
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    return int(freelist) * int(page_size), int(page_count) * int(page_size)


def optimize() -> None:
    """PRAGMA optimize — SQLite's own cheap "refresh index stats if it looks
    worthwhile" pass, safe to run often."""
    with _connect() as conn:
        conn.execute("PRAGMA optimize")


def vacuum() -> None:
    """Reclaim free pages by rewriting the whole database file. Expensive
    (exclusive lock, full file rewrite) — callers gate this behind
    storage_stats() actually showing meaningful reclaimable space."""
    with _connect() as conn:
        conn.execute("VACUUM")


def _connect_manual() -> sqlite3.Connection:
    """A connection in manual-transaction (autocommit) mode, so the caller can
    issue an explicit BEGIN IMMEDIATE to serialize with other writers — needed
    for try_reserve_spend's atomic check-and-insert."""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def try_reserve_spend(
    owner: str | None,
    model: str,
    estimated_cost_usd: float,
    limit_usd: float | None,
    owner_limit_usd: float | None = None,
    conversation_id: int | None = None,
) -> tuple[bool, float, int | None]:
    """Atomically compare today's spend + `estimated_cost_usd` against
    `limit_usd` and, if it fits, insert a placeholder spend_log row for the
    estimate — all under one write lock (BEGIN IMMEDIATE), so two concurrent
    callers can't both read the same stale total and jointly admit past the
    cap (the gap a plain read-then-later-write leaves open). Returns
    (admitted, spent_before_this_reservation, reservation_id); reservation_id
    is None when not admitted. Reconcile an admitted reservation via
    finalize_spend/release_spend once the call's real cost is known.

    `owner_limit_usd`, when given, additionally caps THIS owner's own spend
    today (scoped the same way spend_log's owner column already is — `None`
    is its own distinct bucket, not "everyone"), checked in the same
    transaction so it can't be raced past either. Both the global and
    per-owner limits (whichever are configured; either may be None to skip
    that check) must be satisfied to admit.
    """
    conn = _connect_manual()
    try:
        conn.execute("BEGIN IMMEDIATE")
        spent = float(
            conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend_log "
                "WHERE created_at >= date('now')"
            ).fetchone()["total"]
        )
        if limit_usd is not None and spent + estimated_cost_usd > limit_usd:
            conn.execute("ROLLBACK")
            return False, spent, None
        if owner_limit_usd is not None:
            owner_clause = "owner IS NULL" if owner is None else "owner = ?"
            owner_params: tuple[str, ...] = () if owner is None else (owner,)
            owner_spent = float(
                conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend_log "
                    f"WHERE created_at >= date('now') AND {owner_clause}",
                    owner_params,
                ).fetchone()["total"]
            )
            if owner_spent + estimated_cost_usd > owner_limit_usd:
                conn.execute("ROLLBACK")
                return False, spent, None
        cursor = conn.execute(
            "INSERT INTO spend_log (owner, model, input_tokens, output_tokens, "
            "cost_usd, conversation_id) VALUES (?, ?, 0, 0, ?, ?)",
            (owner, model, estimated_cost_usd, conversation_id),
        )
        assert cursor.lastrowid is not None
        reservation_id = cursor.lastrowid
        conn.execute("COMMIT")
        return True, spent, reservation_id
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def finalize_spend(
    reservation_id: int, input_tokens: int, output_tokens: int, cost_usd: float | None
) -> None:
    """Reconcile a reservation row (see try_reserve_spend) to the call's real
    usage/cost, replacing the worst-case placeholder it was admitted with."""
    with _connect() as conn:
        conn.execute(
            "UPDATE spend_log SET input_tokens = ?, output_tokens = ?, cost_usd = ? "
            "WHERE id = ?",
            (input_tokens, output_tokens, cost_usd, reservation_id),
        )


def release_spend(reservation_id: int) -> None:
    """Zero out a reservation whose call never completed with any billable
    usage, so it stops counting against the daily cap."""
    with _connect() as conn:
        conn.execute(
            "UPDATE spend_log SET cost_usd = 0.0 WHERE id = ?", (reservation_id,)
        )


def spend_today_usd() -> float:
    """Total USD cost recorded since UTC midnight today (0.0 if none).

    `date('now')` and the CURRENT_TIMESTAMP defaults are both UTC, so this is a
    calendar-day total in UTC; the created_at index keeps the scan cheap.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0.0) AS total
            FROM spend_log
            WHERE created_at >= date('now')
            """
        ).fetchone()
    return float(row["total"] or 0.0)


def usage_summary(owner: str | None, days: int = 14) -> dict[str, Any]:
    """This owner's spend: today's total, a per-model breakdown, and a daily
    series, all windowed to the last `days` calendar days (UTC, inclusive of
    today). Days with no spend still appear in `by_day` with cost_usd=0, so a
    chart over the series has no gaps. In `by_model`, cost_usd is None (not
    0.0) for a model with no known cost at all — an unpriced model, never
    conflated with a genuinely free one.
    """
    owner_clause = "owner IS NULL" if owner is None else "owner = ?"
    owner_params: tuple[str, ...] = () if owner is None else (owner,)
    window = f"-{days - 1} days"

    with _connect() as conn:
        today_row = conn.execute(
            f"""
            SELECT COALESCE(SUM(cost_usd), 0.0) AS total
            FROM spend_log
            WHERE {owner_clause} AND created_at >= date('now')
            """,
            owner_params,
        ).fetchone()

        model_rows = conn.execute(
            f"""
            SELECT model,
                   COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   SUM(cost_usd) AS cost_usd
            FROM spend_log
            WHERE {owner_clause} AND created_at >= date('now', ?)
            GROUP BY model
            ORDER BY cost_usd DESC
            """,
            (*owner_params, window),
        ).fetchall()

        day_rows = conn.execute(
            f"""
            SELECT date(created_at) AS day,
                   COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                   COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
                       AS tokens
            FROM spend_log
            WHERE {owner_clause} AND created_at >= date('now', ?)
            GROUP BY day
            """,
            (*owner_params, window),
        ).fetchall()

    by_day_cost = {row["day"]: float(row["cost_usd"] or 0.0) for row in day_rows}
    by_day_tokens = {row["day"]: int(row["tokens"] or 0) for row in day_rows}
    today = datetime.now(timezone.utc).date()
    by_day = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        by_day.append(
            {
                "date": day,
                "cost_usd": by_day_cost.get(day, 0.0),
                "tokens": by_day_tokens.get(day, 0),
            }
        )

    window_cost = sum(by_day_cost.values())
    window_tokens = sum(by_day_tokens.values())

    return {
        "today_usd": float(today_row["total"] or 0.0),
        "days": days,
        "by_model": [
            {
                "model": row["model"] or "unknown",
                "calls": row["calls"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                # None (not 0.0) when this model has no known cost at all —
                # an unpriced model, not a genuinely free one.
                "cost_usd": (
                    float(row["cost_usd"]) if row["cost_usd"] is not None else None
                ),
            }
            for row in model_rows
        ],
        "by_day": by_day,
        # The tokens-per-dollar KPI: None when the window spent nothing (no
        # usage, or every call was free) — a ratio against zero cost isn't a
        # number, so the frontend uses window_tokens to tell those apart.
        "tokens_per_dollar": (window_tokens / window_cost) if window_cost > 0 else None,
        "window_tokens": window_tokens,
    }


def cache_get(key: str) -> dict[str, Any] | None:
    """A cache row plus its age in seconds, or None if absent."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT key, question, mode, answer, mode_used, notes, model,
                   input_tokens, output_tokens, cost_usd, hit_count,
                   CAST(strftime('%s', 'now') - strftime('%s', created_at)
                        AS INTEGER) AS age_seconds
            FROM response_cache
            WHERE key = ?
            """,
            (key,),
        ).fetchone()
    return dict(row) if row else None


def cache_put(
    key: str,
    question: str,
    mode: str,
    answer: str,
    mode_used: str | None,
    notes: str | None,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float | None,
) -> None:
    """Insert or replace a cache entry (a replace resets its age / TTL clock)."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO response_cache
                (key, question, mode, answer, mode_used, notes, model,
                 input_tokens, output_tokens, cost_usd,
                 created_at, last_hit_at, hit_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)
            ON CONFLICT(key) DO UPDATE SET
                answer = excluded.answer,
                mode_used = excluded.mode_used,
                notes = excluded.notes,
                model = excluded.model,
                input_tokens = excluded.input_tokens,
                output_tokens = excluded.output_tokens,
                cost_usd = excluded.cost_usd,
                created_at = CURRENT_TIMESTAMP
            """,
            (
                key,
                question,
                mode,
                answer,
                mode_used,
                notes,
                model,
                input_tokens,
                output_tokens,
                cost_usd,
            ),
        )


def cache_touch(key: str) -> None:
    """Record a cache hit (updates last_hit_at + hit_count)."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE response_cache
            SET last_hit_at = CURRENT_TIMESTAMP, hit_count = hit_count + 1
            WHERE key = ?
            """,
            (key,),
        )


def cache_delete(key: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM response_cache WHERE key = ?", (key,))


def cache_count() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM response_cache").fetchone()
    return int(row["n"]) if row else 0


def cache_delete_oldest(count: int) -> None:
    """Evict the `count` least-recently-hit entries."""
    if count <= 0:
        return
    with _connect() as conn:
        conn.execute(
            """
            DELETE FROM response_cache
            WHERE key IN (
                SELECT key FROM response_cache
                ORDER BY last_hit_at ASC, created_at ASC
                LIMIT ?
            )
            """,
            (count,),
        )


def cache_clear() -> int:
    """Remove every cache entry. Returns the number removed."""
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM response_cache").fetchone()["n"]
        conn.execute("DELETE FROM response_cache")
    return int(count)


def semantic_cache_list(scope_key: str) -> list[dict[str, Any]]:
    """Every stored embedding within this scope (mode+model-config+owner),
    for app.semantic_cache.find's brute-force similarity scan."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT question, embedding, answer, mode_used, notes, model,
                   input_tokens, output_tokens, cost_usd
            FROM semantic_cache
            WHERE scope_key = ?
            """,
            (scope_key,),
        ).fetchall()
    return [dict(row) for row in rows]


def semantic_cache_put(
    scope_key: str,
    question: str,
    embedding: str,
    answer: str,
    mode_used: str | None,
    notes: str | None,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float | None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO semantic_cache
                (scope_key, question, embedding, answer, mode_used, notes,
                 model, input_tokens, output_tokens, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope_key,
                question,
                embedding,
                answer,
                mode_used,
                notes,
                model,
                input_tokens,
                output_tokens,
                cost_usd,
            ),
        )


def semantic_cache_count() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM semantic_cache").fetchone()
    return int(row["n"]) if row else 0


def semantic_cache_delete_oldest(count: int) -> None:
    """Evict the `count` oldest entries (global, not per-scope — mirrors the
    exact cache's own simple global cap)."""
    if count <= 0:
        return
    with _connect() as conn:
        conn.execute(
            """
            DELETE FROM semantic_cache
            WHERE id IN (
                SELECT id FROM semantic_cache
                ORDER BY created_at ASC
                LIMIT ?
            )
            """,
            (count,),
        )


def semantic_cache_clear() -> int:
    """Remove every semantic-cache entry. Returns the number removed."""
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM semantic_cache").fetchone()["n"]
        conn.execute("DELETE FROM semantic_cache")
    return int(count)


def embedding_cache_get(cache_key: str) -> str | None:
    """The cached embedding (JSON float array) for `cache_key`, or None on a
    miss. See app/semantic_cache.py's embed() for the cache_key derivation."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT embedding FROM embedding_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    return str(row["embedding"]) if row else None


def embedding_cache_put(cache_key: str, embedding: str) -> None:
    """Insert-or-replace: two near-simultaneous callers embedding the same
    text is a harmless race (last write wins with the same value), not worth
    an explicit lock over."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO embedding_cache (cache_key, embedding)
            VALUES (?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET embedding = excluded.embedding
            """,
            (cache_key, embedding),
        )


def embedding_cache_count() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM embedding_cache").fetchone()
    return int(row["n"]) if row else 0


def embedding_cache_delete_oldest(count: int) -> None:
    if count <= 0:
        return
    with _connect() as conn:
        conn.execute(
            """
            DELETE FROM embedding_cache
            WHERE cache_key IN (
                SELECT cache_key FROM embedding_cache
                ORDER BY created_at ASC
                LIMIT ?
            )
            """,
            (count,),
        )


def embedding_cache_clear() -> int:
    """Remove every cached embedding. Returns the number removed."""
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM embedding_cache").fetchone()[
            "n"
        ]
        conn.execute("DELETE FROM embedding_cache")
    return int(count)


def memory_list(
    owner: str | None, exclude_conversation_id: int
) -> list[dict[str, Any]]:
    """Every stored memory entry for this owner, excluding the given
    conversation's own entries (that conversation's own history is already
    folded in via its summary — recalling from itself would be redundant) —
    for app.memory.recall's brute-force similarity scan.

    Joins the source conversation's title in (`conversation_title`) so
    app.memory.format_snippet can attach visible provenance to a recalled
    snippet — see that function's docstring for why: an entity-swap
    confusable ("Priya" vs "Devon", one date vs another) can clear the
    similarity threshold, and the source conversation's own title/date is
    the model's best remaining signal for catching a mismatch the
    embedding math didn't. A conversation deleted since the memory entry
    was written (LEFT JOIN, not JOIN) still recalls with `conversation_
    title=None` rather than silently disappearing.
    """
    owner_clause = "m.owner IS NULL" if owner is None else "m.owner = ?"
    params: list[Any] = [] if owner is None else [owner]
    params.append(exclude_conversation_id)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT m.conversation_id, m.question, m.answer, m.embedding,
                   m.created_at, c.title AS conversation_title
            FROM memory m
            LEFT JOIN conversations c ON c.id = m.conversation_id
            WHERE {owner_clause} AND m.conversation_id != ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def memory_put(
    owner: str | None,
    conversation_id: int,
    question: str,
    answer: str,
    embedding: str,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO memory (owner, conversation_id, question, answer, embedding)
            VALUES (?, ?, ?, ?, ?)
            """,
            (owner, conversation_id, question, answer, embedding),
        )


def memory_count(owner: str | None) -> int:
    owner_clause = "owner IS NULL" if owner is None else "owner = ?"
    params: list[Any] = [] if owner is None else [owner]
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM memory WHERE {owner_clause}", params
        ).fetchone()
    return int(row["n"]) if row else 0


def memory_total_count() -> int:
    """Total memory entries across every owner, for admin-facing stats (see
    app.memory.stats) — distinct from memory_count's per-owner scoping, which
    is what the eviction cap actually needs."""
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM memory").fetchone()
    return int(row["n"]) if row else 0


def memory_delete_oldest(owner: str | None, count: int) -> None:
    """Evict the `count` oldest entries for this owner (per-owner, unlike
    semantic_cache_delete_oldest's global cap — memory is meant to grow with
    one user's real conversation history over time, not share a single cap
    across every owner on a multi-user deployment)."""
    if count <= 0:
        return
    owner_clause = "owner IS NULL" if owner is None else "owner = ?"
    params: list[Any] = [] if owner is None else [owner]
    with _connect() as conn:
        conn.execute(
            f"""
            DELETE FROM memory
            WHERE id IN (
                SELECT id FROM memory
                WHERE {owner_clause}
                ORDER BY created_at ASC
                LIMIT ?
            )
            """,
            [*params, count],
        )


def memory_clear() -> int:
    """Remove every memory entry, across every owner. Returns the number
    removed."""
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM memory").fetchone()["n"]
        conn.execute("DELETE FROM memory")
    return int(count)


# --- RAG document library (see app/rag_library.py) ---------------------------


def library_document_create(
    owner: str | None, filename: str, mime_type: str, size_bytes: int
) -> dict[str, Any]:
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO library_documents (owner, filename, mime_type, size_bytes)
            VALUES (?, ?, ?, ?)
            """,
            (owner, filename, mime_type, size_bytes),
        )
        document_id = cursor.lastrowid
        row = conn.execute(
            "SELECT * FROM library_documents WHERE id = ?", (document_id,)
        ).fetchone()
    return dict(row)


def library_documents_list(owner: str | None) -> list[dict[str, Any]]:
    owner_clause = "owner IS NULL" if owner is None else "owner = ?"
    params: list[Any] = [] if owner is None else [owner]
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM library_documents
            WHERE {owner_clause}
            ORDER BY created_at DESC
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def library_document_get(document_id: int, owner: str | None) -> dict[str, Any] | None:
    owner_clause = "owner IS NULL" if owner is None else "owner = ?"
    params: list[Any] = [document_id]
    if owner is not None:
        params.append(owner)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT * FROM library_documents WHERE id = ? AND {owner_clause}",
            params,
        ).fetchone()
    return dict(row) if row else None


def library_document_set_chunk_count(document_id: int, count: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE library_documents SET chunk_count = ? WHERE id = ?",
            (count, document_id),
        )


def library_document_delete(document_id: int, owner: str | None) -> bool:
    """Deletes the document (and cascades to its chunks) if it exists and is
    owned by `owner`. Returns whether a row was actually removed, so the
    caller can 404 on a missing/not-owned id rather than silently no-op."""
    owner_clause = "owner IS NULL" if owner is None else "owner = ?"
    params: list[Any] = [document_id]
    if owner is not None:
        params.append(owner)
    with _connect() as conn:
        conn.execute("DELETE FROM library_chunks WHERE document_id = ?", (document_id,))
        cursor = conn.execute(
            f"DELETE FROM library_documents WHERE id = ? AND {owner_clause}", params
        )
    return cursor.rowcount > 0


def library_chunk_add(
    document_id: int, owner: str | None, chunk_index: int, text: str, embedding: str
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO library_chunks (document_id, owner, chunk_index, text, embedding)
            VALUES (?, ?, ?, ?, ?)
            """,
            (document_id, owner, chunk_index, text, embedding),
        )


def library_generation(owner: str | None) -> tuple[int, int]:
    """(chunk_count, highest_chunk_id) for this owner's library.

    A cheap fingerprint of EXACTLY the rows library_chunks_list below would
    scan, for folding into the response/semantic cache keys (see
    cache.library_generation) so a cached answer can't outlive the library
    state it was produced under. Deliberately fingerprints CHUNKS, not
    documents: chunks are what retrieval actually sees, so a document row
    that exists with no chunks yet (mid-upload, between
    library_document_create and the first library_chunk_add) correctly
    doesn't move it — it can't affect an answer either.

    Both halves are needed. The count alone misses "delete one document,
    upload another of the same size"; the max id alone misses a pure
    delete, since ids are never reused.
    """
    owner_clause = "owner IS NULL" if owner is None else "owner = ?"
    params: list[Any] = [] if owner is None else [owner]
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS chunk_count, COALESCE(MAX(id), 0) AS max_id
            FROM library_chunks
            WHERE {owner_clause}
            """,
            params,
        ).fetchone()
    return int(row["chunk_count"]), int(row["max_id"])


def library_chunks_list(owner: str | None) -> list[dict[str, Any]]:
    """Every stored chunk for this owner, each carrying its parent document's
    filename, for app.rag_library.retrieve's brute-force similarity scan."""
    owner_clause = "lc.owner IS NULL" if owner is None else "lc.owner = ?"
    params: list[Any] = [] if owner is None else [owner]
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT lc.id, lc.document_id, lc.chunk_index, lc.text, lc.embedding,
                   ld.filename
            FROM library_chunks lc
            JOIN library_documents ld ON ld.id = lc.document_id
            WHERE {owner_clause}
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_model_catalog() -> dict[str, Any] | None:
    """The last successfully synced model/pricing catalog, or None if a
    sync has never completed (see app/model_catalog.py)."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT fetched_at, pricing_json, model_names_json, new_models_json,
                   model_count
            FROM model_catalog
            WHERE id = 1
            """
        ).fetchone()
    return dict(row) if row else None


def set_model_catalog(
    pricing_json: str,
    model_names_json: str,
    new_models_json: str,
    model_count: int,
) -> None:
    """Upsert the singleton catalog row, stamping a fresh fetched_at."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO model_catalog
                (id, fetched_at, pricing_json, model_names_json, new_models_json,
                 model_count)
            VALUES (1, CURRENT_TIMESTAMP, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                fetched_at = CURRENT_TIMESTAMP,
                pricing_json = excluded.pricing_json,
                model_names_json = excluded.model_names_json,
                new_models_json = excluded.new_models_json,
                model_count = excluded.model_count
            """,
            (pricing_json, model_names_json, new_models_json, model_count),
        )


def revoked_token_add(jti: str, expires_at: int, now: int) -> None:
    """Persist one revoked token id until the moment it would have expired on
    its own, pruning entries already past theirs in the same transaction —
    the lazy-prune contract app/revocation.py has always had, now applied to
    the table a restart cannot empty. `now` is passed in rather than read
    here so the time source stays in revocation.py with the rest of the
    expiry semantics."""
    with _connect() as conn:
        conn.execute("DELETE FROM revoked_tokens WHERE expires_at < ?", (now,))
        conn.execute(
            """
            INSERT INTO revoked_tokens (jti, expires_at) VALUES (?, ?)
            ON CONFLICT(jti) DO UPDATE SET expires_at = excluded.expires_at
            """,
            (jti, expires_at),
        )


def revoked_token_present(jti: str, now: int) -> bool:
    """Whether `jti` is revoked as of `now`. `>=` deliberately: an entry at
    exactly its expiry second still reads as revoked, preserving
    revocation.py's strict-`<` prune boundary (see is_revoked's comment there
    for why this boundary is never load-bearing against PyJWT's own exp
    check)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM revoked_tokens WHERE jti = ? AND expires_at >= ?",
            (jti, now),
        ).fetchone()
    return row is not None


def prune_expired_revoked_tokens(now: int) -> int:
    """Delete revoked-token rows whose token has expired on its own — the
    periodic backstop to revoked_token_add's lazy sweep (see
    retention.rollup_and_prune). Returns the number of rows removed."""
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM revoked_tokens WHERE expires_at < ?", (now,))
    return int(cursor.rowcount)


def user_epoch_get(username: str) -> int:
    """The user's current session epoch — 0 until their first logout, since
    a missing row and an epoch of 0 mean the same thing: no token this user
    holds has ever been mass-invalidated."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT epoch FROM user_epochs WHERE username = ?", (username,)
        ).fetchone()
    return int(row["epoch"]) if row else 0


def user_epoch_bump(username: str) -> int:
    """Atomically advance the user's epoch and return the new value — the
    "log out everywhere" write. The upsert makes first-logout (no row yet)
    and every later logout the same single statement, so two concurrent
    logouts cannot read-modify-write past each other; the follow-up SELECT
    sits in the same transaction (no RETURNING — this file predates assuming
    it, and the two-statement form is equally atomic under SQLite's write
    lock)."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_epochs (username, epoch) VALUES (?, 1)
            ON CONFLICT(username) DO UPDATE SET epoch = epoch + 1
            """,
            (username,),
        )
        row = conn.execute(
            "SELECT epoch FROM user_epochs WHERE username = ?", (username,)
        ).fetchone()
    return int(row["epoch"])


def create_user(
    username: str, password_hash: str, must_change_password: bool = False
) -> dict[str, Any] | None:
    """Insert a user. Returns the new row, or None if the username is taken.

    `must_change_password` defaults to False (self-registration: the user
    already chose their own password) — admin-created accounts pass True.
    """
    try:
        with _connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO users (username, password_hash, must_change_password)
                VALUES (?, ?, ?)
                """,
                (username, password_hash, int(must_change_password)),
            )
            user_id = cursor.lastrowid

            row = conn.execute(
                """
                SELECT id, username, created_at, is_active, must_change_password,
                       last_login_at
                FROM users WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
    except sqlite3.IntegrityError:
        return None

    return dict(row)


def get_user_by_username(username: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, username, password_hash, created_at, is_active,
                   must_change_password, last_login_at
            FROM users
            WHERE username = ?
            """,
            (username,),
        ).fetchone()

    return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    """Every account, newest first, for the admin user-management list.
    Never includes password_hash."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, username, created_at, is_active, must_change_password,
                   last_login_at
            FROM users
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def set_user_password(
    username: str, password_hash: str, must_change_password: bool
) -> bool:
    """Replace a user's password hash (admin reset or self-service change),
    setting must_change_password to the given value. Returns False if the
    username doesn't exist."""
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE users
            SET password_hash = ?, must_change_password = ?
            WHERE username = ?
            """,
            (password_hash, int(must_change_password), username),
        )
    return cursor.rowcount > 0


def set_user_active(username: str, active: bool) -> dict[str, Any] | None:
    """Deactivate/reactivate a user (their conversations are untouched either
    way — this only gates login). Returns the updated row, or None if the
    username doesn't exist."""
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE users SET is_active = ? WHERE username = ?",
            (int(active), username),
        )
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            """
            SELECT id, username, created_at, is_active, must_change_password,
                   last_login_at
            FROM users WHERE username = ?
            """,
            (username,),
        ).fetchone()
    return dict(row) if row else None


def record_login(username: str) -> None:
    """Stamp a successful login's timestamp for the admin user list's
    "last seen" column."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE username = ?",
            (username,),
        )


_CONVERSATION_COLUMNS = (
    "id, title, owner, pinned_model, system_prompt, favorite, archived, tags, "
    "created_at, updated_at"
)


def create_conversation(title: str, owner: str | None = None) -> dict[str, Any]:
    clean_title = title.strip() or "Untitled conversation"

    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO conversations (title, owner) VALUES (?, ?)",
            (clean_title, owner),
        )
        conversation_id = cursor.lastrowid

        row = conn.execute(
            f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()

    return dict(row)


def list_conversations(
    owner: str | None = None, include_archived: bool = False
) -> list[dict[str, Any]]:
    # owner is None for the shared/unauthenticated bucket (owner IS NULL);
    # a username returns only that user's conversations. Favorited
    # conversations sort first as a group, most-recently-updated within each
    # group, so starring one is a pure reorder, not something that also
    # changes when a conversation would otherwise appear. Archived
    # conversations are hidden from the default list (like an inbox archive,
    # not a delete) unless include_archived is set.
    archived_clause = "" if include_archived else "AND archived = 0"
    # A correlated subquery, not a JOIN + GROUP BY: this list is already
    # per-conversation, and messages(conversation_id) is indexed, so this
    # stays cheap without reshaping the query's grouping.
    count_column = (
        "(SELECT COUNT(*) FROM messages WHERE messages.conversation_id = "
        "conversations.id) AS message_count"
    )
    with _connect() as conn:
        if owner is None:
            rows = conn.execute(
                f"""
                SELECT {_CONVERSATION_COLUMNS}, {count_column}
                FROM conversations
                WHERE owner IS NULL {archived_clause}
                ORDER BY favorite DESC, updated_at DESC, id DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT {_CONVERSATION_COLUMNS}, {count_column}
                FROM conversations
                WHERE owner = ? {archived_clause}
                ORDER BY favorite DESC, updated_at DESC, id DESC
                """,
                (owner,),
            ).fetchall()

    return [dict(row) for row in rows]


def get_conversation(conversation_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()

    return dict(row) if row else None


def free_tier_usage_count(model: str, date: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT count FROM free_tier_usage WHERE model = ? AND date = ?",
            (model, date),
        ).fetchone()
    return int(row["count"]) if row else 0


def free_tier_usage_increment(model: str, date: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO free_tier_usage (model, date, count)
            VALUES (?, ?, 1)
            ON CONFLICT(model, date) DO UPDATE SET count = count + 1
            """,
            (model, date),
        )


def free_tier_usage_set(model: str, date: str, count: int) -> None:
    """Set (not increment) today's usage counter for `model` — used by
    free_tier.exhaust_for_today to immediately mark a throttled model as
    out of quota for the rest of the UTC day."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO free_tier_usage (model, date, count)
            VALUES (?, ?, ?)
            ON CONFLICT(model, date) DO UPDATE SET count = excluded.count
            """,
            (model, date, count),
        )


def create_share_token(
    conversation_id: int, token: str, expires_at: str | None
) -> None:
    """Replace any existing share link for this conversation with a fresh
    one — at most one live token per conversation (see share_tokens' table
    comment in init_db)."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM share_tokens WHERE conversation_id = ?", (conversation_id,)
        )
        conn.execute(
            """
            INSERT INTO share_tokens (conversation_id, token, expires_at)
            VALUES (?, ?, ?)
            """,
            (conversation_id, token, expires_at),
        )


def get_share_token(conversation_id: int) -> dict[str, Any] | None:
    """This conversation's live share token (token, expires_at, created_at),
    or None if it has never been shared or its link has expired — the
    expiry check happens in SQL against CURRENT_TIMESTAMP so it can never
    drift from whatever clock this same row's other timestamps were written
    against."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT token, expires_at, created_at FROM share_tokens
            WHERE conversation_id = ?
              AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            """,
            (conversation_id,),
        ).fetchone()
    return dict(row) if row else None


def get_conversation_id_by_token(token: str) -> int | None:
    """The conversation a live (non-expired) share token points to, or None
    for an unknown or expired token — the public GET /v1/shared/{token}
    route's only gate, so an expired token 404s exactly like one that never
    existed rather than revealing it once did."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT conversation_id FROM share_tokens
            WHERE token = ?
              AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            """,
            (token,),
        ).fetchone()
    return int(row["conversation_id"]) if row else None


def delete_share_tokens(conversation_id: int) -> int:
    """Revoke this conversation's share link (if any). Returns the number of
    rows removed (0 or 1, given create_share_token's replace-not-accumulate
    behavior) so the caller can tell a real revoke from a no-op."""
    with _connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM share_tokens WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()["n"]
        conn.execute(
            "DELETE FROM share_tokens WHERE conversation_id = ?", (conversation_id,)
        )
    return int(count)


def update_conversation_title(
    conversation_id: int, title: str
) -> dict[str, Any] | None:
    clean_title = title.strip() or "Untitled conversation"

    with _connect() as conn:
        conn.execute(
            """
            UPDATE conversations
            SET title = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (clean_title, conversation_id),
        )

        row = conn.execute(
            f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()

    return dict(row) if row else None


def set_conversation_pin(
    conversation_id: int, pinned_model: str | None
) -> dict[str, Any] | None:
    """Pin a model/tier to a conversation (None or '' clears the pin)."""
    value = (pinned_model or "").strip() or None

    with _connect() as conn:
        conn.execute(
            "UPDATE conversations SET pinned_model = ? WHERE id = ?",
            (value, conversation_id),
        )
        row = conn.execute(
            f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()

    return dict(row) if row else None


def set_conversation_system_prompt(
    conversation_id: int, system_prompt: str | None
) -> dict[str, Any] | None:
    """Set (or, with None/'', clear) this conversation's custom instructions."""
    value = (system_prompt or "").strip() or None

    with _connect() as conn:
        conn.execute(
            "UPDATE conversations SET system_prompt = ? WHERE id = ?",
            (value, conversation_id),
        )
        row = conn.execute(
            f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()

    return dict(row) if row else None


def set_conversation_favorite(
    conversation_id: int, favorite: bool
) -> dict[str, Any] | None:
    """Star (or unstar) a conversation for the sidebar. Doesn't touch
    updated_at — a bookmark toggle isn't "activity" and must not reshuffle
    the recency ordering within the favorited/unfavorited groups."""
    with _connect() as conn:
        conn.execute(
            "UPDATE conversations SET favorite = ? WHERE id = ?",
            (1 if favorite else 0, conversation_id),
        )
        row = conn.execute(
            f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()

    return dict(row) if row else None


def set_conversation_archived(
    conversation_id: int, archived: bool
) -> dict[str, Any] | None:
    """Archive (or restore) a conversation — hides it from the default
    sidebar list without deleting anything, unlike delete_conversation.
    Doesn't touch updated_at, for the same reason set_conversation_favorite
    doesn't: this is a visibility toggle, not activity."""
    with _connect() as conn:
        conn.execute(
            "UPDATE conversations SET archived = ? WHERE id = ?",
            (1 if archived else 0, conversation_id),
        )
        row = conn.execute(
            f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()

    return dict(row) if row else None


def set_conversation_tags(
    conversation_id: int, tags: list[str]
) -> dict[str, Any] | None:
    """Replace a conversation's freeform tags wholesale. Doesn't touch
    updated_at, same as favorite/archived — organizing conversations isn't
    "activity"."""
    with _connect() as conn:
        conn.execute(
            "UPDATE conversations SET tags = ? WHERE id = ?",
            (json.dumps(tags), conversation_id),
        )
        row = conn.execute(
            f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()

    return dict(row) if row else None


def duplicate_conversation(
    conversation_id: int, owner: str | None
) -> dict[str, Any] | None:
    """Copy a conversation (title, pin, instructions, and every message,
    full-fidelity including attachments/cost/tokens) into a brand-new one
    owned by `owner`. Returns None if the source doesn't exist.

    A pending action is deliberately NOT copied: confirming it on the
    duplicate would re-fire the exact same webhook payload a second time,
    which the propose-then-confirm model must never do implicitly. Archived
    status is also deliberately NOT copied: duplicating implies wanting a
    fresh working copy, which archiving it immediately would defeat.
    """
    original = get_conversation(conversation_id)
    if original is None:
        return None

    new_conversation = create_conversation(f"{original['title']} (copy)", owner)
    new_id = new_conversation["id"]

    if original.get("pinned_model"):
        set_conversation_pin(new_id, original["pinned_model"])
    if original.get("system_prompt"):
        set_conversation_system_prompt(new_id, original["system_prompt"])
    if original.get("favorite"):
        set_conversation_favorite(new_id, True)
    original_tags = original.get("tags")
    if original_tags and original_tags != "[]":
        set_conversation_tags(new_id, json.loads(original_tags))

    for message in list_messages(conversation_id):
        add_message(
            conversation_id=new_id,
            role=message["role"],
            content=message["content"],
            mode_used=message["mode_used"],
            notes=message["notes"],
            input_tokens=message["input_tokens"],
            output_tokens=message["output_tokens"],
            cost_usd=message["cost_usd"],
            cached=bool(message["cached"]),
            sources=message["sources"],
            search_queries=message["search_queries"],
            images=message["images"],
            files=message["files"],
            audio=message["audio"],
            truncated=bool(message["truncated"]),
            max_output_tokens=message["max_output_tokens"],
            no_output=bool(message["no_output"]),
            code_results=message["code_results"],
            fact_checks=message["fact_checks"],
            academic_results=message["academic_results"],
            math_results=message["math_results"],
            library_sources=message["library_sources"],
            memory_sources=message["memory_sources"],
            workflow_steps=message["workflow_steps"],
            model=message["model"],
            feedback=message["feedback"],
            feedback_reason=message["feedback_reason"],
        )

    return get_conversation(new_id)


def branch_conversation(
    conversation_id: int, owner: str | None, up_to_message_id: int
) -> dict[str, Any] | None:
    """Like duplicate_conversation, but only copies messages up to and
    including `up_to_message_id` — for branching an alternate line of
    conversation from some earlier point without disturbing the original.
    Returns None if the source conversation doesn't exist, or if
    `up_to_message_id` doesn't belong to it.
    """
    original = get_conversation(conversation_id)
    if original is None:
        return None

    messages = list_messages(conversation_id)
    cutoff = next(
        (index for index, m in enumerate(messages) if m["id"] == up_to_message_id),
        None,
    )
    if cutoff is None:
        return None
    messages = messages[: cutoff + 1]

    new_conversation = create_conversation(f"{original['title']} (branch)", owner)
    new_id = new_conversation["id"]

    if original.get("pinned_model"):
        set_conversation_pin(new_id, original["pinned_model"])
    if original.get("system_prompt"):
        set_conversation_system_prompt(new_id, original["system_prompt"])
    original_tags = original.get("tags")
    if original_tags and original_tags != "[]":
        set_conversation_tags(new_id, json.loads(original_tags))

    for message in messages:
        add_message(
            conversation_id=new_id,
            role=message["role"],
            content=message["content"],
            mode_used=message["mode_used"],
            notes=message["notes"],
            input_tokens=message["input_tokens"],
            output_tokens=message["output_tokens"],
            cost_usd=message["cost_usd"],
            cached=bool(message["cached"]),
            sources=message["sources"],
            search_queries=message["search_queries"],
            images=message["images"],
            files=message["files"],
            audio=message["audio"],
            truncated=bool(message["truncated"]),
            max_output_tokens=message["max_output_tokens"],
            no_output=bool(message["no_output"]),
            code_results=message["code_results"],
            fact_checks=message["fact_checks"],
            academic_results=message["academic_results"],
            math_results=message["math_results"],
            library_sources=message["library_sources"],
            memory_sources=message["memory_sources"],
            workflow_steps=message["workflow_steps"],
            model=message["model"],
            feedback=message["feedback"],
            feedback_reason=message["feedback_reason"],
        )

    return get_conversation(new_id)


def delete_conversation(conversation_id: int) -> bool:
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()

        if not existing:
            return False

        conn.execute(
            "DELETE FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        )

        conn.execute(
            "DELETE FROM conversations WHERE id = ?",
            (conversation_id,),
        )

        conn.execute(
            "DELETE FROM summary_cache WHERE conversation_id = ?",
            (conversation_id,),
        )

        conn.execute(
            "DELETE FROM share_tokens WHERE conversation_id = ?",
            (conversation_id,),
        )

    return True


_MESSAGE_COLUMNS = (
    "id, conversation_id, role, content, mode_used, notes, "
    "input_tokens, output_tokens, cost_usd, cached, sources, search_queries, "
    "pending_action, action_status, images, files, audio, bookmarked, truncated, "
    "max_output_tokens, no_output, "
    "code_results, fact_checks, academic_results, math_results, "
    "library_sources, memory_sources, workflow_steps, model, feedback, feedback_reason, "
    "created_at"
)


def add_message(
    conversation_id: int,
    role: str,
    content: str,
    mode_used: str | None = None,
    notes: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    cached: bool = False,
    sources: str | None = None,
    search_queries: str | None = None,
    pending_action: str | None = None,
    action_status: str | None = None,
    images: str | None = None,
    files: str | None = None,
    audio: str | None = None,
    truncated: bool = False,
    max_output_tokens: int | None = None,
    no_output: bool = False,
    code_results: str | None = None,
    fact_checks: str | None = None,
    academic_results: str | None = None,
    math_results: str | None = None,
    library_sources: str | None = None,
    memory_sources: str | None = None,
    workflow_steps: str | None = None,
    model: str | None = None,
    feedback: int | None = None,
    feedback_reason: str | None = None,
) -> dict[str, Any]:
    """`sources`/`search_queries`/`pending_action`/`images`/`files`/`audio`/
    `code_results`/`fact_checks`/`academic_results`/`math_results`/
    `library_sources`/`memory_sources`/`workflow_steps`, if given, must
    already be JSON-encoded strings.

    `feedback`/`feedback_reason` are accepted here (unlike `bookmarked`,
    which relies entirely on its column DEFAULT) so a duplicated/branched/
    imported message can carry its rating forward — see
    duplicate_conversation/branch_conversation/routers/conversations.py's
    import_conversation.
    """
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages
                (conversation_id, role, content, mode_used, notes,
                 input_tokens, output_tokens, cost_usd, cached, sources, search_queries,
                 pending_action, action_status, images, files, audio, truncated,
                 max_output_tokens, no_output,
                 code_results, fact_checks, academic_results, math_results,
                 library_sources, memory_sources, workflow_steps, model, feedback,
                 feedback_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                role,
                content,
                mode_used,
                notes,
                input_tokens,
                output_tokens,
                cost_usd,
                1 if cached else 0,
                sources,
                search_queries,
                pending_action,
                action_status,
                images,
                files,
                audio,
                1 if truncated else 0,
                max_output_tokens,
                1 if no_output else 0,
                code_results,
                fact_checks,
                academic_results,
                math_results,
                library_sources,
                memory_sources,
                workflow_steps,
                model,
                feedback,
                feedback_reason,
            ),
        )

        conn.execute(
            """
            UPDATE conversations
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (conversation_id,),
        )

        message_id = cursor.lastrowid

        row = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()

    return dict(row)


def get_message(message_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    return dict(row) if row else None


def append_to_message(
    conversation_id: int,
    message_id: int,
    additional_content: str,
    truncated: bool,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any] | None:
    """Extend a truncated assistant message with a continuation, scoped to
    this conversation (same defense-in-depth as set_message_bookmarked).

    Appends `additional_content` to the existing text (the model was
    instructed to continue exactly where it left off, so no separator is
    inserted) and adds the continuation's own tokens/cost on top of whatever
    was already recorded — the message's cost should reflect everything spent
    producing it, original call plus every continuation. `truncated` reflects
    only the continuation's own outcome: it can still be true if the
    continuation itself got cut off again.

    `max_output_tokens` REPLACES the stored ceiling rather than accumulating,
    unlike the token/cost columns: it describes the most recent attempt, and
    the most recent attempt is the one whose cut-off the truncation notice is
    explaining. Ignored when None, so a caller that doesn't know the ceiling
    leaves whatever was already recorded instead of erasing it.

    Returns the updated row, or None if it doesn't exist in this conversation.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT content, input_tokens, output_tokens, cost_usd, max_output_tokens "
            "FROM messages WHERE conversation_id = ? AND id = ?",
            (conversation_id, message_id),
        ).fetchone()
        if row is None:
            return None

        conn.execute(
            """
            UPDATE messages
            SET content = ?, truncated = ?, input_tokens = ?, output_tokens = ?,
                cost_usd = ?, max_output_tokens = ?
            WHERE id = ?
            """,
            (
                str(row["content"]) + additional_content,
                1 if truncated else 0,
                (row["input_tokens"] or 0) + (input_tokens or 0),
                (row["output_tokens"] or 0) + (output_tokens or 0),
                (row["cost_usd"] or 0.0) + (cost_usd or 0.0),
                (
                    max_output_tokens
                    if max_output_tokens is not None
                    else row["max_output_tokens"]
                ),
                message_id,
            ),
        )
        updated = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    return dict(updated) if updated else None


def set_action_status(message_id: int, status: str) -> dict[str, Any] | None:
    """Resolve a message's pending action (confirmed/declined/failed).

    Returns the updated row, or None if the message doesn't exist.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE messages SET action_status = ? WHERE id = ?",
            (status, message_id),
        )
        row = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    return dict(row) if row else None


def claim_pending_action(message_id: int, claimed_status: str) -> dict[str, Any] | None:
    """Atomically move a message's action_status from 'pending' to claimed_status.

    Guards against two concurrent /action requests both reading action_status
    as "pending" and both firing the webhook — the UPDATE's WHERE clause makes
    the pending->claimed transition atomic, so only the request whose UPDATE
    actually matches a row (cursor.rowcount == 1) wins the claim.

    Returns the updated row if this call won the claim, or None if the
    message doesn't exist or its action was no longer "pending".
    """
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE messages SET action_status = ? "
            "WHERE id = ? AND action_status = 'pending'",
            (claimed_status, message_id),
        )
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    return dict(row) if row else None


def set_message_bookmarked(
    conversation_id: int, message_id: int, bookmarked: bool
) -> dict[str, Any] | None:
    """Bookmark/unbookmark a single message, scoped to this conversation.

    A pure marker like conversation favorite/archived — doesn't touch the
    conversation's updated_at. Returns the updated message row, or None if
    it doesn't exist in this conversation.
    """
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE messages SET bookmarked = ? WHERE conversation_id = ? AND id = ?",
            (1 if bookmarked else 0, conversation_id, message_id),
        )
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    return dict(row) if row else None


def set_message_feedback(
    conversation_id: int,
    message_id: int,
    owner: str | None,
    verdict: int | None,
    reason: str | None,
) -> dict[str, Any] | None:
    """Set/clear a caller's 👍/👎 on a single assistant message, scoped to
    this conversation. Setting the SAME verdict that's already there clears
    it instead (same click-again-to-clear UX contract as the bookmark
    toggle) — decided here, not by the caller, so a direct API call gets the
    same behavior the UI's click-again does. Passing `verdict=None`
    explicitly always clears, regardless of what was set before.

    Always appends one feedback_log row (verdict 1/-1, or 0 for a clear)
    snapshotting the message's model/mode_used/category at rating time —
    the ledger is the analytics source of truth and must keep the signal
    even after the message row is later replaced by regenerate/edit (see
    delete_messages_after) or deleted outright, which is why message_id
    there carries no foreign-key constraint.

    A pure marker like set_message_bookmarked — never touches the
    conversation's updated_at. Returns the updated message row, or None if
    it doesn't exist in this conversation.
    """
    from .feedback import parse_mode_used

    with _connect() as conn:
        row = conn.execute(
            "SELECT feedback, mode_used, model FROM messages "
            "WHERE conversation_id = ? AND id = ?",
            (conversation_id, message_id),
        ).fetchone()
        if row is None:
            return None

        current = row["feedback"]
        effective = None if verdict is not None and current == verdict else verdict

        conn.execute(
            "UPDATE messages SET feedback = ?, feedback_reason = ? WHERE id = ?",
            (effective, reason if effective is not None else None, message_id),
        )

        mode_used = row["mode_used"]
        model = row["model"] or parse_mode_used(mode_used)[0]
        category = parse_mode_used(mode_used)[1]
        conn.execute(
            """
            INSERT INTO feedback_log
                (owner, message_id, model, mode_used, category, verdict, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner,
                message_id,
                model,
                mode_used,
                category,
                effective if effective is not None else 0,
                reason,
            ),
        )

        updated = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    return dict(updated) if updated else None


def feedback_log_entries(owner: str | None, days: int) -> list[dict[str, Any]]:
    """Every feedback_log row this owner recorded in the last `days` days
    (UTC calendar days, same window convention as usage_summary) — the raw
    material GET /v1/feedback/summary aggregates in Python (see
    app/feedback.py's summarize()); only SET/change events matter for
    quality stats, so `verdict != 0` (clears) are excluded here."""
    owner_clause = "owner IS NULL" if owner is None else "owner = ?"
    owner_params: tuple[str, ...] = () if owner is None else (owner,)
    window = f"-{max(days - 1, 0)} days"

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT model, mode_used, category, verdict, reason, created_at
            FROM feedback_log
            WHERE {owner_clause} AND verdict != 0
              AND date(created_at) >= date('now', ?)
            ORDER BY created_at
            """,
            (*owner_params, window),
        ).fetchall()
    return [dict(row) for row in rows]


def record_correction_flag(
    owner: str | None,
    message_id: int,
    model: str | None,
    mode_used: str | None,
    category: str | None,
) -> None:
    """Append one correction_log row flagging `message_id` (the PREVIOUS
    assistant answer) — see that table's CREATE TABLE comment. Called at
    most once per new user turn, only when correction_tracking.
    record_if_correction decides the turn matched a curated phrase; never
    called for an ordinary follow-up."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO correction_log (owner, message_id, model, mode_used, category)
            VALUES (?, ?, ?, ?, ?)
            """,
            (owner, message_id, model, mode_used, category),
        )


def correction_log_entries(owner: str | None, days: int) -> list[dict[str, Any]]:
    """Every correction_log row this owner recorded in the last `days` days —
    the raw material app/correction_tracking.py's summarize() aggregates in
    Python, same window convention as feedback_log_entries.

    `message_id` is included for app/retry_cost.py, which needs it to
    re-attribute a flag raised against attempt N of a retried turn back to
    that turn's ORIGINAL routing decision; correction_tracking's own
    per-model/category/lane summarize() ignores it.
    """
    owner_clause = "owner IS NULL" if owner is None else "owner = ?"
    owner_params: tuple[str, ...] = () if owner is None else (owner,)
    window = f"-{max(days - 1, 0)} days"

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT message_id, model, mode_used, category, created_at
            FROM correction_log
            WHERE {owner_clause} AND date(created_at) >= date('now', ?)
            ORDER BY created_at
            """,
            (*owner_params, window),
        ).fetchall()
    return [dict(row) for row in rows]


def assistant_message_mode_rows(owner: str | None, days: int) -> list[dict[str, Any]]:
    """(id, model, mode_used, cost_usd) for every assistant message this owner
    received in the last `days` days — the denominator app/
    correction_tracking.py's summarize() uses to turn a raw correction_log
    count into a rate per model/category/lane. Joins through conversations
    since messages has no owner column of its own, same join shape as
    tool_usage_counts.

    `id` and `cost_usd` are here for app/retry_cost.py, which uses the same
    rows as its own denominator: every answer is one turn's LAST attempt, so
    it takes the cost from here for turns that were never retried and skips
    (by id) the ones retry_log already accounts for. correction_tracking uses
    neither column.
    """
    owner_clause = "c.owner IS NULL" if owner is None else "c.owner = ?"
    owner_params: tuple[str, ...] = () if owner is None else (owner,)
    window = f"-{max(days - 1, 0)} days"

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT m.id, m.model, m.mode_used, m.cost_usd
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.role = 'assistant' AND {owner_clause}
              AND m.created_at >= date('now', ?)
            """,
            (*owner_params, window),
        ).fetchall()
    return [dict(row) for row in rows]


def record_fallback_event(
    owner: str | None, model: str, reason: str, succeeded: bool
) -> None:
    """Append one fallback_log row — see that table's CREATE TABLE comment.
    Called once per primary-model failure that triggers a fallback attempt,
    whether or not some fallback candidate went on to actually answer."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO fallback_log (owner, model, reason, succeeded)
            VALUES (?, ?, ?, ?)
            """,
            (owner, model, reason, 1 if succeeded else 0),
        )


def fallback_reason_counts(owner: str | None, days: int) -> list[dict[str, Any]]:
    """[{"reason", "count"}, ...] for this owner's fallback_log rows in the
    last `days` days, most common first — the raw material self_report.py's
    paid-fallback-causes section tallies."""
    owner_clause = "owner IS NULL" if owner is None else "owner = ?"
    owner_params: tuple[str, ...] = () if owner is None else (owner,)
    window = f"-{max(days - 1, 0)} days"

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT reason, COUNT(*) AS count
            FROM fallback_log
            WHERE {owner_clause} AND date(created_at) >= date('now', ?)
            GROUP BY reason
            ORDER BY count DESC, reason ASC
            """,
            (*owner_params, window),
        ).fetchall()
    return [{"reason": row["reason"], "count": int(row["count"])} for row in rows]


# --- Re-run attribution (see app/retry_attribution.py, app/retry_cost.py) ----
#
# Four reads and one write, all keyed on retry_log — see that table's CREATE
# TABLE comment for the column semantics these depend on.


def replaced_answer_rows(conversation_id: int, after_id: int) -> list[dict[str, Any]]:
    """The assistant message(s) a regenerate/edit of the turn at `after_id`
    is ABOUT to delete (id > after_id, oldest first), with just the routing/
    cost/rating fields re-run attribution needs.

    Must be called BEFORE delete_messages_after/delete_messages_from — after
    the delete there is nothing left to read, which is the whole problem
    retry_log exists to solve.

    Returns a list, not one row, though one is the normal case: a turn has one
    answer, and a Continue appends into that same row rather than adding
    another (see append_to_message). If some path ever leaves several
    assistant messages after one user turn, all of them are about to be
    deleted, so retry_attribution takes the first as the attempt's routing
    decision and the sum as its cost rather than losing the rest.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, mode_used, model, cost_usd, feedback, created_at
            FROM messages
            WHERE conversation_id = ? AND id > ? AND role = 'assistant'
            ORDER BY id
            """,
            (conversation_id, after_id),
        ).fetchall()
    return [dict(row) for row in rows]


def retry_turn_key(conversation_id: int, user_message_id: int) -> int | None:
    """The turn_key an existing retry_log chain already uses for the turn
    whose CURRENT user message is `user_message_id`, or None when this turn
    has no recorded attempts yet (so the caller starts a chain keyed on
    `user_message_id` itself).

    Needed because an edit deletes the user row and re-inserts it under a new
    id: matching on user_message_id (the id as of each recorded attempt)
    finds the chain again, and its turn_key keeps the whole history — original
    included — attributable to one turn. Newest match wins, so a turn edited
    twice keeps following the same chain.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT turn_key
            FROM retry_log
            WHERE conversation_id = ? AND user_message_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (conversation_id, user_message_id),
        ).fetchone()
    return int(row["turn_key"]) if row else None


def retry_log_chain(turn_key: int) -> list[dict[str, Any]]:
    """Every attempt recorded so far for one turn, oldest attempt first — how
    retry_attribution learns the next attempt_index and which attempts it has
    already recorded (so a second retry doesn't re-record the first)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, message_id, attempt_index, signal, cost_usd
            FROM retry_log
            WHERE turn_key = ?
            ORDER BY attempt_index, id
            """,
            (turn_key,),
        ).fetchall()
    return [dict(row) for row in rows]


def record_retry_attempt(
    owner: str | None,
    conversation_id: int,
    turn_key: int,
    user_message_id: int | None,
    message_id: int | None,
    attempt_index: int,
    signal: str | None,
    mode_used: str | None,
    model: str | None,
    category: str | None,
    tier: str | None,
    cost_usd: float | None,
    created_at: str | None = None,
) -> None:
    """Append one retry_log row. `created_at` defaults to now (the newest
    attempt, being recorded as it happens) but is passed explicitly for the
    attempt being REPLACED, which is recorded retroactively and must keep its
    own answer time — see the table's CREATE TABLE comment."""
    columns = [
        "owner",
        "conversation_id",
        "turn_key",
        "user_message_id",
        "message_id",
        "attempt_index",
        "signal",
        "mode_used",
        "model",
        "category",
        "tier",
        "cost_usd",
    ]
    values: list[Any] = [
        owner,
        conversation_id,
        turn_key,
        user_message_id,
        message_id,
        attempt_index,
        signal,
        mode_used,
        model,
        category,
        tier,
        cost_usd,
    ]
    if created_at is not None:
        columns.append("created_at")
        values.append(created_at)
    placeholders = ", ".join("?" for _ in columns)
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO retry_log ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(values),
        )


def retry_log_turn_rows(owner: str | None, days: int) -> list[dict[str, Any]]:
    """Every attempt of every turn this owner retried, where AT LEAST ONE of
    that turn's attempts falls in the last `days` days — whole chains, not a
    windowed slice of one.

    Whole chains deliberately: a turn answered just before the window opened
    and retried just after would otherwise report a retry with no original,
    making its first-attempt cost vanish and its retry look free. The tradeoff
    is the mirror image and is the honest one to take — such a turn's ORIGINAL
    cost is counted in a window that predates it, which overstates that
    window's first-attempt cost rather than understating its total.
    """
    owner_clause = "owner IS NULL" if owner is None else "owner = ?"
    owner_params: tuple[str, ...] = () if owner is None else (owner,)
    window = f"-{max(days - 1, 0)} days"

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT turn_key, message_id, attempt_index, signal, mode_used,
                   model, category, tier, cost_usd, created_at
            FROM retry_log
            WHERE turn_key IN (
                SELECT turn_key
                FROM retry_log
                WHERE {owner_clause} AND date(created_at) >= date('now', ?)
            )
            AND {owner_clause}
            ORDER BY turn_key, attempt_index, id
            """,
            (*owner_params, window, *owner_params),
        ).fetchall()
    return [dict(row) for row in rows]


def list_bookmarked_messages(owner: str | None) -> list[dict[str, Any]]:
    """Every bookmarked message across this owner's conversations, newest
    first, each carrying its conversation's title so a Bookmarks panel can
    link straight back to it without a second lookup.
    """
    qualified_columns = ", ".join(
        f"m.{column.strip()}" for column in _MESSAGE_COLUMNS.split(",")
    )
    owner_clause = "c.owner IS NULL" if owner is None else "c.owner = ?"
    owner_params: tuple[str, ...] = () if owner is None else (owner,)

    sql = f"""
        SELECT {qualified_columns}, c.title AS conversation_title
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.bookmarked = 1 AND {owner_clause}
        ORDER BY m.id DESC
    """

    with _connect() as conn:
        rows = conn.execute(sql, owner_params).fetchall()

    return [dict(row) for row in rows]


def delete_messages_after(conversation_id: int, after_id: int) -> int:
    """Delete messages in a conversation with id greater than after_id.

    Used by regenerate to drop the assistant answer(s) following the last user
    message before producing a fresh one. Returns the number removed.
    """
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM messages WHERE conversation_id = ? AND id > ?",
            (conversation_id, after_id),
        )
    if cursor.rowcount:
        clear_summary_cache(conversation_id)
    return cursor.rowcount


def delete_messages_from(conversation_id: int, from_id: int) -> int:
    """Delete messages in a conversation with id >= from_id (inclusive).

    Used by message-edit to drop the message being edited plus everything
    that followed it, right before the edited turn's fresh answer is
    persisted. Returns the number removed.
    """
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM messages WHERE conversation_id = ? AND id >= ?",
            (conversation_id, from_id),
        )
    if cursor.rowcount:
        clear_summary_cache(conversation_id)
    return cursor.rowcount


def delete_message(conversation_id: int, message_id: int) -> bool:
    """Delete a single message (whatever its role), scoped to this
    conversation. Unlike delete_messages_after/from, this never cascades to
    other messages — it's the primitive behind letting a user remove one
    stray message without touching the rest of the conversation. Returns
    True if a row was deleted; bumps the conversation's updated_at on success.
    """
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM messages WHERE conversation_id = ? AND id = ?",
            (conversation_id, message_id),
        )
        deleted = cursor.rowcount > 0
        if deleted:
            conn.execute(
                "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (conversation_id,),
            )

    if deleted:
        # An arbitrary message could have been in the already-summarized
        # older window; the cached summary can no longer be trusted to match
        # the real history, so drop it — the next answer just re-summarizes
        # from scratch once, same as a brand new conversation.
        clear_summary_cache(conversation_id)

    return deleted


def get_summary_cache(conversation_id: int) -> dict[str, Any] | None:
    """The cached history summary for a conversation, or None if there isn't
    one yet. `older_count` is how many older (pre-recent-window) messages
    `summary` already covers — see build_context_prompt in main.py."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT older_count, summary FROM summary_cache WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    return dict(row) if row else None


def set_summary_cache(conversation_id: int, older_count: int, summary: str) -> None:
    """Upsert the cached history summary for a conversation."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO summary_cache (conversation_id, older_count, summary, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(conversation_id) DO UPDATE SET
                older_count = excluded.older_count,
                summary = excluded.summary,
                updated_at = CURRENT_TIMESTAMP
            """,
            (conversation_id, older_count, summary),
        )


def clear_summary_cache(conversation_id: int) -> None:
    """Drop a conversation's cached history summary, if any."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM summary_cache WHERE conversation_id = ?", (conversation_id,)
        )


_TEMPLATE_COLUMNS = "id, owner, name, content, created_at, updated_at"


def list_templates(owner: str | None) -> list[dict[str, Any]]:
    """This owner's saved prompt templates, most-recently-updated first."""
    owner_clause = "owner IS NULL" if owner is None else "owner = ?"
    owner_params: tuple[str, ...] = () if owner is None else (owner,)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_TEMPLATE_COLUMNS} FROM templates
            WHERE {owner_clause}
            ORDER BY updated_at DESC, id DESC
            """,
            owner_params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_template(template_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_TEMPLATE_COLUMNS} FROM templates WHERE id = ?",
            (template_id,),
        ).fetchone()
    return dict(row) if row else None


def create_template(owner: str | None, name: str, content: str) -> dict[str, Any]:
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO templates (owner, name, content) VALUES (?, ?, ?)",
            (owner, name, content),
        )
        row = conn.execute(
            f"SELECT {_TEMPLATE_COLUMNS} FROM templates WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    assert row is not None
    return dict(row)


def update_template(
    template_id: int, name: str | None = None, content: str | None = None
) -> dict[str, Any] | None:
    """Update whichever of name/content is given. Returns None if not found."""
    with _connect() as conn:
        if name is not None:
            conn.execute(
                "UPDATE templates SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (name, template_id),
            )
        if content is not None:
            conn.execute(
                "UPDATE templates SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (content, template_id),
            )
        row = conn.execute(
            f"SELECT {_TEMPLATE_COLUMNS} FROM templates WHERE id = ?",
            (template_id,),
        ).fetchone()
    return dict(row) if row else None


def delete_template(template_id: int) -> bool:
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
    return cursor.rowcount > 0


def list_messages(conversation_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM messages
            WHERE conversation_id = ?
            ORDER BY id ASC
            """,
            (conversation_id,),
        ).fetchall()

    return [dict(row) for row in rows]


def _escape_like(term: str) -> str:
    # Escape SQLite LIKE wildcards in user input so "50%" or "a_b" search
    # literally rather than as glob patterns.
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_conversations(
    owner: str | None, query: str, limit: int = 30
) -> list[dict[str, Any]]:
    """Search conversation titles and message content, owner-scoped.

    Returns one row per matching conversation (title match or newest
    matching message), each with a `snippet` of the matched text.
    """
    pattern = f"%{_escape_like(query)}%"
    owner_clause = "c.owner IS NULL" if owner is None else "c.owner = ?"
    owner_params: tuple[str, ...] = () if owner is None else (owner,)

    sql = f"""
        SELECT
            c.id, c.title, c.owner, c.pinned_model, c.created_at, c.updated_at,
            (
                SELECT m.content FROM messages m
                WHERE m.conversation_id = c.id AND m.content LIKE ? ESCAPE '\\'
                ORDER BY m.id DESC LIMIT 1
            ) AS matched_content
        FROM conversations c
        WHERE {owner_clause}
          AND (
                c.title LIKE ? ESCAPE '\\'
                OR EXISTS (
                    SELECT 1 FROM messages m
                    WHERE m.conversation_id = c.id AND m.content LIKE ? ESCAPE '\\'
                )
          )
        ORDER BY c.updated_at DESC, c.id DESC
        LIMIT ?
    """
    params = (pattern, *owner_params, pattern, pattern, limit)

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    results = []
    for row in rows:
        item = dict(row)
        matched_content = item.pop("matched_content")
        item["snippet"] = (
            matched_content if matched_content is not None else item["title"]
        )
        results.append(item)

    return results


# --- Client-side crash reports ----------------------------------------------

# Stored-field caps: applied HERE (truncation on insert), not as pydantic
# validation caps that would 422 an oversized report away — a crash report
# losing its tail is fine, a crash report rejected entirely defeats the whole
# point of the endpoint (see app/routers/system.py's report_client_error).
_CLIENT_ERROR_MESSAGE_MAX_CHARS = 4_000
_CLIENT_ERROR_STACK_MAX_CHARS = 30_000
_CLIENT_ERROR_URL_MAX_CHARS = 2_000
_CLIENT_ERROR_USER_AGENT_MAX_CHARS = 1_000

# Debugging aid, not a ledger: keep only the newest N rows so an error loop
# on some device can't grow the database unboundedly through an
# unauthenticated endpoint.
_CLIENT_ERRORS_MAX_ROWS = 500


def record_client_error(
    message: str,
    stack: str | None,
    source_url: str | None,
    user_agent: str | None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO client_errors (message, stack, source_url, user_agent)
            VALUES (?, ?, ?, ?)
            """,
            (
                message[:_CLIENT_ERROR_MESSAGE_MAX_CHARS],
                stack[:_CLIENT_ERROR_STACK_MAX_CHARS] if stack else None,
                source_url[:_CLIENT_ERROR_URL_MAX_CHARS] if source_url else None,
                user_agent[:_CLIENT_ERROR_USER_AGENT_MAX_CHARS] if user_agent else None,
            ),
        )
        conn.execute(
            """
            DELETE FROM client_errors
            WHERE id NOT IN (
                SELECT id FROM client_errors ORDER BY id DESC LIMIT ?
            )
            """,
            (_CLIENT_ERRORS_MAX_ROWS,),
        )


def list_client_errors(limit: int = 50) -> list[dict[str, Any]]:
    """Newest first — the whole point is 'what just went wrong on that
    device'."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, message, stack, source_url, user_agent, created_at
            FROM client_errors
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
