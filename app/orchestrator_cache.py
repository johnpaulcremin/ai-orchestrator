"""Response-cache and semantic-cache plumbing shared by run_orchestrator and
stream_orchestrator: whether a request's shape is cacheable at all, the exact
cache key, and the AskResponse/notes built from an exact or semantic cache
hit."""

from __future__ import annotations

from . import cache
from .schemas import AskRequest, AskResponse


def _cacheable_shape(req: AskRequest) -> bool:
    """Whether this request's SHAPE allows caching at all — independent of
    whether any particular cache backend is turned on. False when:
    - a model is forced (a key/embedding doesn't encode it, so caching would
      read or poison the normally-routed entry);
    - no_cache is set (e.g. regenerate) — a one-off fresh answer must neither
      be served from nor written into any cache; or
    - the request has attached images or files — the cacheable text is
      question-only, so it can't distinguish "this question" from "this
      question + this photo/document", and the answer's correctness depends
      on the attachment's content, not just the text; or
    - research mode is on — forcing a live web search must never be served
      from (or overwrite) a cache entry answered without one.

    Shared by both the exact cache (_cache_key) and the semantic cache
    (gated separately on this AND its own SEMANTIC_CACHE flag) so neither
    backend's on/off toggle accidentally gates the other.
    """
    return not (req.model or req.no_cache or req.images or req.files or req.research)


def _cache_key(req: AskRequest, owner: str | None = None) -> str | None:
    """The exact-cache key for this request, or None when it should be
    skipped — either the request's shape disqualifies any caching
    (_cacheable_shape) or the exact cache itself is off (RESPONSE_CACHE).

    `owner` is folded into the key (see cache.make_key) so the cache is
    scoped per-user in a JWT multi-user deployment, not a cross-user oracle.
    """
    if not cache.enabled() or not _cacheable_shape(req):
        return None
    return cache.make_key(req.question, req.mode.value, owner)


def _cached_hit_note(hit: dict, meta: object, ms: int) -> str:
    original = hit.get("mode_used") or "?"
    saved = hit.get("cost_usd")
    saved_note = (
        f", saved≈${saved:.4f}" if isinstance(saved, (int, float)) and saved else ""
    )
    return (
        f"Served from response cache (originally {original}{saved_note}) "
        f"| request_id={getattr(meta, 'request_id', '?')} | ms={ms}"
    )


def _cached_response(hit: dict, meta: object, ms: int) -> AskResponse:
    return AskResponse(
        answer=str(hit.get("answer") or ""),
        mode_used=str(hit.get("mode_used") or "cache"),
        notes=_cached_hit_note(hit, meta, ms),
        model=str(hit.get("model") or "") or None,
        cost_usd=0.0,
        cached=True,
    )


def _semantic_cached_hit_note(hit: dict, meta: object, ms: int) -> str:
    original = hit.get("mode_used") or "?"
    saved = hit.get("cost_usd")
    saved_note = (
        f", saved≈${saved:.4f}" if isinstance(saved, (int, float)) and saved else ""
    )
    similarity = hit.get("similarity")
    similarity_note = (
        f"{similarity:.3f}" if isinstance(similarity, (int, float)) else "?"
    )
    return (
        f"Served from semantic cache (similarity={similarity_note}, "
        f"originally {original}{saved_note}) "
        f"| request_id={getattr(meta, 'request_id', '?')} | ms={ms}"
    )


def _semantic_cached_response(hit: dict, meta: object, ms: int) -> AskResponse:
    return AskResponse(
        answer=str(hit.get("answer") or ""),
        mode_used=str(hit.get("mode_used") or "semantic_cache"),
        notes=_semantic_cached_hit_note(hit, meta, ms),
        model=str(hit.get("model") or "") or None,
        cost_usd=0.0,
        cached=True,
    )
