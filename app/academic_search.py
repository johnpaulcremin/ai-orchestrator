"""Optional academic-search lookup: a standalone, heuristic-triggered call to
OpenAlex (free, no API key required, no LLM tokens involved) that surfaces
scholarly literature relevant to a research question.

Same "standalone call gated by a phrase heuristic" design as fact_check.py
(see that module's docstring for the full rationale) — neither OpenAI nor
Anthropic offers a hosted tool for scholarly-literature search the model
could call itself, and this app has no client-side tool-execution loop.

OpenAlex (https://openalex.org) was chosen over Semantic Scholar because it
never requires a key at all (Semantic Scholar's unauthenticated tier is
rate-limited and recommends a key for sustained use) — the simpler, fully
keyless option the spec asked to prefer. Check OpenAlex's current terms if
you need higher throughput than its default "polite pool" (identified via a
mailto contact) offers.
"""

from __future__ import annotations

from typing import Any

import httpx

from .settings import bool_setting
from .telemetry import logger

_OPENALEX_URL = "https://api.openalex.org/works"

# A search can turn up hundreds of loosely related works; only the first few
# are worth surfacing to the user.
_MAX_RESULTS = 5

_MAX_AUTHORS_SHOWN = 3
_MAX_SNIPPET_CHARS = 240


def academic_search_enabled() -> bool:
    """Opt-in: ACADEMIC_SEARCH=true (env, or a saved Settings override — same
    override > env > default chain as any other toggle). Off by default,
    same as every other tool here that reaches an external service."""
    return bool_setting("ACADEMIC_SEARCH", False)


# A deliberately narrow, high-precision phrase list — same design as
# fact_check._FACT_CHECK_PHRASES: errs toward missing a request over
# over-triggering an extra external call for an ordinary question. Notably
# excludes the bare word "research" (as in "research my competitors"),
# which is not a request for scholarly literature.
_ACADEMIC_SEARCH_PHRASES = (
    "papers on",
    "papers about",
    "paper on",
    "paper about",
    "studies on",
    "studies about",
    "study on",
    "academic research on",
    "academic research about",
    "academic literature on",
    "scholarly articles on",
    "scholarly research on",
    "peer-reviewed",
    "peer reviewed",
    "research papers on",
    "research papers about",
    "research paper on",
    "literature review on",
    "citations for",
)


def looks_like_academic_search_request(question: str) -> bool:
    """Errs toward missing a request over over-triggering an extra call."""
    text = " ".join((question or "").lower().split())
    return any(phrase in text for phrase in _ACADEMIC_SEARCH_PHRASES)


def _reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str | None:
    """OpenAlex returns abstracts as {word: [positions]} to sidestep
    publisher copyright on the abstract text itself; rebuild plain text from
    it, truncated to a short snippet."""
    if not inverted_index:
        return None
    positions: list[tuple[int, str]] = []
    for word, indices in inverted_index.items():
        for index in indices:
            positions.append((index, word))
    if not positions:
        return None
    positions.sort()
    text = " ".join(word for _, word in positions)
    if len(text) > _MAX_SNIPPET_CHARS:
        text = text[:_MAX_SNIPPET_CHARS].rstrip() + "…"
    return text


def _format_authors(authorships: list[dict[str, Any]] | None) -> str | None:
    names: list[str] = []
    for entry in authorships or []:
        author = entry.get("author") or {}
        name = str(author.get("display_name", "") or "").strip()
        if name:
            names.append(name)
    if not names:
        return None
    if len(names) > _MAX_AUTHORS_SHOWN:
        shown = names[:_MAX_AUTHORS_SHOWN]
        return ", ".join(shown) + " et al."
    return ", ".join(names)


def _best_url(work: dict[str, Any]) -> str | None:
    """Prefer an actual open-access landing page over OpenAlex's own id URL,
    matching what a user expects to click through to."""
    open_access = work.get("open_access") or {}
    oa_url = str(open_access.get("oa_url", "") or "").strip()
    if oa_url.lower().startswith(("http://", "https://")):
        return oa_url
    doi = str(work.get("doi", "") or "").strip()
    if doi:
        if doi.lower().startswith(("http://", "https://")):
            return doi
        return f"https://doi.org/{doi}"
    work_id = str(work.get("id", "") or "").strip()
    if work_id.lower().startswith(("http://", "https://")):
        return work_id
    return None


def search_papers(query: str) -> list[dict[str, Any]]:
    """Up to _MAX_RESULTS scholarly works matching `query`, as {"title",
    "authors", "year", "venue", "citation_count", "url",
    "abstract_snippet"} dicts, ordered by OpenAlex's own relevance ranking.
    [] if the query is blank or the call fails — never raises, since this is
    an enrichment, not worth failing the answer over.
    """
    clean_query = (query or "").strip()
    if not clean_query:
        return []
    try:
        response = httpx.get(
            _OPENALEX_URL,
            params={"search": clean_query, "per-page": str(_MAX_RESULTS)},
            timeout=8.0,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        logger.warning("academic_search.lookup_failed", exc_info=True)
        return []

    results: list[dict[str, Any]] = []
    try:
        for work in data.get("results", None) or []:
            title = str(work.get("display_name", "") or "").strip()
            if not title:
                continue
            primary_location = work.get("primary_location") or {}
            source = primary_location.get("source") or {}
            venue = str(source.get("display_name", "") or "").strip() or None
            results.append(
                {
                    "title": title,
                    "authors": _format_authors(work.get("authorships")),
                    "year": work.get("publication_year"),
                    "venue": venue,
                    "citation_count": work.get("cited_by_count"),
                    "url": _best_url(work),
                    "abstract_snippet": _reconstruct_abstract(
                        work.get("abstract_inverted_index")
                    ),
                }
            )
            if len(results) >= _MAX_RESULTS:
                break
    except (AttributeError, TypeError):
        logger.warning("academic_search.parse_failed", exc_info=True)
        return []
    return results


def format_note(count: int) -> str:
    return (
        "Found a related academic paper."
        if count == 1
        else f"Found {count} related academic papers."
    )
