from __future__ import annotations

from dataclasses import dataclass
import os

from .schemas import Mode


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


def decide_route(question: str, mode: Mode) -> RouteDecision:
    """
    Routing rules:
    - fast: always use OPENAI_MODEL_FAST
    - smart: always use OPENAI_MODEL_SMART
    - auto: simple heuristic:
        * long / complex-looking questions -> SMART
        * otherwise -> FAST
    """
    base = _env("OPENAI_MODEL", "gpt-5")
    fast = _env("OPENAI_MODEL_FAST", base)
    smart = _env("OPENAI_MODEL_SMART", base)

    q = (question or "").strip()
    q_len = len(q)

    if mode == Mode.fast:
        return RouteDecision(
            model=fast,
            mode_used="fast",
            notes=f"Routed explicitly to FAST model: {fast}",
            max_output_tokens=350,
            temperature=0.2,
        )

    if mode == Mode.smart:
        return RouteDecision(
            model=smart,
            mode_used="smart",
            notes=f"Routed explicitly to SMART model: {smart}",
            max_output_tokens=700,
            temperature=0.2,
        )

    # AUTO heuristic:
    complex_markers = [
        "compare", "tradeoff", "design", "architecture", "plan", "strategy",
        "debug", "error", "why", "explain", "step-by-step", "implement",
        "refactor", "optimize", "security", "threat", "database", "schema",
    ]
    looks_complex = (q_len > 220) or any(m in q.lower() for m in complex_markers)

    if looks_complex:
        return RouteDecision(
            model=smart,
            mode_used="auto->smart",
            notes=f"AUTO heuristic selected SMART model: {smart}",
            max_output_tokens=700,
            temperature=0.2,
        )

    return RouteDecision(
        model=fast,
        mode_used="auto->fast",
        notes=f"AUTO heuristic selected FAST model: {fast}",
        max_output_tokens=350,
        temperature=0.2,
    )
