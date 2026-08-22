"""Pure(ish) helpers that turn a raw provider Response object into the pieces
orchestrator.py assembles into an AskResponse: answer text, citations, a
proposed action, generated images, executed-code results, and usage. None of
these call a model themselves — they only read what a call already returned.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from . import database
from .schemas import (
    dedupe_code_files,
    _CODE_FILE_MIME_ALLOWLIST,
    _MAX_CODE_FILE_CHARS,
    guess_code_file_mime,
)
from .telemetry import logger
from .usage import Usage, estimate_cost

# Raw-byte mirror of schemas._MAX_CODE_FILE_CHARS (a base64 STRING cap) --
# checked before encoding, so an oversized sandbox file is never even
# base64'd, let alone held in memory twice.
_MAX_CODE_FILE_BYTES = int(_MAX_CODE_FILE_CHARS * 3 / 4)


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


# Cap on how many search queries are kept per answer — same reasoning as
# _MAX_CITATIONS (a search can issue several queries; only the first few are
# worth surfacing).
_MAX_SEARCH_QUERIES = 8


def _extract_search_queries(result: object) -> list[str]:
    """Pull the actual query text out of every `web_search_call` output item
    — the search terms the model's web_search tool call issued, distinct
    from `sources`/`_extract_citations` (the RESULTS a search returned).
    De-duplicated in first-seen order, capped at _MAX_SEARCH_QUERIES.

    The Responses API's ActionSearch shape carries the query as EITHER a
    singular `query` string or a `queries` list depending on API version —
    both are read here so this doesn't silently miss one on either shape.
    Never raises: an enrichment, same posture as _extract_citations.
    """
    queries: list[str] = []
    seen: set[str] = set()
    try:
        for item in getattr(result, "output", None) or []:
            if getattr(item, "type", None) != "web_search_call":
                continue
            action = getattr(item, "action", None)
            if action is None:
                continue
            candidates = list(getattr(action, "queries", None) or [])
            single = getattr(action, "query", None)
            if single:
                candidates.append(single)
            for query in candidates:
                query = str(query or "").strip()
                if not query or query in seen:
                    continue
                seen.add(query)
                queries.append(query)
                if len(queries) >= _MAX_SEARCH_QUERIES:
                    return queries
    except Exception:
        logger.exception("search_queries.extract_failed")
        return []
    return queries


# A proposed real-world action: {"action": str, "summary": str, "payload": dict}.
# Propose-then-confirm: extracting this NEVER executes anything — see app/actions.py.
PendingActionDict = dict[str, object]


def _action_confirmation_note(action: PendingActionDict) -> str:
    return (
        f"I've prepared an action for your review: **{action['summary']}**. "
        "Confirm below to run it."
    )


def _record_malformed_call(result: object, tool: str) -> None:
    """Tally a function_call whose arguments failed to parse or validate —
    the event the extractors otherwise swallow silently. Aggregate count
    only (see database.malformed_tool_calls); the model comes off the
    Response object itself, since the extractors deliberately don't know
    which call produced their input. Best-effort on the same terms as the
    extractors: a failed write must not fail the answer."""
    model = str(getattr(result, "model", "") or "")
    logger.warning("tool_call.malformed tool=%s model=%s", tool, model or "?")
    try:
        database.record_malformed_tool_call(model, tool)
    except Exception:
        logger.warning("tool_call.malformed_record_failed", exc_info=True)


def _extract_pending_action(result: object) -> PendingActionDict | None:
    """Pull a propose_action function-call out of a Response's output items.

    Returns the FIRST valid one found (a single answer proposes at most one
    action in this design) or None. Never raises — a malformed/unexpected tool
    call degrades to "no action proposed" rather than failing the answer,
    though the degradation is now counted (_record_malformed_call) so a
    model that habitually fumbles the schema shows up in the weekly report
    instead of just quietly proposing nothing.
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
                _record_malformed_call(result, "propose_action")
                continue
            if not isinstance(args, dict):
                _record_malformed_call(result, "propose_action")
                continue
            action = str(args.get("action", "")).strip()
            summary = str(args.get("summary", "")).strip()
            payload = args.get("payload")
            if not action or not summary or not isinstance(payload, dict):
                _record_malformed_call(result, "propose_action")
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
                _record_malformed_call(result, "math_solve")
                continue
            if not isinstance(args, dict):
                _record_malformed_call(result, "math_solve")
                continue
            operation = str(args.get("operation", "")).strip()
            expression = str(args.get("expression", "")).strip()
            if not operation or not expression:
                _record_malformed_call(result, "math_solve")
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


