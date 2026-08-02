"""Optional fact-check lookup: a standalone, heuristic-triggered call to
Google's Fact Check Tools API (free, no LLM tokens involved) that surfaces
existing published fact-checks (Snopes, PolitiFact, Reuters Fact Check, ...)
relevant to a claim the user is asking to verify.

Independent of which model answers the question — same "standalone call
gated by a phrase heuristic" design as the Gemini/Imagen image-generation
path (see orchestrator_tools._looks_like_image_request), since neither
OpenAI nor Anthropic offers a hosted tool for this the model could call
itself, and this app has no client-side tool-execution loop to hand a model
a tool that isn't hosted server-side by the provider.

Genuinely different from web search: web_search is general retrieval (the
model decides what to look up and reads whatever page content it finds);
this queries a STRUCTURED database of claims ALREADY reviewed by
professional fact-checkers, returning a claim/rating/publisher/url per hit
rather than raw page content the model has to interpret itself.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .settings import bool_setting
from .telemetry import logger

_FACT_CHECK_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"

# A claim can turn up dozens of loosely related reviews; only the first few
# are worth surfacing to the user.
_MAX_RESULTS = 5


def fact_check_enabled() -> bool:
    """Opt-in: FACT_CHECK=true (env, or a saved Settings override — same
    override > env > default chain as any other toggle). Off by default,
    same as every other tool here that reaches an external service."""
    return bool_setting("FACT_CHECK", False)


def _api_key() -> str:
    return (os.getenv("GOOGLE_FACT_CHECK_API_KEY") or "").strip()


# A deliberately narrow, high-precision phrase list — same design as
# orchestrator_tools._IMAGE_REQUEST_PHRASES: errs toward missing a request
# over over-triggering an extra external call for an ordinary question.
#
# BUG HISTORY (found by evals/tests/test_fact_check.py's adversarial trap
# fixtures): a bare "is this claim" phrase used to be in this list and
# false-positived on any unrelated "claim" noun-phrase sentence containing
# it as a literal substring -- e.g. "is this claim form filled out
# correctly for my insurance?" is not a fact-check request at all, but
# "is this claim form..." contains the substring "is this claim". Removed
# rather than narrowed: "verify this claim"/"verify the claim" already
# cover the unambiguous phrasing, and "is it true that"/"is this true"/
# "true or false" already cover the general "is X true" intent, so nothing
# else in this list relies on the bare "claim" noun to catch a genuine
# fact-check request.
_FACT_CHECK_PHRASES = (
    "fact check",
    "fact-check",
    "factcheck",
    "is it true that",
    "is it true this",
    "is this true",
    "is that true",
    "true or false",
    "debunk",
    "verify this claim",
    "verify the claim",
    "did this really happen",
    "did that really happen",
    "is this a hoax",
    "is that a hoax",
)


def looks_like_fact_check_request(question: str) -> bool:
    """Errs toward missing a request over over-triggering an extra call."""
    text = " ".join((question or "").lower().split())
    return any(phrase in text for phrase in _FACT_CHECK_PHRASES)


def check_claim(query: str) -> list[dict[str, Any]]:
    """Up to _MAX_RESULTS published fact-checks matching `query`, as
    {"claim", "rating", "publisher", "url"} dicts, best-effort ordered by
    the API's own relevance ranking. [] if GOOGLE_FACT_CHECK_API_KEY isn't
    set, the query is blank, or the call fails. Never raises: this is an
    enrichment, not worth failing the answer over. Only http(s) URLs are
    kept — the same single choke point every other link-bearing field in
    this app (citations, action webhooks) filters through, since a
    javascript:/data: URL would otherwise reach a rendered `<a href>`
    unescaped by React's text-only escaping.
    """
    api_key = _api_key()
    clean_query = (query or "").strip()
    if not api_key or not clean_query:
        return []
    try:
        response = httpx.get(
            _FACT_CHECK_URL,
            params={"query": clean_query, "key": api_key, "languageCode": "en"},
            timeout=8.0,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        logger.warning("fact_check.lookup_failed", exc_info=True)
        return []

    results: list[dict[str, Any]] = []
    try:
        for claim in data.get("claims", None) or []:
            claim_text = str(claim.get("text", "") or "").strip()
            for review in claim.get("claimReview", None) or []:
                url = str(review.get("url", "") or "").strip()
                if not url.lower().startswith(("http://", "https://")):
                    continue
                publisher = review.get("publisher") or {}
                results.append(
                    {
                        "claim": claim_text
                        or str(review.get("title", "") or "").strip()
                        or "(claim text unavailable)",
                        "rating": str(review.get("textualRating", "") or "").strip()
                        or None,
                        "publisher": str(publisher.get("name", "") or "").strip()
                        or None,
                        "url": url,
                    }
                )
                if len(results) >= _MAX_RESULTS:
                    return results
    except (AttributeError, TypeError):
        logger.warning("fact_check.parse_failed", exc_info=True)
        return []
    return results


def format_note(count: int) -> str:
    return (
        "Found a related fact-check."
        if count == 1
        else f"Found {count} related fact-checks."
    )
