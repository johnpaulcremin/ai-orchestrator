"""Semantic (near-duplicate) response caching — an ADDITIONAL, opt-in layer on
top of the exact-match response cache (app/cache.py). Where the exact cache
only fires on a byte-identical prompt, this also catches paraphrases
("what's the capital of France?" vs "capital of france?") via embedding
similarity, using OpenAI's embeddings API (no new key — bills through the
existing OPENAI_API_KEY).

Deliberately scoped far narrower than the exact cache, because the failure
mode is worse: a wrong EXACT-cache hit can't happen by construction (the key
IS the prompt), but a wrong SEMANTIC hit means serving a plausible-but-wrong
answer for a question that only sounded similar — worse than a miss, which
just costs one ordinary model call. Two guardrails:

1. CONTEXT-FREE ONLY. The caller (see orchestrator.run_orchestrator/
   stream_orchestrator's `context_free` param) only ever offers this a
   question that has NO conversation history and NO custom system prompt
   behind it — i.e. exactly the case where main._assemble_context_parts's
   early return makes `req.question` literally just the bare question, with
   nothing else folded in. Once a conversation has history, the "question"
   becomes an assembled context blob (system + history + question), and two
   similar-LOOKING blobs can still imply different answers (different
   history = different referents for "that", "it", "the previous one", ...).
   Rather than try to detect that from text, semantic matching is simply
   never offered a context-bearing prompt in the first place.
2. HIGH THRESHOLD, OFF BY DEFAULT. SEMANTIC_CACHE=true is required (unlike
   the exact cache's default-on RESPONSE_CACHE) and the default similarity
   threshold (SEMANTIC_CACHE_THRESHOLD=0.96 — cosine similarity is roughly
   0-1 for same-sign embeddings, and real paraphrases of the same question
   typically score noticeably higher than merely-related-topic text at this
   embedding size) is deliberately conservative. Widen it only after you've
   confirmed on your own traffic that it isn't producing wrong hits.

Same isolation boundaries as the exact cache otherwise: scoped by mode +
resolved model-config signature + owner (see cache._config_signature/
make_key), so a semantic hit can never cross a routing-config change or leak
between users.

No vector DB dependency: embeddings are stored as JSON float arrays and
matched with a brute-force cosine scan in Python — correct for a personal,
local-first deployment where SEMANTIC_CACHE_MAX_ENTRIES (default 200) keeps
that scan trivially fast; a real index would be needed at a much larger
scale, deliberately out of scope here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from typing import Any

from . import database
from .cache import _config_signature, library_generation
from .settings import bool_setting
from .telemetry import logger


def enabled() -> bool:
    """Opt-in: SEMANTIC_CACHE=true (env, or a saved Settings override — same
    override > env > default chain as any other feature flag). Off by
    default — unlike the exact cache, this changes which answers get served
    (paraphrase matching, not just identical-text matching), so it needs an
    explicit opt-in."""
    return bool_setting("SEMANTIC_CACHE", False)


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def threshold() -> float:
    """Minimum cosine similarity to count as a match. Deliberately high by
    default — see module docstring on why a false positive here is worse
    than the exact cache's failure mode (which doesn't exist)."""
    value = _float_env("SEMANTIC_CACHE_THRESHOLD", 0.96)
    return value if 0.0 < value <= 1.0 else 0.96


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def max_entries() -> int:
    """Cap on stored embeddings; kept small by default since lookups are a
    brute-force scan over every stored entry (see module docstring)."""
    value = _int_env("SEMANTIC_CACHE_MAX_ENTRIES", 200)
    return value if value > 0 else 200


def _embedding_model() -> str:
    return (os.getenv("SEMANTIC_CACHE_EMBEDDING_MODEL") or "").strip() or (
        "text-embedding-3-small"
    )


def _scope_key(mode: str, owner: str | None) -> str:
    """Groups embeddings the same way cache.make_key scopes exact-match
    entries (mode + resolved model-config + owner + that owner's library
    generation) — WITHOUT folding in the question text, since that's exactly
    what a semantic lookup needs to compare fuzzily rather than exactly.

    The library generation belongs here for the same reason it belongs in
    cache.make_key (see cache.library_generation): this cache is MORE exposed
    to library staleness, not less, since a merely-similar question can hit
    an entry answered under a different library."""
    raw = "\x1f".join(
        [mode, _config_signature(), owner or "", library_generation(owner)]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# Cap on cached embedding vectors (shared by every caller of embed() — the
# semantic response cache and cross-conversation memory alike), evicted
# oldest-first once exceeded. Kept generous relative to SEMANTIC_CACHE_MAX_
# ENTRIES/MEMORY_MAX_ENTRIES since a repeated *question* across those two
# features should still hit this cache even after either one has evicted its
# own higher-level entry.
_EMBEDDING_CACHE_MAX_ENTRIES = 2000


def _embedding_cache_key(model: str, text: str) -> str:
    raw = "\x1f".join([model, text])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def embed(text: str) -> list[float] | None:
    """The embedding vector for `text`, or None on any failure (missing key,
    timeout, provider error) — this is a best-effort auxiliary call, same
    fail-safe contract as orchestrator.summarize_text, never a hard
    dependency for answering.

    Backed by a persistent cache keyed on (embedding model, exact text): the
    same question asked twice — by a user repeating themselves, or by the
    semantic cache and cross-conversation memory both embedding the same
    turn — costs one embeddings-API call instead of two. Cache misses still
    cost a call; this never widens what counts as a "match" the way raising
    SEMANTIC_CACHE_THRESHOLD would — it only dedupes the identical-text case.
    """
    from .orchestrator import get_client

    clean = (text or "").strip()
    if not clean:
        return None

    model = _embedding_model()
    cache_key = _embedding_cache_key(model, clean)
    try:
        cached = database.embedding_cache_get(cache_key)
    except sqlite3.Error:
        cached = None
    if cached is not None:
        try:
            return [float(x) for x in json.loads(cached)]
        except (TypeError, ValueError):
            pass  # Corrupt row: fall through and re-embed.

    try:
        client = get_client()
    except RuntimeError:
        return None
    try:
        # Fail fast (no SDK retries) with a short timeout, same as the
        # router-classifier/summarizer auxiliary calls — this sits on the
        # pre-answer critical path and must never stall it for long.
        timeout_client = client.with_options(timeout=8.0, max_retries=0)
        response = timeout_client.embeddings.create(input=clean, model=model)
        vector = list(response.data[0].embedding)
    except Exception:
        logger.warning("semantic_cache.embed_failed", exc_info=True)
        return None

    try:
        database.embedding_cache_put(cache_key, json.dumps(vector))
        count = database.embedding_cache_count()
        if count > _EMBEDDING_CACHE_MAX_ENTRIES:
            database.embedding_cache_delete_oldest(count - _EMBEDDING_CACHE_MAX_ENTRIES)
    except sqlite3.Error:
        pass  # Best-effort: a failed cache write must not fail the caller.
    return vector


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def find(
    question: str, mode: str, owner: str | None
) -> tuple[dict[str, Any] | None, list[float] | None]:
    """(hit, vector). `hit` is the best above-threshold match (with a
    `similarity` field added), or None if nothing cleared the bar (or
    embedding failed, or no candidates exist). `vector` is the just-computed
    embedding of `question` — callers should pass it to put() on a miss
    rather than embedding the same text a second time.
    """
    if not enabled():
        return None, None
    vector = embed(question)
    if vector is None:
        return None, None
    scope = _scope_key(mode, owner)
    try:
        candidates = database.semantic_cache_list(scope)
    except sqlite3.Error:
        return None, vector
    best: dict[str, Any] | None = None
    best_score = 0.0
    for row in candidates:
        try:
            candidate_vector = json.loads(str(row["embedding"]))
        except (TypeError, ValueError):
            continue
        score = _cosine_similarity(vector, candidate_vector)
        if score > best_score:
            best_score = score
            best = row
    if best is not None and best_score >= threshold():
        return {**best, "similarity": best_score}, vector
    return None, vector


def put(
    question: str,
    mode: str,
    owner: str | None,
    vector: list[float] | None,
    answer: str,
    mode_used: str | None,
    notes: str | None,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float | None,
) -> None:
    """Store a fresh answer's embedding for future semantic matching.

    No-op if semantic caching is off, there's no answer text, or `vector` is
    None (embedding failed during the find() call this reuses — never
    embeds a second time just to write).
    """
    if not enabled() or not (answer or "").strip() or vector is None:
        return
    scope = _scope_key(mode, owner)
    try:
        database.semantic_cache_put(
            scope,
            question,
            json.dumps(vector),
            answer,
            mode_used,
            notes,
            model,
            input_tokens,
            output_tokens,
            cost_usd,
        )
        cap = max_entries()
        if cap:
            count = database.semantic_cache_count()
            if count > cap:
                database.semantic_cache_delete_oldest(count - cap)
    except sqlite3.Error:
        # Best-effort: a failed cache write must not fail the request.
        return


def clear() -> int:
    try:
        return database.semantic_cache_clear()
    except sqlite3.Error:
        return 0


def stats() -> dict[str, Any]:
    try:
        entries = database.semantic_cache_count()
    except sqlite3.Error:
        entries = 0
    return {
        "enabled": enabled(),
        "entries": entries,
        "threshold": threshold(),
        "max_entries": max_entries(),
    }
