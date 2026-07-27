from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TypedDict

from openai import BadRequestError

from .categories import ALL_CATEGORIES, FAST_CATEGORIES, SMART_CATEGORIES
from .providers import provider_of
from .schemas import Mode
from .settings import bool_setting, get_model_overrides, model_setting
from .telemetry import logger


class Classification(TypedDict):
    category: str
    complexity: str
    reason: str
    # Whether the question depends on information that changes over time
    # (news, prices, scores, weather, "current"/"latest" anything) and would be
    # stale without a live web search. Only ever acted on when WEB_SEARCH=true.
    needs_live_data: bool
    # True when the question references something ("this", "that", "it", "the
    # app", ...) that has more than one plausible referent in the recent
    # conversation history given alongside it — e.g. it could mean either an
    # app being discussed OR the assistant itself. Only ever set when history
    # was actually provided; a fresh conversation has nothing to be ambiguous
    # against. When true, the orchestrator returns clarifying_question
    # directly instead of answering, since guessing wrong wastes a full
    # answer (see CLASSIFIER_PROMPT).
    ambiguous: bool
    clarifying_question: str


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
    # Whether this call should use the OpenAI web_search tool. Already fully
    # gated by the time it reaches here: WEB_SEARCH=true, a freshness signal
    # fired, AND the resolved model is OpenAI-served (the only provider path
    # that supports the tool) — see _gate_live_data.
    needs_live_data: bool = False
    # When true, the orchestrator returns clarifying_question as the whole
    # answer instead of calling the fast/smart model at all — cheaper than
    # guessing wrong and burning a full answer on the wrong interpretation.
    ambiguous: bool = False
    clarifying_question: str = ""


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
 "reason": "<max 12 words>",
 "needs_live_data": <true|false>,
 "ambiguous": <true|false>,
 "clarifying_question": "<short question, or empty string>"}}

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

needs_live_data: true ONLY if the answer depends on information that changes
over time and would be stale from training data alone — current news, prices,
scores, weather, exchange rates, "latest"/"current" real-world events. false for
everything else, including questions about "the current file", "the latest
commit", or any other reference to the user's own code/documents/conversation.

ambiguous: true ONLY if the request uses a reference word ("this", "that",
"it", "these", "those", or a bare noun phrase like "the app") that could
plausibly point at more than one distinct thing given the recent conversation
history below, AND answering confidently would require guessing which one.
false if there is no history, if a pronoun's referent is clear from context,
or if the request doesn't reference anything at all. When true, do not guess
the category/complexity fields carefully — set clarifying_question to a short
question (under 20 words) that names the specific candidates, e.g. "Do you
mean the app we're discussing, or me (this assistant)?". When false, leave
clarifying_question as "".

Recent conversation history (may be empty for a fresh conversation):
{history}

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
                "needs_live_data": {"type": "boolean"},
                "ambiguous": {"type": "boolean"},
                "clarifying_question": {"type": "string"},
            },
            "required": [
                "category",
                "complexity",
                "reason",
                "needs_live_data",
                "ambiguous",
                "clarifying_question",
            ],
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


def _web_search_enabled() -> bool:
    """Opt-in: WEB_SEARCH=true (env, or a saved Settings override — same
    override > env > default chain as any model tier) lets auto mode use the
    OpenAI web_search tool for freshness-sensitive questions. Unset/false =>
    the signal is still computed (harmless) but never acted on, so nothing
    changes until you opt in.
    """
    return bool_setting("WEB_SEARCH", False)


def _gate_live_data(wants_live_data: bool, model: str) -> bool:
    """The final, fully-gated web-search decision for a resolved model.

    True only when the caller/classifier asked for it AND the feature is opted
    in AND the resolved model is served by the native OpenAI Responses API — the
    only path that supports the web_search tool. A Claude/Gemini/LiteLLM model
    never gets it, even if the question clearly needs live data.
    """
    return wants_live_data and _web_search_enabled() and provider_of(model) == "openai"


