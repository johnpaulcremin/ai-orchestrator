"""Optional-tool plumbing for an answer call: the propose_action, image
generation, and code_interpreter tool definitions, the settings gates that
decide whether each is offered, and _build_tools, which collapses whichever
are active into the single `tools` kwarg the Responses API accepts."""

from __future__ import annotations

import os
from typing import Any

from .actions import ACTION_TOOL_DESCRIPTION, action_input_schema
from .math_solve import MATH_SOLVE_TOOL_DESCRIPTION, math_solve_input_schema
from .orchestrator_extract import _WEB_SEARCH_TOOL
from .self_describe import (
    APP_CAPABILITIES_TOOL_DESCRIPTION,
    app_capabilities_input_schema,
)
from .settings import bool_setting
from .usage import estimate_image_cost


def _build_action_tool() -> dict[str, Any]:
    """The propose_action function tool, OpenAI Responses API shape. Its
    `action` field is restricted to an enum of the operator's actual
    configured named routes (see action_input_schema/actions.named_webhooks)
    when any exist — so the model can only ever propose an action type that
    has somewhere real to go, instead of inventing a name that silently
    falls through to the catch-all webhook (or nowhere, if there isn't one).
    Falls back to a freeform string when ACTIONS_WEBHOOKS isn't set. See
    providers._anthropic_action_tool for the Anthropic-shaped equivalent —
    same description and input schema, different wrapper.
    """
    return {
        "tools": [
            {
                "type": "function",
                "name": "propose_action",
                "description": ACTION_TOOL_DESCRIPTION,
                "parameters": action_input_schema(),
                "strict": False,
            }
        ]
    }


def _image_generation_enabled() -> bool:
    """Opt-in: IMAGE_GENERATION=true (env, or a saved Settings override — same
    override > env > default chain as any model tier) turns on image
    generation.

    Which code path is used depends on _image_generation_provider(): the
    OpenAI path offers a tool and lets the model decide when to call it (same
    as propose_action); the Gemini path has no such tool, so it's gated by
    _looks_like_image_request instead. Off by default either way.
    """
    return bool_setting("IMAGE_GENERATION", False)


def _image_generation_model() -> str:
    return (os.getenv("IMAGE_GENERATION_MODEL") or "").strip() or "gpt-image-1"


def _image_generation_provider() -> str:
    """ "openai" (the built-in Responses API tool) or "gemini" (a standalone
    LiteLLM image_generation call, since Gemini/Imagen has no equivalent of a
    tool the chat model can call itself) — selected by IMAGE_GENERATION_MODEL's
    prefix, the same "prefix picks the provider" convention used everywhere
    else in this app (OPENAI_MODEL_FAST=gemini/... routes through LiteLLM too).
    """
    return (
        "gemini"
        if _image_generation_model().strip().lower().startswith("gemini/")
        else "openai"
    )


_IMAGE_GENERATION_QUALITIES = {"low", "medium", "high", "auto"}


def _image_generation_quality() -> str:
    # Default "high": once an operator opts in, best-effort quality is the
    # point — cost-sensitive deployments can override this down.
    raw = (os.getenv("IMAGE_GENERATION_QUALITY") or "high").strip().lower()
    return raw if raw in _IMAGE_GENERATION_QUALITIES else "high"


def _image_generation_size() -> str:
    return (os.getenv("IMAGE_GENERATION_SIZE") or "").strip() or "auto"


def _worst_case_image_cost(images_wanted: bool, gemini_image_wanted: bool) -> float:
    """Pre-dispatch budget estimate for this call's possible image generation.

    Neither gate guarantees an image actually gets generated (the OpenAI tool
    is only offered, not forced; the Gemini path always requests exactly one),
    but the budget gate already prices every call at its worst case (the full
    output token budget, even if the model uses less) — assuming one image
    here when either path is live is the same philosophy, not a new one.
    """
    if not (images_wanted or gemini_image_wanted):
        return 0.0
    return estimate_image_cost(1, _image_generation_quality()) or 0.0


def _build_image_generation_tool() -> dict[str, Any]:
    return {
        "type": "image_generation",
        "model": _image_generation_model(),
        "quality": _image_generation_quality(),
        "size": _image_generation_size(),
    }


