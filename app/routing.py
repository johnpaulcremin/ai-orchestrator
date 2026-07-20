from __future__ import annotations

import json
import os
from dataclasses import dataclass

from openai import BadRequestError

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


# Strict JSON-schema for the router's structured output (Responses API `text`
# param). With this the model physically cannot return unparseable text or an
# out-of-set category — `category` is constrained to the known list. Models that
# reject the param fall back to free-form prompting + tolerant parsing below.
_CLASSIFIER_FORMAT: dict[str, object] = {
    "format": {
        "type": "json_schema",
        "name": "routing_decision",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": sorted(ALL_CATEGORIES)},
                "complexity": {"type": "string", "enum": ["low", "medium", "high"]},
                "reason": {"type": "string"},
            },
            "required": ["category", "complexity", "reason"],
            "additionalProperties": False,
        },
    }
}


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

    if tier == "budget":
        # The cheapest tier, for bulk / low-stakes work. Falls back to the fast
        # model (still with the tighter budget + minimal effort) when
        # OPENAI_MODEL_BUDGET is unset, so mode=budget is never pricier than fast.
        budget_model = model_setting("OPENAI_MODEL_BUDGET", fast, overrides)
        return RouteDecision(
            model=model or budget_model,
            mode_used=mode_used,
            notes=notes,
            max_output_tokens=_env_int("BUDGET_MAX_OUTPUT_TOKENS", 800),
            reasoning_effort=_env_reasoning_effort(
                "BUDGET_REASONING_EFFORT", "minimal"
            ),
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


def _budget_tier_enabled(overrides: dict[str, str] | None = None) -> bool:
    """Whether a dedicated budget-tier model (OPENAI_MODEL_BUDGET) is configured.

    The budget tier is opt-in: unset => auto mode never routes to it and routing
    behaviour is unchanged.
    """
    return bool(model_setting("OPENAI_MODEL_BUDGET", "", overrides))


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
    """Ask a small, cheap model to classify the task. Returns None on any failure.

    Prefers structured output (a strict JSON schema) so the router can't emit
    unparseable text or an out-of-set category. Degrades gracefully: a model that
    rejects the format or reasoning param drops only that param and retries, so a
    supporting model (e.g. gpt-5-nano) makes exactly one call.
    """
    router_model = model_setting("OPENAI_MODEL_ROUTER", "gpt-5-nano", overrides)
    prompt = CLASSIFIER_PROMPT.format(
        categories=", ".join(sorted(ALL_CATEGORIES)),
        question=question[:2000],
    )

    timeout_client = client.with_options(timeout=15.0)  # type: ignore[attr-defined]

    def _create(**extra: object) -> object:
        return timeout_client.responses.create(
            model=router_model,
            input=prompt,
            max_output_tokens=600,
            **extra,
        )

    # Richest first; only a rejected param (BadRequest) drops to the next, simpler
    # combination. Minimal reasoning keeps the call cheap.
    attempts: tuple[dict[str, object], ...] = (
        {"text": _CLASSIFIER_FORMAT, "reasoning": {"effort": "minimal"}},
        {"text": _CLASSIFIER_FORMAT},
        {"reasoning": {"effort": "minimal"}},
        {},
    )

    result = None
    for kwargs in attempts:
        try:
            result = _create(**kwargs)
            break
        except BadRequestError:
            # An unsupported param (structured output and/or reasoning) for this
            # model — drop it and try the next combination.
            logger.warning(
                "router.classifier_param_rejected model=%s params=%s",
                router_model,
                sorted(kwargs),
            )
            continue
        except Exception as err:
            # A non-parameter failure (timeout, rate limit, network): retrying the
            # same call won't help, so give up and let routing fall back.
            logger.warning(
                "router.classifier_failed model=%s err=%s",
                router_model,
                type(err).__name__,
            )
            return None

    if result is None:
        logger.warning("router.classifier_all_attempts_failed model=%s", router_model)
        return None

    raw = getattr(result, "output_text", None) or ""
    parsed = _parse_classifier_json(raw)

    if parsed is None:
        logger.warning("router.classifier_unparseable output=%r", raw[:200])

    return parsed


# A pre-gate for auto mode: a free, high-confidence heuristic that skips the
# gpt-5-nano classifier call for obvious prompts. It only ever decides the tier
# (never a category), and stands down entirely when a per-category override is
# configured, so a skipped classification can never bypass a category override.
#
# The greeting fast-path uses a WHITELIST, not a blocklist: it fires only when
# the whole message reduces to greetings + harmless filler. Any substantive
# leftover (a verb, a topic) makes it defer to the classifier — so a
# greeting-prefixed real task ("hey refactor this") can never be misrouted to
# fast. Erring toward deferral is safe; a confident misroute is not.
_GREETING_WORDS = frozenset(
    {"hi", "hey", "hello", "hiya", "yo", "sup", "howdy", "thanks", "thx", "cheers"}
)
_GREETING_PHRASES = (
    "thank you so much",
    "thank you",
    "good morning",
    "good afternoon",
    "good evening",
    "good day",
    "how are you doing",
    "how are you",
    "how is it going",
    "hows it going",
    "how are things",
    "whats up",
    "nice to meet you",
    "long time no see",
    "hope you are well",
    "hope youre well",
)
# Non-substantive words allowed to surround a greeting without disqualifying it.
_FILLER_WORDS = frozenset(
    {
        "there",
        "everyone",
        "all",
        "team",
        "folks",
        "guys",
        "yall",
        "again",
        "today",
        "tonight",
        "so",
        "much",
        "very",
        "really",
        "mate",
        "friend",
        "buddy",
        "pal",
        "man",
        "dude",
        "a",
        "lot",
        "please",
        "well",
        "and",
        "just",
        "still",
        "you",
        "to",
        "the",
        "for",
    }
)


def _prefilter_enabled() -> bool:
    raw = (os.getenv("ROUTER_PREFILTER") or "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


def _any_category_override(overrides: dict[str, str] | None) -> bool:
    """Whether any task category has a configured model (saved override or env)."""
    return any(
        model_setting(f"MODEL_{category.upper()}", "", overrides)
        for category in ALL_CATEGORIES
    )


def _normalize(text: str) -> str:
    lowered = text.lower().replace("'", "").replace("’", "")
    for ch in '.,!?;:"-()[]{}/\\':
        lowered = lowered.replace(ch, " ")
    return " ".join(lowered.split())


def _is_pure_greeting(question: str) -> bool:
    """True only when the message is nothing but greetings + filler words."""
    text = _normalize(question)
    if not text or len(text) > 80:
        return False

    had_greeting = False
    for phrase in _GREETING_PHRASES:  # multi-word first (longest listed first)
        if phrase in text:
            had_greeting = True
            text = text.replace(phrase, " ")

    for word in text.split():
        if word in _GREETING_WORDS:
            had_greeting = True
        elif word in _FILLER_WORDS:
            continue
        else:
            return False  # a substantive leftover — this is a real request

    return had_greeting


def _prefilter_tier(question: str, overrides: dict[str, str] | None) -> str | None:
    """A confident fast/smart tier for an obvious prompt, or None to defer.

    Fires only on unambiguous cases so auto mode can skip the classifier: a
    fenced code block is clearly a smart task; a message that is nothing but a
    greeting is clearly fast. Disabled by ROUTER_PREFILTER=false or whenever a
    category override is configured (routing then needs the classifier).
    """
    if not _prefilter_enabled() or _any_category_override(overrides):
        return None

    q = (question or "").strip()
    if not q:
        return None

    # Obvious SMART: a fenced code block is unambiguously coding/debugging.
    if "```" in q:
        return "smart"

    # Obvious cheap task: the message is a pure greeting with nothing
    # substantive in it — the budget tier if one is configured, else fast.
    if _is_pure_greeting(q):
        return "budget" if _budget_tier_enabled(overrides) else "fast"

    return None


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
    # the requested tier's token budget + reasoning effort. mode=fast/budget map
    # to their own tier; auto/smart use the generous smart-tier budget.
    if forced_model:
        tier = mode.value if mode in (Mode.fast, Mode.budget) else "smart"
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

    if mode == Mode.budget:
        budget_model = model_setting("OPENAI_MODEL_BUDGET", fast, overrides)
        return _tier_decision(
            tier="budget",
            mode_used="budget",
            notes=f"Routed explicitly to BUDGET model: {budget_model}",
            overrides=overrides,
        )

    # AUTO: skip the classifier for obvious prompts (free), else let a small model
    # decide which AI option fits the task best.
    if client is not None:
        prefiltered = _prefilter_tier(question, overrides)
        if prefiltered is not None:
            if prefiltered == "smart":
                model = smart
            elif prefiltered == "budget":
                model = model_setting("OPENAI_MODEL_BUDGET", fast, overrides)
            else:
                model = fast
            return _tier_decision(
                tier=prefiltered,
                mode_used=f"auto->{prefiltered}",
                notes=(
                    f"Prefilter: obvious {prefiltered.upper()} prompt, "
                    f"skipped the classifier -> {model}"
                ),
                overrides=overrides,
            )

        classification = _classify_with_ai(question, client, overrides)

        if classification:
            category = classification["category"]
            complexity = classification["complexity"]
            reason = classification["reason"]

            # The tier still sets the token budget + reasoning effort; a
            # per-category model override (if configured) picks the actual model.
            # A low-complexity fast-category task drops to the budget tier when
            # one is configured (bulk/low-stakes work); medium ones stay fast.
            if category in SMART_CATEGORIES or complexity == "high":
                tier = "smart"
            elif complexity == "low" and _budget_tier_enabled(overrides):
                tier = "budget"
            else:
                tier = "fast"
            override = _category_model(category, overrides)
            if tier == "smart":
                tier_model = smart
            elif tier == "budget":
                tier_model = model_setting("OPENAI_MODEL_BUDGET", fast, overrides)
            else:
                tier_model = fast
            chosen = override or tier_model
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
