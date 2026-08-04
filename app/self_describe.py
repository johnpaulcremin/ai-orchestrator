"""Self-description: grounds a "what can you do / do you support X / what
models do you use" question in this app's REAL configured state, instead of
letting the model guess (or flatly confabulate — it has no training data on
a private, self-hosted app named "ai-orchestrator").

Cross-provider tool, same pattern as math_solve.py: `app_capabilities` is
offered to the model as a function/custom tool (OpenAI Responses API
`function`, Anthropic Messages API custom tool-use) whenever SELF_DESCRIBE
is on and the model's provider supports one (see orchestrator._SELF_DESCRIBE_TOOL_PROVIDERS)
— the MODEL decides when a question is actually about the app itself,
instead of a phrase list guessing on the app's behalf. A call is executed
immediately, server-side (capabilities_snapshot() reads local config only —
no external call, no LLM tokens spent computing it), the moment it's
extracted from the model's response — same "no confirmation needed, no
persisted pending state" reasoning as math_solve (see that module's
docstring): capabilities_snapshot() has no real-world side effects either.

Still NOT a real cross-provider function-calling round trip in the fullest
sense: this codebase's OpenAI/Anthropic dispatch chain (see
orchestrator_calls.py/providers.py) never sends a tool call's *result* back
to the model for a second turn — math_solve and this tool both compute
their result AFTER the model's one real answer and fold it in as an
appended note (see orchestrator._compose_answer_with_notes call sites),
rather than the model itself narrating the result in its own words. The
note carries the real ground truth regardless of what the model's own text
says, which is what actually prevents a wrong answer from being the last
word on the subject — see test_run_orchestrator_appends_real_data_even_when_model_confabulates.

A provider with no hosted/custom tool-calling wired up here at all (every
LiteLLM-routed model — Gemini, Bedrock, Mistral, Groq, Ollama, local
endpoints) falls back to the same phrase-heuristic trigger this module used
exclusively before (see looks_like_capabilities_request) — same
"heuristic fallback for a provider with no native tool" reasoning
orchestrator_tools._looks_like_image_request already uses for Gemini image
generation.

The cacheable system prefix (see context_builder.py) gets a short, STATIC
identity + tool-hint line (_CAPABILITIES_IDENTITY_LINE below) whenever
SELF_DESCRIBE is on — never live numbers (a remaining-budget figure baked
into a prompt-cache-eligible prefix would either go stale across turns or
bust the cache every time it changed). The identity line just tells the
model the tool exists and when to reach for it; the actual verified numbers
only ever appear in the appended note, computed fresh per turn.

"RAG-seed app docs" is out of scope here: app/rag_library.py has no
ownerless/system-scoped document concept today (every document requires an
owner and a budget-charged embedding call) — seeding is instead a per-owner
action (see the Library modal's "Seed library with app docs" button /
POST /v1/library/seed-app-docs), not a system-wide document.
"""

from __future__ import annotations

from typing import Any

# NOTE: every app-internal import below (settings, free_tier, budget,
# database, schemas) is LAZY — inside the function that needs it, never at
# module level. providers.py imports THIS module's
# APP_CAPABILITIES_TOOL_DESCRIPTION/app_capabilities_input_schema for the
# Anthropic custom-tool definition (same as it already does for
# math_solve.py's), and settings.py imports providers.py — so ANY of these
# at module level here would be a circular import the moment it's reached
# transitively (settings.py directly; free_tier.py and schemas.py both
# import settings.py themselves). Same reasoning as orchestrator_tools.py
# keeping _math_solve_enabled() out of math_solve.py itself (see that
# function's docstring) and math_solve.py itself having zero app-internal
# imports at module level.

# Bumped alongside each CHANGELOG release cut (see CHANGELOG.md) — the one
# source of truth self_describe/GET /v1/capabilities report, so "what
# version are you" never depends on the answering model's own guess.
APP_VERSION = "0.3.0"

# Prepended to the cacheable system prefix (see context_builder.py) whenever
# SELF_DESCRIBE is on — deliberately static (no live numbers: see module
# docstring) so it doesn't bust prompt caching, and short enough that
# turning the flag on costs a handful of tokens, not a paragraph.
CAPABILITIES_IDENTITY_LINE = (
    "You are AI Orchestrator, a cost-aware multi-model router. For "
    "questions about your own features, configuration or limits, call the "
    "app_capabilities tool."
)

