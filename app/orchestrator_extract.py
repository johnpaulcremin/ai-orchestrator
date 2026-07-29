"""Pure(ish) helpers that turn a raw provider Response object into the pieces
orchestrator.py assembles into an AskResponse: answer text, citations, a
proposed action, generated images, executed-code results, and usage. None of
these call a model themselves — they only read what a call already returned.
"""

from __future__ import annotations

import json
from typing import Any

from .telemetry import logger
from .usage import Usage, estimate_cost


def _extract_text(result: object) -> str:
    """The response's output text, or '' when the model produced none.

    A model can return an empty-output response WITHOUT raising (e.g. a reasoning
    call truncated before any output — status 'incomplete', HTTP 200). Return ''
    in that case, never the object's repr, so callers and persistence treat it as
    the empty answer it is (see the empty-answer guards in main.py) instead of
    storing a 'Response(...)' string as the assistant reply.
    """
    answer_text = getattr(result, "output_text", None) or ""
    if not answer_text:
        # Log the raw object for debugging, but never return it as the answer.
        logger.warning("response.no_output_text result=%r", str(result)[:200])
        return ""
    return answer_text.strip()


def _usage_fields(model: str, usage: Usage, extra_cost_usd: float = 0.0) -> dict:
    """AskResponse/done-event usage fields, or empty if no tokens were captured.

    `extra_cost_usd` folds in non-token costs (currently: generated images).
    """
    if usage.total_tokens == 0 and not extra_cost_usd:
        return {}
    cost = estimate_cost(model, usage)
    if extra_cost_usd:
        cost = (cost or 0.0) + extra_cost_usd
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": cost,
    }


def _record_openai_usage(result: object, usage: Usage | None) -> None:
    if usage is None:
        return
    source = getattr(result, "usage", None)
    if source is not None:
        usage.input_tokens = int(getattr(source, "input_tokens", 0) or 0)
        usage.output_tokens = int(getattr(source, "output_tokens", 0) or 0)
        # Prompt tokens OpenAI served from its cache (billed at a discount).
        details = getattr(source, "input_tokens_details", None)
        if details is not None:
            usage.cached_input_tokens = int(getattr(details, "cached_tokens", 0) or 0)


# A web citation from the web_search tool: {"title": str, "url": str}.
Citation = dict[str, str]

# Cap on how many citations are kept per answer — a search can return dozens;
# only the first few are worth surfacing to the user.
_MAX_CITATIONS = 8

_WEB_SEARCH_TOOL: dict[str, Any] = {"tools": [{"type": "web_search"}]}


def _extract_citations(result: object) -> list[Citation]:
    """Pull url_citation annotations out of a Response's output items.

    De-duplicated by URL, in first-seen order, capped at _MAX_CITATIONS. Never
    raises — citations are an enrichment, not something worth failing an answer
    over if the SDK's shape ever changes underneath us. Only http(s) URLs are
    kept: this is the single point every citation passes through (persisted
    history and the live SSE frame alike), so it's the one place to block a
    javascript:/data: URL that page content fetched by the search tool could
    have injected into the model's output before it ever reaches a rendered
    <a href> — React escapes text but not attribute values.
    """
    citations: list[Citation] = []
    seen: set[str] = set()
    try:
        for item in getattr(result, "output", None) or []:
            for content in getattr(item, "content", None) or []:
                for annotation in getattr(content, "annotations", None) or []:
                    if getattr(annotation, "type", None) != "url_citation":
                        continue
                    url = getattr(annotation, "url", "") or ""
                    if not url or url in seen:
                        continue
                    if not url.lower().startswith(("http://", "https://")):
                        continue
                    seen.add(url)
                    citations.append(
                        {"title": getattr(annotation, "title", "") or url, "url": url}
                    )
                    if len(citations) >= _MAX_CITATIONS:
                        return citations
    except Exception:
        logger.exception("citations.extract_failed")
        return []
    return citations


# A proposed real-world action: {"action": str, "summary": str, "payload": dict}.
# Propose-then-confirm: extracting this NEVER executes anything — see app/actions.py.
PendingActionDict = dict[str, object]


def _action_confirmation_note(action: PendingActionDict) -> str:
    return (
        f"I've prepared an action for your review: **{action['summary']}**. "
        "Confirm below to run it."
    )


def _extract_pending_action(result: object) -> PendingActionDict | None:
    """Pull a propose_action function-call out of a Response's output items.

    Returns the FIRST valid one found (a single answer proposes at most one
    action in this design) or None. Never raises — a malformed/unexpected tool
    call degrades to "no action proposed" rather than failing the answer.
    """
    try:
        for item in getattr(result, "output", None) or []:
            if getattr(item, "type", None) != "function_call":
                continue
            if getattr(item, "name", None) != "propose_action":
                continue
            raw_args = getattr(item, "arguments", None) or ""
            try:
                args = json.loads(raw_args)
            except (ValueError, TypeError):
                continue
            if not isinstance(args, dict):
                continue
            action = str(args.get("action", "")).strip()
            summary = str(args.get("summary", "")).strip()
            payload = args.get("payload")
            if not action or not summary or not isinstance(payload, dict):
                continue
            return {"action": action, "summary": summary, "payload": payload}
    except Exception:
        logger.exception("actions.extract_failed")
        return None
    return None