def _extract_capabilities_call(result: object) -> bool:
    """True if the model called the app_capabilities tool. No arguments to
    parse (see self_describe.app_capabilities_input_schema) — a call is a
    call, so unlike _extract_math_call/_extract_pending_action this returns
    a bool, not the call's payload. Never raises, mirroring
    _extract_pending_action."""
    try:
        for item in getattr(result, "output", None) or []:
            if getattr(item, "type", None) != "function_call":
                continue
            if getattr(item, "name", None) == "app_capabilities":
                return True
    except Exception:
        logger.exception("app_capabilities.extract_failed")
        return False
    return False


def _compose_answer_with_notes(model_text: str, notes: list[str]) -> str:
    """Append synthesized notes (action confirmation, image caption, ...) to the
    model's own text, or return the notes alone when the model produced none.

    A model that calls a hosted/function tool commonly returns NO text at all.
    Without this, a tool-only reply would look like an empty answer and get
    silently dropped by the empty-answer guards (see main.py).

    Blank notes are dropped rather than joined: a note-producing step that
    fails is allowed to return "" (see _self_describe_note_safely), and
    gluing that on would leave the answer with trailing blank lines and,
    worse, a "\\n\\n" separator implying something followed.
    """
    notes = [note for note in notes if note and note.strip()]
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


