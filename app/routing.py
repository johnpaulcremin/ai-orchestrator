from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .categories import ALL_CATEGORIES, FAST_CATEGORIES, SMART_CATEGORIES
from .schemas import Mode
from .settings import get_model_overrides, model_setting
from .telemetry import logger

# Re-exported for backwards compatibility: callers historically imported the
# category sets from app.routing. They now live in app.categories.
__all__ = [
    "ALL_CATEGORIES",
    "FAST_CATEGORIES",
    "SMART_CATEGORIES",
    "decide_route",
]


@dataclass(frozen=True)
class RouteDecision:
    model: str
    mode_used: str
    notes: str
    max_output_tokens: int
    reasoning_effort: str
    # The classifier's predicted task category in auto mode (e.g. "coding");
    # empty for explicit fast/smart modes and the heuristic fallback.
    category: str = ""


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    try:
        return int(v.strip()) if v else default
    except ValueError:
        return default


# Reasoning efforts the Responses API accepts.
VALID_REASONING_EFFORTS = {"minimal", "low", "medium", "high"}


def _env_reasoning_effort(name: str, default: str) -> str:
    value = (os.getenv(name) or "").strip().lower()
    return value if value in VALID_REASONING_EFFORTS else default


CLASSIFIER_PROMPT = """You are a routing classifier for an AI orchestrator.
Classify the user request below and reply with ONLY a JSON object, no other text:

{{"category": "<one of: {categories}>",
 "complexity": "<low|medium|high>",
 "reason": "<max 12 words>"}}

Category guide:
- quick_fact: short factual lookup or definition
- casual_chat: greetings, small talk, opinions
- summarization: condense or restate provided text
- simple_transform: reformat, translate, extract, rewrite
- coding: write or modify code
- debugging: diagnose errors or unexpected behaviour
- reasoning: multi-step logic, tradeoffs, deep explanation
- planning: designs, architectures, strategies, plans
- math: calculations, proofs, quantitative problems
- analysis: compare options, evaluate data or documents
- creative_writing: stories, poems, marketing copy

User request:
{question}"""


def _category_model(category: str, overrides: dict[str, str] | None = None) -> str:
    """
    Optional per-task-category model override, e.g. MODEL_CODING=claude-sonnet-5.

    Resolved through the settings layer (saved override, then env var), so it can
    be edited at runtime via the settings API. Lets you send each kind of task to
    the model best suited to it, across providers. Unset categories fall back to
    the fast/smart tier model.
    """
    return model_setting(f"MODEL_{category.upper()}", "", overrides)


def _tier_decision(
    tier: str,
    mode_used: str,
    notes: str,
    model: str | None = None,
    overrides: dict[str, str] | None = None,
    category: str = "",
) -> RouteDecision:
    base = model_setting("OPENAI_MODEL", "gpt-5", overrides)
    fast = model_setting("OPENAI_MODEL_FAST", base, overrides)
    smart = model_setting("OPENAI_MODEL_SMART", base, overrides)

    # Token budgets include model reasoning tokens, so they need headroom.
    fast_tokens = _env_int("FAST_MAX_OUTPUT_TOKENS", 1500)
    smart_tokens = _env_int("SMART_MAX_OUTPUT_TOKENS", 4000)

    if tier == "smart":
        return RouteDecision(
            # A per-category override wins, but keeps the tier's budget/effort.
            model=model or smart,
            mode_used=mode_used,
            notes=notes,
            max_output_tokens=smart_tokens,
            reasoning_effort=_env_reasoning_effort("SMART_REASONING_EFFORT", "medium"),
            category=category,
        )

    # Low reasoning effort keeps the fast tier genuinely fast on simple tasks.
    return RouteDecision(
        model=model or fast,
        mode_used=mode_used,
        notes=notes,
        max_output_tokens=fast_tokens,
        reasoning_effort=_env_reasoning_effort("FAST_REASONING_EFFORT", "low"),
        category=category,
    )


def _heuristic_route(
    question: str, overrides: dict[str, str] | None = None
) -> RouteDecision:
    """Keyword fallback used when the AI classifier is unavailable."""
    q = (question or "").strip()

    complex_markers = [
        "compare",
        "tradeoff",
        "design",
        "architecture",
        "plan",
        "strategy",
        "debug",
        "error",
        "why",
        "explain",
        "step-by-step",
        "implement",
        "refactor",
        "optimize",
        "security",
        "threat",
        "database",
        "schema",
    ]
    looks_complex = (len(q) > 220) or any(m in q.lower() for m in complex_markers)

    tier = "smart" if looks_complex else "fast"
    base = model_setting("OPENAI_MODEL", "gpt-5", overrides)
    model = model_setting(
        "OPENAI_MODEL_SMART" if tier == "smart" else "OPENAI_MODEL_FAST",
        base,
        overrides,
    )

    return _tier_decision(
        tier=tier,
        mode_used=f"auto->{tier}",
        notes=f"Heuristic fallback selected {tier.upper()} model: {model}",
        overrides=overrides,
    )


