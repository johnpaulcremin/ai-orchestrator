"""Optional moderation gate: an independent OpenAI moderation check run on
the incoming question before any answer call, so an obviously disallowed
request is refused up front — no budget spent, no model asked to exercise
judgment about its own prompt. This is the "safety net" layer nothing else
in the app provides: routing/answering never second-guesses what a model
decides to say, this is a separate, independent check on what the user sent
in the first place.

OpenAI-only by construction: it's the one provider whose SDK exposes a
dedicated moderation endpoint (client.moderations.create), reached through
the same OpenAI client every request already creates for routing — no new
client, no new key, and (per OpenAI's own pricing) no extra token cost.
"""

from __future__ import annotations

import os

from openai import OpenAI

from .settings import bool_setting
from .telemetry import logger


def moderation_enabled() -> bool:
    """Opt-in: MODERATION=true (env, or a saved Settings override — same
    override > env > default chain as any other toggle in this app). Off by
    default: a personal/local-first deployment may not want every request
    round-tripped through an extra API call before it's even routed."""
    return bool_setting("MODERATION", False)


def _moderation_model() -> str:
    return (os.getenv("MODERATION_MODEL") or "").strip() or "omni-moderation-latest"


def check_question(client: OpenAI, question: str) -> list[str]:
    """The flagged category names for `question`, or [] when it's clean OR
    the check itself failed. Never raises: a moderation-API outage must not
    block the whole app — it just means this one request goes unchecked,
    same fail-open philosophy as every other extract-and-enrich helper here
    (a missing safety net beats an app that can't answer anything)."""
    if not question.strip():
        return []
    try:
        result = client.moderations.create(model=_moderation_model(), input=question)
        moderation = result.results[0]
        if not moderation.flagged:
            return []
        return [
            name
            for name in type(moderation.categories).model_fields
            if getattr(moderation.categories, name, False)
        ]
    except Exception:
        logger.exception("moderation.check_failed")
        return []


def refusal_note(flagged: list[str]) -> str:
    categories = ", ".join(sorted(flagged))
    return f"This request was refused by the moderation check ({categories})."