# A deliberately narrow, high-precision phrase list used ONLY to trigger the
# separate Gemini/Imagen image-generation call (see _image_generation_provider)
# — Gemini has no equivalent of OpenAI's image_generation tool a chat model can
# call itself, so something has to decide when an image is actually wanted.
# Unlike web search's live-data heuristic, an image request is rarely ambiguous
# phrasing, so a phrase list is adequate here (not just an outage fallback).
_IMAGE_REQUEST_PHRASES = (
    "draw me",
    "draw a",
    "draw an",
    "generate an image",
    "generate a image",
    "generate a picture",
    "generate a photo",
    "generate artwork",
    "create an image",
    "create a picture",
    "create a photo",
    "create artwork",
    "make me an image",
    "make me a picture",
    "make an image",
    "make a picture",
    "paint a picture",
    "paint me",
    "illustrate a",
    "illustrate an",
    "sketch a",
    "sketch an",
    "design a logo",
    "generate a logo",
    "create a logo",
)


def _looks_like_image_request(question: str) -> bool:
    """Errs toward missing a request over over-triggering an extra paid call."""
    text = " ".join((question or "").lower().split())
    return any(phrase in text for phrase in _IMAGE_REQUEST_PHRASES)


def _code_execution_enabled() -> bool:
    """Opt-in: CODE_EXECUTION=true (env, or a saved Settings override — same
    override > env > default chain as any model tier) lets the model run
    Python via OpenAI's hosted code_interpreter tool — a sandboxed container
    in OpenAI's own cloud, never on this machine, same trust boundary as
    web_search/image_generation. The model decides for itself when running
    code would help (verifying a calculation, testing a snippet), same as
    propose_action/image_generation. Off by default.
    """
    return bool_setting("CODE_EXECUTION", False)


_CODE_INTERPRETER_TOOL: dict[str, Any] = {
    "tools": [{"type": "code_interpreter", "container": {"type": "auto"}}]
}


def _math_solve_enabled() -> bool:
    """Opt-in: MATH_SOLVE=true (env, or a saved Settings override — same
    override > env > default chain as any other feature flag). Off by
    default, same as every other optional tool here. Lives here rather than
    in app/math_solve.py itself to avoid a circular import: settings.py
    imports providers.py (for key_env_for/provider_of), and providers.py
    needs math_solve.py's tool description/schema for the Anthropic tool
    definition — so math_solve.py itself must not import settings.py."""
    return bool_setting("MATH_SOLVE", False)


def _build_math_solve_tool() -> dict[str, Any]:
    """The math_solve function tool, OpenAI Responses API shape. Unlike
    propose_action, a call to this is executed immediately (no user
    confirmation needed — see app/math_solve.py's module docstring). See
    providers._anthropic_math_solve_tool for the Anthropic-shaped
    equivalent — same description and input schema, different wrapper.
    """
    return {
        "tools": [
            {
                "type": "function",
                "name": "math_solve",
                "description": MATH_SOLVE_TOOL_DESCRIPTION,
                "parameters": math_solve_input_schema(),
                "strict": False,
            }
        ]
    }


def _build_self_describe_tool() -> dict[str, Any]:
    """The app_capabilities function tool, OpenAI Responses API shape. Same
    "executed immediately, no confirmation" reasoning as math_solve (see
    app/self_describe.py's module docstring). See
    providers._anthropic_self_describe_tool for the Anthropic-shaped
    equivalent — same description and (empty) input schema, different
    wrapper.
    """
    return {
        "tools": [
            {
                "type": "function",
                "name": "app_capabilities",
                "description": APP_CAPABILITIES_TOOL_DESCRIPTION,
                "parameters": app_capabilities_input_schema(),
                "strict": False,
            }
        ]
    }


def _build_tools(
    web_search: bool,
    actions: bool,
    images: bool = False,
    code_execution: bool = False,
    math_solve: bool = False,
    capabilities: bool = False,
) -> dict[str, Any]:
    """The combined `tools` kwarg for however many optional tools are active.

    web_search, actions, images, code_execution, math_solve, and
    capabilities are independent features that all just add an entry to the
    SAME `tools` list the Responses API accepts — collapsing them here keeps
    the retry ladder below a single "has tools or not" dimension instead of
    a combinatorial one.
    """
    tools: list[dict[str, Any]] = []
    if web_search:
        tools.extend(_WEB_SEARCH_TOOL["tools"])
    if actions:
        tools.extend(_build_action_tool()["tools"])
    if images:
        tools.append(_build_image_generation_tool())
    if code_execution:
        tools.extend(_CODE_INTERPRETER_TOOL["tools"])
    if math_solve:
        tools.extend(_build_math_solve_tool()["tools"])
    if capabilities:
        tools.extend(_build_self_describe_tool()["tools"])
    return {"tools": tools} if tools else {}