def _parse_classifier_json(raw: str) -> dict[str, str] | None:
    text = (raw or "").strip()

    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    category = str(data.get("category", "")).strip().lower()
    complexity = str(data.get("complexity", "")).strip().lower()
    reason = str(data.get("reason", "")).strip()

    if category not in ALL_CATEGORIES:
        return None
    if complexity not in {"low", "medium", "high"}:
        complexity = "medium"

    return {"category": category, "complexity": complexity, "reason": reason}


def _classify_with_ai(
    question: str, client: object, overrides: dict[str, str] | None = None
) -> dict[str, str] | None:
    """Ask a small, cheap model to classify the task. Returns None on any failure."""
    router_model = model_setting("OPENAI_MODEL_ROUTER", "gpt-5-nano", overrides)
    prompt = CLASSIFIER_PROMPT.format(
        categories=", ".join(sorted(ALL_CATEGORIES)),
        question=question[:2000],
    )

    timeout_client = client.with_options(timeout=15.0)  # type: ignore[attr-defined]

    def _create(**extra: object) -> object:
        return timeout_client.responses.create(  # type: ignore[attr-defined]
            model=router_model,
            input=prompt,
            max_output_tokens=600,
            **extra,
        )

    try:
        # Minimal reasoning effort keeps the router call cheap and quick.
        result = _create(reasoning={"effort": "minimal"})
    except Exception as first_error:
        logger.warning(
            "router.classifier_first_try_failed model=%s err=%s",
            router_model,
            type(first_error).__name__,
        )
        try:
            result = _create()
        except Exception as retry_error:
            logger.warning(
                "router.classifier_failed model=%s err=%s",
                router_model,
                type(retry_error).__name__,
            )
            return None

    raw = getattr(result, "output_text", None) or ""
    parsed = _parse_classifier_json(raw)

    if parsed is None:
        logger.warning("router.classifier_unparseable output=%r", raw[:200])

    return parsed


def decide_route(
    question: str,
    mode: Mode,
    client: object | None = None,
    forced_model: str | None = None,
) -> RouteDecision:
    """
    Routing rules:
    - fast: always use OPENAI_MODEL_FAST
    - smart: always use OPENAI_MODEL_SMART
    - auto: an AI classifier (OPENAI_MODEL_ROUTER) decides which model suits
      the task best; if the classifier is unavailable or fails, fall back to
      a keyword heuristic.

    Model keys resolve through the settings layer (a saved override wins over the
    env var), read once here and threaded through so a single decision never
    sees a half-changed map.
    """
    overrides = get_model_overrides()

    # Switch-model: a caller-forced model bypasses routing entirely, but keeps
    # the tier's token budget + reasoning effort (fast tier only when mode=fast).
    if forced_model:
        tier = "fast" if mode == Mode.fast else "smart"
        return _tier_decision(
            tier=tier,
            mode_used=f"forced:{forced_model}",
            notes=f"Forced model {forced_model} ({tier}-tier budget)",
            model=forced_model,
            overrides=overrides,
        )

    base = model_setting("OPENAI_MODEL", "gpt-5", overrides)
    fast = model_setting("OPENAI_MODEL_FAST", base, overrides)
    smart = model_setting("OPENAI_MODEL_SMART", base, overrides)

    if mode == Mode.fast:
        return _tier_decision(
            tier="fast",
            mode_used="fast",
            notes=f"Routed explicitly to FAST model: {fast}",
            overrides=overrides,
        )

    if mode == Mode.smart:
        return _tier_decision(
            tier="smart",
            mode_used="smart",
            notes=f"Routed explicitly to SMART model: {smart}",
            overrides=overrides,
        )

    # AUTO: let a small model decide which AI option fits the task best.
    if client is not None:
        classification = _classify_with_ai(question, client, overrides)

        if classification:
            category = classification["category"]
            complexity = classification["complexity"]
            reason = classification["reason"]

            # The tier still sets the token budget + reasoning effort; a
            # per-category model override (if configured) picks the actual model.
            tier = (
                "smart"
                if category in SMART_CATEGORIES or complexity == "high"
                else "fast"
            )
            override = _category_model(category, overrides)
            chosen = override or (smart if tier == "smart" else fast)
            mode_used = f"auto->{tier}:{category}" if override else f"auto->{tier}"
            notes = (
                f"AI router: task={category} complexity={complexity}"
                f"{f' ({reason})' if reason else ''} -> "
                f"{'category model' if override else tier.upper() + ' model'} {chosen}"
                f"{f' ({tier}-tier budget)' if override else ''}"
            )

            return _tier_decision(
                tier=tier,
                mode_used=mode_used,
                notes=notes,
                model=override or None,
                overrides=overrides,
                category=category,
            )

    return _heuristic_route(question, overrides)
