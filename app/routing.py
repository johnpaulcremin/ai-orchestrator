from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .schemas import Mode
from .telemetry import logger


@dataclass(frozen=True)
class RouteDecision:
    model: str
    mode_used: str
    notes: str
    max_output_tokens: int
    temperature: float


def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v.strip() if v else default


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    try:
        return int(v.strip()) if v else default
    except ValueError:
        return default


# Task categories the router understands, and which tier handles them best.
FAST_CATEGORIES = {
    "quick_fact",
    "casual_chat",
    "summarization",
    "simple_transform",
}

SMART_CATEGORIES = {
    "coding",
    "debugging",
    "reasoning",
    "planning",
    "math",
    "analysis",
    "creative_writing",
}

ALL_CATEGORIES = FAST_CATEGORIES | SMART_CATEGORIES

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


def _tier_decision(
    tier: str,
    mode_used: str,
    notes: str,
) -> RouteDecision:
    base = _env("OPENAI_MODEL", "gpt-5")
    fast = _env("OPENAI_MODEL_FAST", base)
    smart = _env("OPENAI_MODEL_SMART", base)

    # Token budgets include model reasoning tokens, so they need headroom.
    fast_tokens = _env_int("FAST_MAX_OUTPUT_TOKENS", 1500)
    smart_tokens = _env_int("SMART_MAX_OUTPUT_TOKENS", 4000)

    if tier == "smart":
        return RouteDecision(
            model=smart,
            mode_used=mode_used,
            notes=notes,
            max_output_tokens=smart_tokens,
            temperature=0.2,
        )

    return RouteDecision(
        model=fast,
        mode_used=mode_used,
        notes=notes,
        max_output_tokens=fast_tokens,
        temperature=0.2,
    )


def _heuristic_route(question: str) -> RouteDecision:
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
    model = _env(
        "OPENAI_MODEL_SMART" if tier == "smart" else "OPENAI_MODEL_FAST",
        _env("OPENAI_MODEL", "gpt-5"),
    )

    return _tier_decision(
        tier=tier,
        mode_used=f"auto->{tier}",
        notes=f"Heuristic fallback selected {tier.upper()} model: {model}",
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


def _classify_with_ai(question: str, client: object) -> dict[str, str] | None:
    """Ask a small, cheap model to classify the task. Returns None on any failure."""
    router_model = _env("OPENAI_MODEL_ROUTER", "gpt-5-nano")
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
) -> RouteDecision:
    """
    Routing rules:
    - fast: always use OPENAI_MODEL_FAST
    - smart: always use OPENAI_MODEL_SMART
    - auto: an AI classifier (OPENAI_MODEL_ROUTER) decides which model suits
      the task best; if the classifier is unavailable or fails, fall back to
      a keyword heuristic.
    """
    base = _env("OPENAI_MODEL", "gpt-5")
    fast = _env("OPENAI_MODEL_FAST", base)
    smart = _env("OPENAI_MODEL_SMART", base)

    if mode == Mode.fast:
        return _tier_decision(
            tier="fast",
            mode_used="fast",
            notes=f"Routed explicitly to FAST model: {fast}",
        )

    if mode == Mode.smart:
        return _tier_decision(
            tier="smart",
            mode_used="smart",
            notes=f"Routed explicitly to SMART model: {smart}",
        )

    # AUTO: let a small model decide which AI option fits the task best.
    if client is not None:
        classification = _classify_with_ai(question, client)

        if classification:
            category = classification["category"]
            complexity = classification["complexity"]
            reason = classification["reason"]

            tier = (
                "smart"
                if category in SMART_CATEGORIES or complexity == "high"
                else "fast"
            )
            model = smart if tier == "smart" else fast

            return _tier_decision(
                tier=tier,
                mode_used=f"auto->{tier}",
                notes=(
                    f"AI router: task={category} complexity={complexity}"
                    f"{f' ({reason})' if reason else ''}"
                    f" -> {tier.upper()} model {model}"
                ),
            )

    return _heuristic_route(question)