def _image_generation_failed_note(model: str) -> str:
    """Said when the standalone image call was made for this turn and came
    back with nothing.

    generate_images_litellm never raises — an image is an enrichment, not
    worth failing the answer over — so a refused key, a bad model name, or a
    provider outage all return an empty list and used to vanish completely. The
    user asked for a picture and got prose that did not mention a picture,
    and the answering model, which is never told the call happened, cannot
    explain the absence either: asked "where's the image?", it can only
    guess. This is the same defect as a silently-denied web search, one tool
    over — a request dropped with nothing said.
    """
    return (
        f"(The image couldn't be generated — the {model} call returned no "
        "image. The server log for this request has the provider's reason.)"
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


def _download_openai_code_file(
    client: object, container_id: str, file_id: str, filename: str
) -> tuple[str, dict[str, object]] | tuple[str, str]:
    """Download one non-image file a code_interpreter sandbox run produced,
    via OpenAI's containers Files API. Returns `("file", {"filename",
    "mime_type", "data"})` (see schemas.CodeFile), or `("skipped", reason)` —
    never a bare None — for a mime type outside _CODE_FILE_MIME_ALLOWLIST, an
    oversized file, or a failed download, so a caller always has something to
    surface instead of a silent drop (see providers._download_anthropic_code_file,
    which the same "never silent" contract was fixed on first).
    schemas.guess_code_file_mime from the citation's own filename (rather
    than a second metadata round trip, unlike Anthropic's Files API) since
    OpenAI's container_file_citation annotation already carries the
    filename. Never raises: an enrichment, not worth failing the answer over
    if the SDK's shape ever changes underneath us.
    """
    try:
        mime_type = guess_code_file_mime(filename) or ""
        if mime_type not in _CODE_FILE_MIME_ALLOWLIST:
            logger.warning(
                "code_results.file_unsupported_type file_id=%s filename=%s "
                "guessed_mime_type=%s",
                file_id,
                filename,
                mime_type,
            )
            return "skipped", f"{filename} (unsupported file type)"
        response = client.containers.files.content.retrieve(  # type: ignore[attr-defined]
            file_id, container_id=container_id
        )
        data = response.read()
        if len(data) > _MAX_CODE_FILE_BYTES:
            logger.warning(
                "code_results.file_too_large file_id=%s filename=%s size=%d",
                file_id,
                filename,
                len(data),
            )
            return "skipped", f"{filename} (too large to attach)"
        b64 = base64.b64encode(data).decode("ascii")
        file_payload: dict[str, object] = {
            "filename": filename,
            "mime_type": mime_type,
            "data": f"data:{mime_type};base64,{b64}",
        }
        return "file", file_payload
    except Exception:
        logger.exception("code_results.file_download_failed file_id=%s", file_id)
        return "skipped", f"{filename} (download failed)"


def _extract_code_results(
    result: object, client: object | None = None
) -> list[CodeResultDict]:
    """Pull completed code_interpreter_call items out of a Response's output,
    as {"code": str, "logs": str | None, "images": [data URL, ...],
    "files": [{"filename", "mime_type", "data"}, ...]}.

    Non-image files a sandbox run produced surface as a `container_file_citation`
    ANNOTATION on the answer's output_text (the same place url_citation lives
    -- see _extract_citations), not as an `outputs` entry on the
    code_interpreter_call item itself, so they're collected in a separate pass
    over every output item's content/annotations. There is no per-call
    attribution available in that shape (a citation carries container_id/
    file_id/filename but not which code_interpreter_call produced it) --
    since a turn practically always has at most one code_interpreter_call,
    every file found is attached to the first (only) result; a citation found
    with no code_interpreter_call at all is dropped (nothing to attach it to).

    `client` is optional: pass it to also download each cited non-image file
    (via _download_openai_code_file) into `files`; omit it (or pass None) to
    skip downloading and leave `files` empty, e.g. for a caller that only
    cares about code/logs/images.

    Never raises — an enrichment, not worth failing the answer over if the
    SDK's shape ever changes underneath us. Only "completed" calls are kept;
    an "in_progress"/"failed" call has nothing useful to show.
    """
    results: list[CodeResultDict] = []
    try:
        output_items = list(getattr(result, "output", None) or [])
        for item in output_items:
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
                    "files": [],
                }
            )

        if results and client is not None:
            files: list[dict[str, object]] = []
            warnings: list[str] = []
            for item in output_items:
                for content in getattr(item, "content", None) or []:
                    for annotation in getattr(content, "annotations", None) or []:
                        if (
                            getattr(annotation, "type", None)
                            != "container_file_citation"
                        ):
                            continue
                        container_id = getattr(annotation, "container_id", "") or ""
                        file_id = getattr(annotation, "file_id", "") or ""
                        filename = getattr(annotation, "filename", "") or ""
                        if not (container_id and file_id and filename):
                            continue
                        kind, payload = _download_openai_code_file(
                            client, container_id, file_id, filename
                        )
                        if kind == "file":
                            files.append(payload)  # type: ignore[arg-type]
                        else:
                            warnings.append(str(payload))
            results[0]["files"] = files
            results[0]["file_warnings"] = warnings or None
        # Same repeated-file shape as the Anthropic path, reached differently:
        # here every container_file_citation across the whole response lands in
        # one list, so a file cited by both the run that wrote it and the run
        # that re-read it appears twice in it. See schemas.dedupe_code_files.
        dedupe_code_files(results)
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


# Deliberately does NOT name the ceiling: the message persists carrying
# `truncated` and `max_output_tokens`, and the UI's truncation notice already
# says "Response was cut off at the N-token <tier>-tier ceiling" from those.
# Repeating the number here would state it twice, and state it from a second
# source that could drift.
# The double-failure corner: the self-describe lookup failed AND the model
# produced no text of its own (the ORDINARY shape for a tool-calling turn —
# both providers end the turn on the tool_use block). The note WAS going to
# be the whole answer, so losing it leaves nothing, and an empty answer is
# dropped on the floor by the persistence guards. Answering from the model's
# own memory instead is precisely the guessing this feature exists to stop,
# so the honest move is to say which part broke.
SELF_DESCRIBE_NOTE_FAILED = (
    "I couldn't read my own configuration to answer that — the lookup that "
    "supplies my real model map, feature flags and limits failed on this "
    "request. Answering from memory instead would be guesswork, which is the "
    "exact thing that lookup exists to prevent, so I'd rather say so.\n\n"
    "The server log for this request has the reason. If it was transient, "
    "asking again should work."
)

TRUNCATED_EMPTY_ANSWER = (
    "I ran out of output space before writing any of the answer — the whole "
    "budget for this reply went on internal work (a long tool call, or "
    "reasoning) rather than on text.\n\n"
    "Asking again unchanged would hit the same ceiling. Narrow the request, "
    "or use **Retry as workflow** below to re-answer it in several separately "
    "capped steps."
)
