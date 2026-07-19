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
