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

CATEGORY_PROMPT_DEFAULTS: dict[str, str] = {
    "planning": _PLAN_BEFORE_PRODUCE,
    "coding": _PLAN_BEFORE_PRODUCE,
    "analysis": _PLAN_BEFORE_PRODUCE,
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
