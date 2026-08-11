"""RAG document library: an opt-in, per-owner set of reference documents the
model can automatically draw on, distinct from a per-message attachment
(which only exists for that one turn — see schemas.FileAttachment). Same
"no vector DB, brute-force cosine scan, OpenAI embeddings" approach as
app/semantic_cache.py and app/memory.py (embed/_cosine_similarity are reused
from semantic_cache directly, not duplicated).

A document is: extracted to plain text (extract_text), split into
overlapping chunks (chunk_text), each chunk embedded and stored
(app/routers/library.py's upload endpoint does the extract/chunk/embed/store
sequence). A question is matched against every stored chunk for that owner
(retrieve) and the best few injected as extra context (context_block) — the
same "recalled context, model uses its own judgment on relevance" design
memory.py uses for cross-conversation recall, just over uploaded documents
instead of past conversations.

Deliberately conversation-independent (unlike memory, which excludes the
calling conversation): a document you uploaded is relevant to every
conversation equally, there's no "exclude this one" case here.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import database
from .semantic_cache import _cosine_similarity, embed
from .settings import bool_setting

__all__ = [
    "APP_DOCS_DIR",
    "app_doc_files",
    "chunk_text",
    "embed",
    "extract_text",
    "format_chunk",
    "hybrid_retrieval_enabled",
    "min_similarity",
    "rag_library_enabled",
    "recall",
    "retrieve",
    "summarize_sources",
    "top_k",
]

# The repo's own docs/*.md, seedable into an owner's library (see
# app/routers/library.py's POST /v1/library/seed-app-docs) so a conceptual
# "how does routing work?" question can retrieve this app's REAL
# documentation via the normal library-recall path, instead of only the
# self_describe tool's terse JSON snapshot (see self_describe.py's module
# docstring). app/rag_library.py -> repo root -> docs/.
APP_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"

# Standard Okapi BM25 constants: k1 damps how fast repeated occurrences of a
# term stop helping, b how hard a long chunk is penalised for its length.
# 1.5/0.75 are the usual defaults and there is nothing about this corpus that
# argues for tuning them.
_BM25_K1 = 1.5
_BM25_B = 0.75

# The RRF damping constant from the original paper (Cormack et al. 2009). Large
# relative to top_k(), so the gap between rank 1 and rank 2 stays small and a
# chunk needs to place well in BOTH rankings to beat one that placed very high
# in a single ranking.
_RRF_K = 60

# Sub-tokens kept joined by -, _ and . so the identifiers this whole lexical
# pass exists to catch survive tokenisation intact: `rag_library.py`, `E-4302`
# and `0.3.0` are each one rare term, not three common ones.
_TOKEN = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def app_doc_files() -> list[tuple[str, str]]:
    """(filename, text) for every docs/*.md file, sorted by filename for a
    stable, deterministic seed order. Empty list if the docs directory
    doesn't exist (e.g. an installed package without the repo's docs/
    folder alongside it) — never raises."""
    if not APP_DOCS_DIR.is_dir():
        return []
    return [
        (path.name, path.read_text(encoding="utf-8", errors="replace"))
        for path in sorted(APP_DOCS_DIR.glob("*.md"))
    ]


def rag_library_enabled() -> bool:
    """Opt-in: RAG_LIBRARY=true (env, or a saved Settings override — same
    override > env > default chain as any other toggle). Off by default:
    this changes what gets folded into every conversation's prompt, the same
    class of behavior change as CROSS_CONVERSATION_MEMORY/SEMANTIC_CACHE, so
    it needs an explicit opt-in rather than being silently on."""
    return bool_setting("RAG_LIBRARY", False)


def hybrid_retrieval_enabled() -> bool:
    """On by default, unlike RAG_LIBRARY itself — and that asymmetry is the
    whole justification. This flag only does anything when RAG_LIBRARY is
    already on, and RAG_LIBRARY is off by default, so defaulting this one to
    true changes the behaviour of no existing deployment that has not already
    opted into retrieval. An operator who has opted in gets the better recall
    without a second switch to find; one who measures the fusion displacing
    hits they wanted can turn it off and get the pure-vector ranking back
    exactly (see retrieve's note on displacement).

    Costs nothing to leave on: BM25 is computed locally over chunks already
    loaded for the vector scan — no model call, no tokens, no new dependency.
    """
    return bool_setting("RAG_HYBRID_RETRIEVAL", True)


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def min_similarity() -> float:
    """Minimum cosine similarity to count as relevant enough to inject.
    Looser than semantic_cache's 0.96 (an exact-answer match) but tighter
    than memory's 0.75 (a whole past exchange) since a document chunk is a
    narrower, more literal piece of text to match against."""
    value = _float_env("RAG_MIN_SIMILARITY", 0.30)
    return value if 0.0 < value <= 1.0 else 0.30


def top_k() -> int:
    """How many chunks to inject at most, when relevant ones exist."""
    value = _int_env("RAG_TOP_K", 4)
    return value if value > 0 else 4


# The chunk size/overlap the spec calls for: long enough for a chunk to carry
# real standalone context, with enough overlap that a fact sitting right at a
# chunk boundary isn't split across two chunks with neither containing it whole.
_CHUNK_SIZE = 1_000
_CHUNK_OVERLAP = 150


def chunk_text(
    text: str, chunk_size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP
) -> list[str]:
    """Split `text` into overlapping chunks, each up to `chunk_size` chars,
    advancing by `chunk_size - overlap` each step. Pure and deterministic —
    the same text always produces the same chunks, no randomness/timestamps
    involved. [] for empty/whitespace-only text."""
    if chunk_size <= overlap:
        raise ValueError("chunk_size must be greater than overlap")
    cleaned = text.strip()
    if not cleaned:
        return []
    chunks: list[str] = []
    start = 0
    length = len(cleaned)
    step = chunk_size - overlap
    while start < length:
        end = min(start + chunk_size, length)
        piece = cleaned[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= length:
            break
        start += step
    return chunks


def _extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader  # deferred: only needed for a PDF upload

    reader = PdfReader(io.BytesIO(data))
    parts = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(part for part in parts if part.strip())


def extract_text(mime_type: str, data: bytes) -> str:
    """Plain text from a document's raw bytes. PDF is parsed locally via
    pypdf (unlike the per-message attachment path in app/providers.py, which
    sends PDF bytes to Anthropic/OpenAI to parse server-side — the library
    needs the text itself, up front, to chunk and embed it). Anything else
    (text/plain) is decoded as UTF-8, replacing undecodable bytes rather
    than raising — a document that's mostly readable shouldn't be rejected
    outright over a handful of bad bytes."""
    if mime_type == "application/pdf":
        return _extract_pdf_text(data)
    return data.decode("utf-8", errors="replace")


def _vector_ranking(
    question_vector: list[float], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Chunks at or above min_similarity(), best cosine first — the retrieval
    this module has always done, unchanged and still used on its own when
    RAG_HYBRID_RETRIEVAL is off."""
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in candidates:
        try:
            candidate_vector = json.loads(str(row["embedding"]))
        except (TypeError, ValueError):
            continue
        score = _cosine_similarity(question_vector, candidate_vector)
        if score >= min_similarity():
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _score, row in scored]


def _lexical_ranking(
    question: str, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Chunks ranked by Okapi BM25 against the raw question, best first.

    Scored over the same brute-force scan of the owner's chunks the vector
    pass already does — no index, deliberately, so this inherits exactly the
    scaling limit documented on that pass rather than introducing a second,
    different one.

    A chunk qualifies only if it matches at least one term appearing in half
    the corpus or less. IDF already discounts a common term toward zero, but
    on a library of a dozen chunks "the" is not common enough for that to
    bite, and a chunk that matched nothing but stopwords would otherwise be
    offered to the fusion as a real lexical hit.
    """
    tokens = _tokenize(question)
    if not tokens:
        return []
    documents = [(row, _tokenize(str(row["text"]))) for row in candidates]
    documents = [(row, terms) for row, terms in documents if terms]
    if not documents:
        return []

    total = len(documents)
    average_length = sum(len(terms) for _row, terms in documents) / total
    frequencies = [(row, Counter(terms), len(terms)) for row, terms in documents]

    scored: list[tuple[float, dict[str, Any]]] = []
    for row, counts, length in frequencies:
        score = 0.0
        informative = False
        for term in set(tokens):
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            document_frequency = sum(1 for _r, c, _l in frequencies if term in c)
            if document_frequency * 2 <= total:
                informative = True
            idf = math.log(
                1 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            denominator = frequency + _BM25_K1 * (
                1 - _BM25_B + _BM25_B * length / average_length
            )
            score += idf * frequency * (_BM25_K1 + 1) / denominator
        if score > 0 and informative:
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _score, row in scored]


def _fuse(*rankings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reciprocal rank fusion: each chunk scores sum(1/(_RRF_K + rank)) across
    the rankings it appears in, best first.

    RRF rather than a weighted sum of the two scores because cosine
    similarity and BM25 are on unrelated scales — one is bounded in [-1, 1],
    the other is unbounded and grows with corpus size — so any fixed weighting
    of the raw numbers would silently re-tune itself as a library grew. Rank
    is the only thing the two agree on.
    """
    fused: dict[Any, float] = {}
    rows: dict[Any, dict[str, Any]] = {}
    for ranking in rankings:
        for rank, row in enumerate(ranking, start=1):
            key = row["id"]
            fused[key] = fused.get(key, 0.0) + 1 / (_RRF_K + rank)
            rows.setdefault(key, row)
    order = sorted(fused, key=lambda key: fused[key], reverse=True)
    return [rows[key] for key in order]


def retrieve(
    question_vector: list[float] | None,
    owner: str | None,
    question: str | None = None,
) -> list[dict[str, Any]]:
    """Up to top_k() chunks from this owner's library, best match first. [] if
    the feature is off, `question_vector` is None (embedding failed, or the
    caller skipped it), or nothing clears the bar (including an empty library
    — never engages when there's nothing uploaded). Never raises: a broken
    retrieval must not break answering.

    With RAG_HYBRID_RETRIEVAL on (the default) and `question` supplied, a BM25
    pass over the same chunks is fused into the ranking. This is what finds a
    chunk the embedding pass cannot: an exact identifier — an error code, a
    version string, a function name — is a rare token BM25 scores highly and a
    sentence embedding largely averages away, so "what does E4302 mean" would
    retrieve nothing while the one chunk defining E4302 sat in the library.

    The tradeoff is real and worth stating: fusion can displace a vector hit
    that would previously have made the cut, since both lists compete for the
    same top_k() slots. That is the point — a lexical hit only outranks it by
    also appearing high in its own list — but it does mean this is not a pure
    superset of the old behaviour, which is why the flag exists.

    `question` defaults to None (vector-only, exactly the previous behaviour)
    so no caller is obliged to pass it.
    """
    if not rag_library_enabled() or question_vector is None:
        return []
    try:
        candidates = database.library_chunks_list(owner)
    except sqlite3.Error:
        return []

    vector_hits = _vector_ranking(question_vector, candidates)
    if not question or not hybrid_retrieval_enabled():
        return vector_hits[: top_k()]
    lexical_hits = _lexical_ranking(question, candidates)
    if not lexical_hits:
        return vector_hits[: top_k()]
    return _fuse(vector_hits, lexical_hits)[: top_k()]


def format_chunk(chunk: dict[str, Any]) -> str:
    """A recalled chunk as the "[filename]\\ntext" snippet folded into the
    prompt — mirrors memory.format_snippet's shape so context_builder's
    _library_block can wrap a list of these in the same framing
    _memory_block uses for recalled memory snippets."""
    return f"[{chunk['filename']}]\n{chunk['text']}"


def recall(
    question: str, owner: str | None
) -> tuple[list[str], list[dict[str, Any]], int]:
    """(snippets, sources, duration_ms) for one question: the whole
    embed -> retrieve -> format pipeline, in the one place that owns it.

    `snippets` are format_chunk strings for the prompt, `sources` is the
    answer's `library_sources` provenance summary, and `duration_ms` is how
    long this took (folded into the per-stage latency log as `library_embed`
    by the caller). ([], [], ~0) when RAG_LIBRARY is off — the embed call is
    skipped entirely rather than computed and discarded.

    WHETHER to call this at all is the caller's decision, not this function's:
    orchestrator._recall_library_context gates it on the classifier's task
    category (see categories.retrieval_helps), which this module has no
    business knowing about.
    """
    started = time.perf_counter()
    if not rag_library_enabled():
        return [], [], int((time.perf_counter() - started) * 1000)
    vector = embed(question)
    chunks = retrieve(vector, owner, question)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return (
        [format_chunk(chunk) for chunk in chunks],
        summarize_sources(chunks),
        duration_ms,
    )


def summarize_sources(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{"document": filename, "snippet_count": n}], one entry per distinct
    source document, in first-seen (best-match) order — the answer's
    library_sources field, so the UI can show which documents were actually
    used without exposing every raw chunk."""
    counts: dict[str, int] = {}
    order: list[str] = []
    for chunk in chunks:
        filename = str(chunk["filename"])
        if filename not in counts:
            counts[filename] = 0
            order.append(filename)
        counts[filename] += 1
    return [{"document": name, "snippet_count": counts[name]} for name in order]
