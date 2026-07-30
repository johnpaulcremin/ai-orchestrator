"""Self-description: grounds a "what can you do / do you support X / what
models do you use" question in this app's REAL configured state, instead of
letting the model guess (or flatly confabulate — it has no training data on
a private, self-hosted app named "ai-orchestrator").

Same "standalone call gated by a phrase heuristic" design as fact_check.py/
academic_search.py: independent of which model answers, no external network
call (everything here reads local config), no LLM tokens spent computing it.

Deliberately NOT a real cross-provider function-calling round trip (unlike
its description in the original spec): this codebase's OpenAI/Anthropic
dispatch chain (see orchestrator_calls.py/providers.py) never sends a tool
call's result back to the model for a second turn — math_solve/fact_check/
academic_search all compute their result AFTER the model's one real answer
and simply append a note to it (see orchestrator._compose_answer_with_notes
call sites). Building genuine tool-result round-tripping would mean new
mechanics in the most heavily-exercised part of this app for a single
feature; instead, capabilities_snapshot() is appended as a note the same way
those three already work — the note carries the real ground truth
regardless of what the model's own prose says, which is what actually
prevents a wrong answer from being the last word on the subject.

Likewise, "a static identity line only in the cacheable prefix" was scoped
out of this pass: prepending anything to every request's system prompt
prefix (see context_builder.py) changes the exact prompt sent on literally
every call, including the semantic-cache-eligible "context-free" question
shape ask_support._is_context_free relies on being the bare question with
nothing else — a wide blast radius across the whole app for a
low-value cosmetic addition. The identity line lives at the top of the
appended note instead (see format_note), scoped to exactly the turns this
feature already touches.

"RAG-seed app docs" is also out of scope here: app/rag_library.py has no
ownerless/system-scoped document concept today (every document requires an
owner and a budget-charged embedding call) — seeding one would need a new
sharing/system-library concept, not a small addition to an existing
function. Flagged as a real follow-up, not attempted half-built here.
"""

from __future__ import annotations

from typing import Any

from . import free_tier
from .budget import daily_budget_per_owner_usd
from .database import usage_summary
from .schemas import (
    _MAX_CHAT_MESSAGES,
    _MAX_COMPARE_MODELS,
    _MAX_IMPORT_MESSAGES,
    _MAX_INPUT_FILES,
    _MAX_INPUT_IMAGES,
    _MAX_QUESTION_CHARS,
    _MAX_SYSTEM_PROMPT_CHARS,
)
from .settings import bool_setting, describe_settings

APP_VERSION = "0.1.0"


def self_describe_enabled() -> bool:
    """Opt-in: SELF_DESCRIBE=true (env, or a saved Settings override — same
    override > env > default chain as any other toggle). Off by default,
    same as every other feature here that folds extra context/notes into an
    answer (CROSS_CONVERSATION_MEMORY, RAG_LIBRARY)."""
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
    settings = describe_settings()
    return {
        "tiers": {item["key"]: item["effective_model"] for item in settings["tiers"]},
        "categories": {
            item["category"]: item["effective_model"] for item in settings["categories"]
        },
    }


def _flags() -> dict[str, bool]:
    settings = describe_settings()
    return {item["key"]: item["effective_enabled"] for item in settings["features"]}


def _limits() -> dict[str, int]:
    """Known per-request/per-conversation limits — a small, deliberately
    curated subset of schemas.py's validation constants, not every internal
    cap (e.g. per-field char limits nobody would ask about).

    Imports app.workflow lazily (inside the function, not at module level):
    workflow.py itself imports from app.orchestrator, and orchestrator.py
    imports this module — a module-level import here would be a circular
    import.
    """
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
    return {
        "version": APP_VERSION,
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