def _tier_decision(
    tier: str,
    mode_used: str,
    notes: str,
    model: str | None = None,
    overrides: dict[str, str] | None = None,
    category: str = "",
    wants_live_data: bool = False,
) -> RouteDecision:
    base = model_setting("OPENAI_MODEL", "gpt-5", overrides)
    fast = model_setting("OPENAI_MODEL_FAST", base, overrides)
    smart = model_setting("OPENAI_MODEL_SMART", base, overrides)

    # Token budgets include model reasoning tokens, so they need headroom.
    fast_tokens = _env_int("FAST_MAX_OUTPUT_TOKENS", 1500)
    smart_tokens = _env_int("SMART_MAX_OUTPUT_TOKENS", 4000)

    if tier == "smart":
        # A per-category override wins, but keeps the tier's budget/effort.
        resolved_model = model or smart
        return RouteDecision(
            model=resolved_model,
            mode_used=mode_used,
            notes=notes,
            max_output_tokens=smart_tokens,
            reasoning_effort=_env_reasoning_effort("SMART_REASONING_EFFORT", "medium"),
            category=category,
            needs_live_data=_gate_live_data(wants_live_data, resolved_model),
        )

    if tier == "budget":
        # The cheapest tier, for bulk / low-stakes work. Falls back to the fast
        # model (still with the tighter budget + minimal effort) when
        # OPENAI_MODEL_BUDGET is unset, so mode=budget is never pricier than fast.
        budget_model = model_setting("OPENAI_MODEL_BUDGET", fast, overrides)
        resolved_model = model or budget_model
        return RouteDecision(
            model=resolved_model,
            mode_used=mode_used,
            notes=notes,
            max_output_tokens=_env_int("BUDGET_MAX_OUTPUT_TOKENS", 800),
            reasoning_effort=_env_reasoning_effort(
                "BUDGET_REASONING_EFFORT", "minimal"
            ),
            category=category,
            needs_live_data=_gate_live_data(wants_live_data, resolved_model),
        )

    # Low reasoning effort keeps the fast tier genuinely fast on simple tasks.
    resolved_model = model or fast
    return RouteDecision(
        model=resolved_model,
        mode_used=mode_used,
        notes=notes,
        max_output_tokens=fast_tokens,
        reasoning_effort=_env_reasoning_effort("FAST_REASONING_EFFORT", "low"),
        category=category,
        needs_live_data=_gate_live_data(wants_live_data, resolved_model),
    )


def _budget_tier_enabled(overrides: dict[str, str] | None = None) -> bool:
    """Whether a dedicated budget-tier model (OPENAI_MODEL_BUDGET) is configured.

    The budget tier is opt-in: unset => auto mode never routes to it and routing
    behaviour is unchanged.
    """
    return bool(model_setting("OPENAI_MODEL_BUDGET", "", overrides))


# A conservative, narrow phrase list used ONLY by the keyword heuristic fallback
# (the AI classifier is down, so there's no needs_live_data signal at all). It
# deliberately excludes generic words like "current"/"latest"/"now" alone —
# those are extremely common in ordinary dev questions ("current file", "latest
# commit", "now let's add tests") and would over-trigger a paid search. The
# classifier (used whenever available) understands full sentences and carries
# the real signal; this is just a safety net for its outage.
_LIVE_DATA_FALLBACK_PHRASES = (
    "todays date",
    "current time",
    "current weather",
    "weather today",
    "todays weather",
    "stock price",
    "share price",
    "exchange rate",
    "who won",
    "final score",
    "latest score",
    "breaking news",
    "latest news",
    "election result",
    "current price of",
)


def _looks_time_sensitive_fallback(question: str) -> bool:
    text = _normalize(question)
    return any(phrase in text for phrase in _LIVE_DATA_FALLBACK_PHRASES)


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
        wants_live_data=_looks_time_sensitive_fallback(q),
    )