# A compact, static architecture summary — folded into capabilities_snapshot()
# and format_note() so a model asked to "suggest improvements" (or anything
# else that benefits from knowing HOW this app is built, not just what it's
# configured to do) doesn't re-propose organs the patient already has, e.g.
# suggesting LiteLLM/a RAG pipeline/SQLite as new additions to an app that
# already runs on all three. Deliberately static (no version numbers, no
# live counts) so it's as cheap to include as any other fixed string here —
# see the module docstring's "no live numbers in the cacheable prefix" rule,
# which this doesn't touch since it's only ever appended in format_note()'s
# note, never folded into CAPABILITIES_IDENTITY_LINE.
INTERNALS_SUMMARY = (
    "Built on: OpenAI and Anthropic models are called natively; every other "
    "provider (Gemini, Bedrock, Mistral, Groq, local Ollama models, and any "
    "generic OpenAI-compatible endpoint) is routed through LiteLLM. All data "
    "— conversations, messages, settings, spend, feedback — lives in a "
    "single local SQLite database, with spend and feedback recorded as "
    "append-only ledgers. Retrieval-augmented answers draw on a per-owner "
    "document library matched by brute-force cosine similarity over stored "
    "embeddings — deliberately no vector database. Responses can be exact-"
    "cached (identical request) or semantically cached (near-duplicate "
    "question), and a free-tier lane routes eligible questions to "
    "free-quota models before spending budget."
)

APP_CAPABILITIES_TOOL_DESCRIPTION = (
    "Get this app's REAL, live configuration and capabilities — the actual "
    "enabled features, effective model map, known request limits, your own "
    "remaining daily budget, and free-lane quota status — instead of "
    "guessing or inventing details about a private, self-hosted app you "
    "have no training data on. Call this whenever the user asks what you "
    "can do, what models you use, whether you support some feature, what "
    "your limits are, what version you are, or how much budget they have "
    "left. Takes no arguments."
)


def app_capabilities_input_schema() -> dict[str, Any]:
    """No meaningful arguments — the tool's whole point is a fixed snapshot
    of server-side state, not anything the model would parameterize.
    An empty `properties` object (rather than omitting `parameters`
    entirely) is what both the OpenAI Responses API and Anthropic Messages
    API expect for a zero-argument tool."""
    return {"type": "object", "properties": {}, "additionalProperties": False}


def self_describe_enabled() -> bool:
    """Opt-in: SELF_DESCRIBE=true (env, or a saved Settings override — same
    override > env > default chain as any other toggle). Off by default,
    same as every other feature here that folds extra context/notes into an
    answer (CROSS_CONVERSATION_MEMORY, RAG_LIBRARY)."""
    from .settings import bool_setting

    return bool_setting("SELF_DESCRIBE", False)


# A deliberately narrow, high-precision phrase list — same design as
# fact_check._FACT_CHECK_PHRASES/academic_search._ACADEMIC_SEARCH_PHRASES:
# errs toward missing a request over over-triggering an extra note on an
# ordinary question.
_SELF_DESCRIBE_PHRASES = (
    "what can you do",
    "what do you support",
    "do you support",
    "what are your capabilities",
    "what are your limits",
    "what features do you have",
    "what features do you support",
    "what models do you use",
    "which models do you use",
    "which models do you support",
    "what version are you",
    "what version of",
    "are you rate limited",
    "what's your budget",
    "what is your budget",
    "how much budget",
    "tell me about yourself",
    "what are you",
)


def looks_like_capabilities_request(question: str) -> bool:
    """Errs toward missing a request over over-triggering an extra note."""
    text = " ".join((question or "").lower().split())
    return any(phrase in text for phrase in _SELF_DESCRIBE_PHRASES)


def _model_map() -> dict[str, Any]:
    """Tier + task-category effective models, stripped down to just the
    facts a model answering a user's question needs — not the admin-only
    override/env-var raw values describe_settings() also carries."""
    from .settings import describe_settings

    settings = describe_settings()
    return {
        "tiers": {item["key"]: item["effective_model"] for item in settings["tiers"]},
        "categories": {
            item["category"]: item["effective_model"] for item in settings["categories"]
        },
    }


def _flags() -> dict[str, bool]:
    from .settings import describe_settings

    settings = describe_settings()
    return {item["key"]: item["effective_enabled"] for item in settings["features"]}