# A math_solve tool call's arguments: {"operation", "expression", "variable"}.
# Extraction only — app/orchestrator_calls.py executes it via
# math_solve.solve_math (see that module's docstring for why this one, unlike
# propose_action, is executed immediately rather than confirm-gated).
MathCallDict = dict[str, object]


def _extract_math_call(result: object) -> MathCallDict | None:
    """Pull a math_solve function-call out of a Response's output items.

    Returns the FIRST valid one found, or None — never raises, mirroring
    _extract_pending_action's OpenAI JSON-string-arguments handling.
    """
    try:
        for item in getattr(result, "output", None) or []:
            if getattr(item, "type", None) != "function_call":
                continue
            if getattr(item, "name", None) != "math_solve":
                continue
            raw_args = getattr(item, "arguments", None) or ""
            try:
                args = json.loads(raw_args)
            except (ValueError, TypeError):
                continue
            if not isinstance(args, dict):
                continue
            operation = str(args.get("operation", "")).strip()
            expression = str(args.get("expression", "")).strip()
            if not operation or not expression:
                continue
            variable = str(args.get("variable", "") or "x").strip() or "x"
            return {
                "operation": operation,
                "expression": expression,
                "variable": variable,
            }
    except Exception:
        logger.exception("math_solve.extract_failed")
        return None
    return None


def _compose_answer_with_notes(model_text: str, notes: list[str]) -> str:
    """Append synthesized notes (action confirmation, image caption, ...) to the
    model's own text, or return the notes alone when the model produced none.

    A model that calls a hosted/function tool commonly returns NO text at all.
    Without this, a tool-only reply would look like an empty answer and get
    silently dropped by the empty-answer guards (see main.py).
    """
    if not notes:
        return model_text
    combined = "\n\n".join(notes)
    return f"{model_text}\n\n{combined}" if model_text else combined


def _image_generation_note(count: int) -> str:
    return (
        "Here's the image you asked for."
        if count == 1
        else f"Here are the {count} images you asked for."
    )


def _extract_images(result: object) -> list[str]:
    """Pull completed image_generation_call results out of a Response's output
    items, as ready-to-render `data:image/png;base64,...` URLs.

    Never raises — an enrichment, not worth failing the answer over if the
    SDK's shape ever changes underneath us. Only "completed" calls with a
    result are kept; a "failed"/"in_progress" call has no image to show.
    """
    images: list[str] = []
    try:
        for item in getattr(result, "output", None) or []:
            if getattr(item, "type", None) != "image_generation_call":
                continue
            if getattr(item, "status", None) != "completed":
                continue
            data = getattr(item, "result", None)
            if data:
                images.append(f"data:image/png;base64,{data}")
    except Exception:
        logger.exception("images.extract_failed")
        return []
    return images


CodeResultDict = dict[str, object]


def _extract_code_results(result: object) -> list[CodeResultDict]:
    """Pull completed code_interpreter_call items out of a Response's output,
    as {"code": str, "logs": str | None, "images": [data URL, ...]}.

    Never raises — an enrichment, not worth failing the answer over if the
    SDK's shape ever changes underneath us. Only "completed" calls are kept;
    an "in_progress"/"failed" call has nothing useful to show.
    """
    results: list[CodeResultDict] = []
    try:
        for item in getattr(result, "output", None) or []:
            if getattr(item, "type", None) != "code_interpreter_call":
                continue
            if getattr(item, "status", None) != "completed":
                continue
            code = getattr(item, "code", None)
            if not code:
                continue
            logs_parts: list[str] = []
            images: list[str] = []
            for output in getattr(item, "outputs", None) or []:
                output_type = getattr(output, "type", None)
                if output_type == "logs":
                    text = getattr(output, "logs", "") or ""
                    if text:
                        logs_parts.append(text)
                elif output_type == "image":
                    url = getattr(output, "url", "") or ""
                    if url:
                        images.append(url)
            results.append(
                {
                    "code": code,
                    "logs": "\n".join(logs_parts) or None,
                    "images": images,
                }
            )
    except Exception:
        logger.exception("code_results.extract_failed")
        return []
    return results


def _code_execution_note(count: int) -> str:
    return (
        "Ran a snippet of code to help answer this."
        if count == 1
        else f"Ran {count} snippets of code to help answer this."
    )