def _parse_classifier_json(raw: str) -> Classification | None:
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

    # Structured output always gives a real bool; the free-form fallback path
    # (a model that rejected the json_schema format) may omit it or send a
    # string — coerce tolerantly, defaulting to False (never search by mistake).
    def _coerce_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "1", "yes"}

    needs_live_data = _coerce_bool(data.get("needs_live_data", False))
    # Same tolerant coercion, and default False for the same reason: never
    # block on a clarifying question by mistake if the field is missing.
    ambiguous = _coerce_bool(data.get("ambiguous", False))
    clarifying_question = str(data.get("clarifying_question", "")).strip()
    if ambiguous and not clarifying_question:
        # The model said ambiguous but gave nothing to ask — that's not
        # actionable, so treat it as not-ambiguous rather than short-circuiting
        # to an empty clarifying message.
        ambiguous = False

    return {
        "category": category,
        "complexity": complexity,
        "reason": reason,
        "needs_live_data": needs_live_data,
        "ambiguous": ambiguous,
        "clarifying_question": clarifying_question if ambiguous else "",
    }


def _classify_with_ai(
    question: str,
    client: object,
    overrides: dict[str, str] | None = None,
    history: str = "",
) -> Classification | None:
    """Ask a small, cheap model to classify the task. Returns None on any failure.

    Prefers structured output (a strict JSON schema) so the router can't emit
    unparseable text or an out-of-set category. Degrades gracefully: a model that
    rejects the format or reasoning param drops only that param and retries, so a
    supporting model (e.g. gpt-5-nano) makes exactly one call.
    """
    router_model = model_setting("OPENAI_MODEL_ROUTER", "gpt-5-nano", overrides)
    prompt = CLASSIFIER_PROMPT.format(
        categories=", ".join(sorted(ALL_CATEGORIES)),
        history=(history[:2000] or "(none)"),
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


def _category_overridden(category: str, overrides: dict[str, str] | None) -> bool:
    """Whether this one task category has a configured model (saved override
    or env) — see _prefilter_tier, which checks only the category each of its
    shortcuts could plausibly preempt, not every category in the app."""
    return bool(model_setting(f"MODEL_{category.upper()}", "", overrides))


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
    greeting is clearly fast. Disabled by ROUTER_PREFILTER=false. Each
    shortcut is ALSO disabled individually when the specific category it
    would preempt has its own override configured (routing then needs the
    classifier to find and apply it) — but an override on some OTHER,
    unrelated category (e.g. creative writing) must not force every
    code-fenced prompt or greeting through a paid classifier call too; it has
    no bearing on either shortcut.
    """
    if not _prefilter_enabled():
        return None

    q = (question or "").strip()
    if not q:
        return None

    # Obvious SMART: a fenced code block is unambiguously coding/debugging.
    if "```" in q:
        if _category_overridden("coding", overrides) or _category_overridden(
            "debugging", overrides
        ):
            return None
        return "smart"

    # Obvious cheap task: the message is a pure greeting with nothing
    # substantive in it — the budget tier if one is configured, else fast.
    if _is_pure_greeting(q):
        if _category_overridden("casual_chat", overrides):
            return None
        return "budget" if _budget_tier_enabled(overrides) else "fast"

    return None


def decide_route(
    question: str,
    mode: Mode,
    client: object | None = None,
    forced_model: str | None = None,
    history: str = "",
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

    `history` is a short recent-turns snippet (see main.build_recent_history_snippet),
    used only for the classifier's ambiguity check in auto mode — it never
    affects category/complexity classification, fast/smart/budget modes ignore
    it entirely, and it costs nothing extra since it rides the same classifier
    call the router already makes.
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

        classification = _classify_with_ai(question, client, overrides, history)

        if classification and classification["ambiguous"]:
            # Short-circuit before spending any tier's model call: guessing
            # the wrong referent burns a full answer, so ask instead. The
            # model/tokens fields below are never dispatched to — the
            # orchestrator checks `ambiguous` first and returns
            # clarifying_question directly.
            return RouteDecision(
                model=fast,
                mode_used="auto->clarify",
                notes=(
                    "AI router: ambiguous reference in recent history, "
                    "asked for clarification instead of guessing"
                ),
                max_output_tokens=0,
                reasoning_effort="minimal",
                ambiguous=True,
                clarifying_question=classification["clarifying_question"],
            )

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
                wants_live_data=classification["needs_live_data"],
            )

    return _heuristic_route(question, overrides)