def _limits() -> dict[str, int]:
    """Known per-request/per-conversation limits — a small, deliberately
    curated subset of schemas.py's validation constants, not every internal
    cap (e.g. per-field char limits nobody would ask about).

    Imports app.workflow lazily (inside the function, not at module level):
    workflow.py itself imports from app.orchestrator, and orchestrator.py
    imports this module — a module-level import here would be a circular
    import (see the module docstring's note on lazy imports generally).
    """
    from .schemas import (
        _MAX_CHAT_MESSAGES,
        _MAX_COMPARE_MODELS,
        _MAX_IMPORT_MESSAGES,
        _MAX_INPUT_FILES,
        _MAX_INPUT_IMAGES,
        _MAX_QUESTION_CHARS,
        _MAX_SYSTEM_PROMPT_CHARS,
    )
    from .workflow import max_steps as workflow_max_steps

    return {
        "max_question_chars": _MAX_QUESTION_CHARS,
        "max_attached_images": _MAX_INPUT_IMAGES,
        "max_attached_files": _MAX_INPUT_FILES,
        "max_compare_models": _MAX_COMPARE_MODELS,
        "max_workflow_steps": workflow_max_steps(),
        "max_custom_instructions_chars": _MAX_SYSTEM_PROMPT_CHARS,
        "max_chat_messages_per_compat_request": _MAX_CHAT_MESSAGES,
        "max_import_messages": _MAX_IMPORT_MESSAGES,
    }


def _owner_budget(owner: str | None) -> dict[str, float | None]:
    """This caller's own remaining per-owner daily budget — same computation
    GET /v1/usage already exposes (see app/routers/usage.py), never the live
    global spend total."""
    from .budget import daily_budget_per_owner_usd
    from .database import usage_summary

    limit = daily_budget_per_owner_usd()
    if limit is None:
        return {"daily_budget_per_owner_usd": None, "owner_remaining_usd": None}
    today_usd = float(usage_summary(owner, days=1)["today_usd"])
    return {
        "daily_budget_per_owner_usd": limit,
        "owner_remaining_usd": max(0.0, limit - today_usd),
    }


def capabilities_snapshot(owner: str | None) -> dict[str, Any]:
    """The full self-description JSON: version, model map, feature flags,
    known request limits, this caller's own remaining per-owner budget, and
    free-lane quota status — everything self_describe()/GET /v1/capabilities
    return. Every field here is read from this app's actual configured
    state, never invented."""
    from . import free_tier

    return {
        "version": APP_VERSION,
        "internals": INTERNALS_SUMMARY,
        "models": _model_map(),
        "flags": _flags(),
        "limits": _limits(),
        "budget": _owner_budget(owner),
        "free_lane": {
            "enabled": free_tier.enabled(),
            "models": free_tier.status(),
        },
    }


def format_note(snapshot: dict[str, Any]) -> str:
    """A short, human-readable summary of `snapshot` to append to an
    answer — the identity line plus the handful of facts a "what can you
    do"-style question actually wants, not the full raw JSON."""
    lines = [
        "I'm the assistant embedded in ai-orchestrator, a self-hosted "
        f"multi-provider AI chat app (v{snapshot['version']}). Verified "
        "capabilities (not a guess):",
        f"- {snapshot['internals']}",
    ]
    models = snapshot["models"]["tiers"]
    if models:
        model_bits = ", ".join(f"{tier}: {model}" for tier, model in models.items())
        lines.append(f"- Models — {model_bits}")
    enabled_flags = sorted(key for key, on in snapshot["flags"].items() if on)
    lines.append(
        f"- Enabled optional features — {', '.join(enabled_flags) if enabled_flags else 'none'}"
    )
    limits = snapshot["limits"]
    lines.append(
        "- Limits — "
        f"{limits['max_question_chars']:,} chars/question, "
        f"{limits['max_attached_images']} images, "
        f"{limits['max_attached_files']} files per message"
    )
    remaining = snapshot["budget"]["owner_remaining_usd"]
    if remaining is not None:
        lines.append(f"- Your remaining daily budget — ${remaining:,.4f}")
    free_lane = snapshot["free_lane"]
    if free_lane["enabled"] and free_lane["models"]:
        lane_bits = ", ".join(
            f"{m['model']} ({m['remaining']}/{m['quota']} left today)"
            for m in free_lane["models"]
        )
        lines.append(f"- Free-lane models — {lane_bits}")
    return "\n".join(lines)
