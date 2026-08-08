from __future__ import annotations

# Task categories the router understands, and which tier handles each best.
# Kept in a dependency-free module so both routing.py and settings.py can import
# them without creating an import cycle (routing imports settings for the
# runtime-editable model map; settings needs the category list to build its
# allow-list of settable keys).

FAST_CATEGORIES: frozenset[str] = frozenset(
    {
        "quick_fact",
        "casual_chat",
        "summarization",
        "simple_transform",
    }
)

SMART_CATEGORIES: frozenset[str] = frozenset(
    {
        "coding",
        "debugging",
        "reasoning",
        "planning",
        "math",
        "analysis",
        "creative_writing",
    }
)

ALL_CATEGORIES: frozenset[str] = FAST_CATEGORIES | SMART_CATEGORIES

# Categories whose ENTIRE input is the text supplied with the request: the job
# is to restate or reshape THAT text, not to answer from anything outside it.
# Retrieved reference material (the RAG document library — see
# app/rag_library.py) can only hurt here, and did: a paragraph that happened to
# be ABOUT cost-aware routing matched this app's own docs/features.md and
# docs/routing.md, and a "rewrite this in plain English, translate it, lay it
# out as a table" request came back with an appended note explaining how the
# app's router works, plus a "used your library" provenance line — on a local
# model whose context the extra chunks had also padded. Nothing was retrieved
# that the task could use, because the task needed nothing external.
TEXT_ONLY_CATEGORIES: frozenset[str] = frozenset(
    {
        "simple_transform",
        "summarization",
    }
)


def retrieval_helps(category: str) -> bool:
    """Whether library retrieval could plausibly improve an answer in this
    category — the gate orchestrator._recall_library_context applies before
    spending an embedding call and a library scan.

    True for "" as well as every unlisted category: an empty category means no
    classification ran at all (an explicit fast/smart/budget mode, a forced
    model, or the keyword heuristic fallback), and in that case behaviour must
    be exactly what it was before this gate existed."""
    return category not in TEXT_ONLY_CATEGORIES


# Default CATEGORY_PROMPT_<CATEGORY> text (see settings.category_prompt_key /
# orchestrator.apply_category_role_prompt) for the categories where a
# multi-deliverable ask is common — planning a project, writing several
# files/functions, analyzing several documents/datasets in one request. Model
# behavior otherwise tends to interleave or half-finish parts instead of
# completing one before starting the next. Every OTHER category still
# defaults to "" (unchanged) — an override (env var or Settings) still wins
# over this, same as any other category prompt. Deliberately brief: this
# lives in the cacheable prompt prefix (see apply_category_role_prompt's
# docstring) and must never carry live numbers.
_PLAN_BEFORE_PRODUCE = (
    "If the request contains more than one distinct deliverable, state the "
    "short plan first, then produce the parts in order, completing each "
    "before starting the next. Never attempt several artefacts in a single "
    "undifferentiated output."
)

# The second half of the transform-contamination fix (the first is
# TEXT_ONLY_CATEGORIES above, which stops the library being retrieved for these
# categories at all). Reference material can still reach a transform task by
# other routes the category gate does not cover — recalled cross-conversation
# memory, a per-message attachment, an earlier turn in the conversation — so
# the categories that operate purely on given text also say so outright.
# Phrased as "unless the request asks for it" rather than a flat prohibition:
# "translate this and add the official term from my glossary" is a real
# transform request that DOES need the library.
_TEXT_ONLY_INPUT = (
    "Your input is the text supplied with this request; work only from it. "
    "Reference material elsewhere in the prompt is background that does not "
    "apply here — ignore it, and never append notes about it, unless the "
    "request explicitly asks for knowledge from outside the supplied text."
)

CATEGORY_PROMPT_DEFAULTS: dict[str, str] = {
    "planning": _PLAN_BEFORE_PRODUCE,
    "coding": _PLAN_BEFORE_PRODUCE,
    "analysis": _PLAN_BEFORE_PRODUCE,
    "simple_transform": _TEXT_ONLY_INPUT,
    "summarization": _TEXT_ONLY_INPUT,
}

# Human-readable labels for the UI, keyed by category slug.
CATEGORY_LABELS: dict[str, str] = {
    "quick_fact": "Quick fact",
    "casual_chat": "Casual chat",
    "summarization": "Summarization",
    "simple_transform": "Simple transform",
    "coding": "Coding",
    "debugging": "Debugging",
    "reasoning": "Reasoning",
    "planning": "Planning",
    "math": "Math",
    "analysis": "Analysis",
    "creative_writing": "Creative writing",
}


def tier_of(category: str) -> str:
    """Which tier a category falls back to when it has no explicit model."""
    return "smart" if category in SMART_CATEGORIES else "fast"
