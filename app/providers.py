from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Iterator, Sequence
from typing import Any, Protocol

import anthropic
from openai import AuthenticationError, RateLimitError

from . import local_endpoints
from .actions import ACTION_TOOL_DESCRIPTION, action_input_schema
from .math_solve import MATH_SOLVE_TOOL_DESCRIPTION, math_solve_input_schema
from .telemetry import logger
from .usage import Usage


class FileLike(Protocol):
    """Structural type for a document attachment (schemas.FileAttachment).

    Duck-typed rather than importing schemas.FileAttachment directly: settings
    -> providers already, and schemas -> settings, so providers -> schemas
    would be a circular import.
    """

    filename: str
    data: str


# Unified error tuples so the orchestrator handles auth/rate failures the same
# way regardless of which provider raised them. Anthropic's exception classes
# mirror OpenAI's, but they are distinct types, so both must be listed. Any other
# error triggers the orchestrator's fallback chain.
AUTH_ERRORS = (AuthenticationError, anthropic.AuthenticationError)
RATE_ERRORS = (RateLimitError, anthropic.RateLimitError)


def provider_of(model: str) -> str:
    """
    Which code path handles a model:

    - "anthropic": native Anthropic Messages API (names starting with "claude"
      or "anthropic/").
    - "litellm": any provider-prefixed name (e.g. "gemini/...", "bedrock/...",
      "mistral/...", "groq/...") — routed through LiteLLM.
    - "openai": everything else (bare names like "gpt-5") via the native
      OpenAI Responses API.
    """
    name = (model or "").strip().lower()
    if name.startswith("claude") or name.startswith("anthropic/"):
        return "anthropic"
    if "/" in name:
        return "litellm"
    return "openai"


# Env var an auth failure implicates, per LiteLLM provider prefix. Falls back to
# a generic phrase for prefixes not listed here.
_LITELLM_KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "vertex_ai": "Vertex AI credentials",
    "bedrock": "AWS credentials",
    "mistral": "MISTRAL_API_KEY",
    "cohere": "COHERE_API_KEY",
    "groq": "GROQ_API_KEY",
    "together_ai": "TOGETHER_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    # Local inference — no API key exists; a failure here means the Ollama
    # server isn't reachable, not that a credential is wrong. "ollama_chat/"
    # is LiteLLM's chat-API variant of the same server.
    "ollama": "the local Ollama server (no API key needed — is it running?)",
    "ollama_chat": "the local Ollama server (no API key needed — is it running?)",
}


def key_env_for(model: str) -> str:
    """The credential an auth failure for this model points at."""
    if local_endpoints.is_local_endpoint_model(model):
        # Same "no API key exists, a failure means unreachable" framing as
        # Ollama's own entry below, just naming LOCAL_ENDPOINTS instead of a
        # single fixed server, since a local: model can point at any of
        # several named local servers.
        return "the local server (no API key needed — is it running? check LOCAL_ENDPOINTS)"
    provider = provider_of(model)
    if provider == "anthropic":
        return "ANTHROPIC_API_KEY"
    if provider == "openai":
        return "OPENAI_API_KEY"
    prefix = model.split("/", 1)[0].strip().lower()
    return _LITELLM_KEY_ENV.get(prefix, f"the {prefix} credentials")


_anthropic_client: anthropic.Anthropic | None = None


def anthropic_client(timeout: float) -> anthropic.Anthropic:
    """Lazily create the Anthropic client so the module imports without a key."""
    global _anthropic_client

    if _anthropic_client is None:
        api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set but a Claude model was requested. "
                "Check your .env and shell env vars."
            )
        _anthropic_client = anthropic.Anthropic(api_key=api_key)

    return _anthropic_client.with_options(timeout=timeout)


def _anthropic_model(model: str) -> str:
    """Strip an optional 'anthropic/' prefix; the SDK wants the bare model id."""
    name = model.strip()
    return name[len("anthropic/") :] if name.lower().startswith("anthropic/") else name


def _record(usage: Usage | None, source: object, in_attr: str, out_attr: str) -> None:
    if usage is None or source is None:
        return
    usage.input_tokens = int(getattr(source, in_attr, 0) or 0)
    usage.output_tokens = int(getattr(source, out_attr, 0) or 0)


def _record_anthropic(usage: Usage | None, source: object) -> None:
    """Anthropic's Message.usage reports cache_read_input_tokens (served from
    a prior cache write, billed at a steep discount) and
    cache_creation_input_tokens (this call wrote to the cache, billed at a
    PREMIUM over normal input) as fields separate from input_tokens — unlike
    OpenAI, where cached_tokens is already a subset of input_tokens. Folding
    both into input_tokens here keeps Usage.input_tokens meaning "total
    prompt tokens" consistently across providers, with cached_input_tokens/
    cache_write_input_tokens breaking out which part of that total got a
    discount or a surcharge (see usage.estimate_cost).
    """
    if usage is None or source is None:
        return
    base_input = int(getattr(source, "input_tokens", 0) or 0)
    cache_read = int(getattr(source, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(source, "cache_creation_input_tokens", 0) or 0)
    usage.input_tokens = base_input + cache_read + cache_write
    usage.output_tokens = int(getattr(source, "output_tokens", 0) or 0)
    usage.cached_input_tokens = cache_read
    usage.cache_write_input_tokens = cache_write


def _parse_data_url(url: str) -> tuple[str, str] | None:
    """Split a `data:<mime>;base64,<data>` URL into (mime, base64_data).

    None for anything else — callers silently skip a malformed attachment
    rather than sending it on to a provider API as a broken image block.
    """
    if not url.startswith("data:") or ";base64," not in url:
        return None
    header, b64 = url.split(",", 1)
    mime = header[len("data:") :].split(";", 1)[0] or "image/png"
    return mime, b64


def _anthropic_document_block(file: FileLike) -> dict[str, object] | None:
    """A Claude `document` content block for a PDF or plain-text attachment.

    None for anything unparseable or an unsupported mime — skipped rather than
    sent on as a broken block. PDFs stay base64; Claude's plain-text source
    wants the RAW decoded text, not base64 (unlike every other content type
    here), so text/plain is decoded before being sent.
    """
    parsed = _parse_data_url(file.data)
    if parsed is None:
        return None
    mime, b64 = parsed
    if mime == "application/pdf":
        source: dict[str, object] = {
            "type": "base64",
            "media_type": "application/pdf",
            "data": b64,
        }
    elif mime == "text/plain":
        try:
            text = base64.b64decode(b64).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            return None
        source = {"type": "text", "media_type": "text/plain", "data": text}
    else:
        return None
    return {"type": "document", "source": source, "title": file.filename}


def _anthropic_content(
    question: str,
    attachments: list[str] | None,
    files: Sequence[FileLike] | None = None,
) -> str | list[dict[str, object]]:
    """Claude Messages API content: plain text, or a text + image/document
    block list when the user attached vision input and/or files."""
    if not attachments and not files:
        return question
    content: list[dict[str, object]] = [{"type": "text", "text": question}]
    for url in attachments or []:
        parsed = _parse_data_url(url)
        if parsed is None:
            continue
        mime, b64 = parsed
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            }
        )
    for file in files or []:
        block = _anthropic_document_block(file)
        if block is not None:
            content.append(block)
    return content


def _anthropic_system(system: str | None) -> list[dict[str, object]] | None:
    """The `system` param as a cache_control-marked content block, for
    prompt caching: repeated conversation turns resend this same stable text
    (custom instructions + the folded summary of older history — see
    main.build_context_prompt_with_cache_split), so marking it cacheable
    lets Anthropic bill those tokens at the discounted cache-read rate on
    every turn instead of full price. A no-op (None) when there's nothing
    stable to isolate, e.g. a conversation with no instructions/history yet.
    """
    if not system:
        return None
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


# Anthropic's own server-side hosted web-search tool (GA, not a beta-header
# feature — https://docs.anthropic.com/en/docs/build-with-claude/tool-use/web-search-tool),
# the Claude equivalent of the OpenAI Responses API's `web_search` tool this
# app already offers. No `max_uses` cap set, same as the OpenAI path: the
# model's own judgment plus the answer's max_tokens budget are the only
# limits, not an explicit call count.
_ANTHROPIC_WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "web_search_20250305",
    "name": "web_search",
}

# Mirrors orchestrator_extract._MAX_CITATIONS — not imported from there to
# avoid a providers -> orchestrator_extract dependency (providers.py is a
# lower-level module every orchestrator_* file already imports FROM).
_MAX_ANTHROPIC_CITATIONS = 8


def _extract_anthropic_citations(message: object) -> list[dict[str, str]]:
    """Web-search citations from a Claude Messages API response: each text
    content block produced after a `web_search` tool call carries its own
    `citations` list (type `web_search_result_location`). Deduped by URL in
    first-seen order and capped, http(s)-only — mirrors
    orchestrator_extract._extract_citations' OpenAI equivalent so both
    providers feed the same `Source` shape. Never raises: a shape Anthropic
    changes underneath us degrades to no citations, not a broken answer."""
    seen: set[str] = set()
    citations: list[dict[str, str]] = []
    for block in getattr(message, "content", None) or []:
        for citation in getattr(block, "citations", None) or []:
            url = str(getattr(citation, "url", "") or "").strip()
            if not url or not url.lower().startswith(("http://", "https://")):
                continue
            if url in seen:
                continue
            seen.add(url)
            citations.append(
                {"title": str(getattr(citation, "title", "") or url), "url": url}
            )
            if len(citations) >= _MAX_ANTHROPIC_CITATIONS:
                return citations
    return citations


def _anthropic_action_tool() -> dict[str, Any]:
    """The propose_action custom tool, Anthropic Messages API shape (a plain
    dict, not the SDK's ToolParam TypedDict — same as _ANTHROPIC_WEB_SEARCH_TOOL,
    matched against the real type's field names, not guessed). Same
    description and input schema as the OpenAI Responses API version (see
    orchestrator_tools._build_action_tool) — only the wrapper differs:
    `input_schema` instead of `parameters`, no `type: "function"`."""
    return {
        "name": "propose_action",
        "description": ACTION_TOOL_DESCRIPTION,
        "input_schema": action_input_schema(),
    }


def _extract_anthropic_pending_action(message: object) -> dict[str, object] | None:
    """Pull a propose_action tool_use block out of a Claude Messages API
    response. Unlike OpenAI's function_call.arguments (a JSON string to
    parse), Anthropic's tool_use.input arrives already parsed as a dict.
    Returns the FIRST valid one found, or None — never raises, mirroring
    orchestrator_extract._extract_pending_action's OpenAI equivalent."""
    try:
        for block in getattr(message, "content", None) or []:
            if getattr(block, "type", None) != "tool_use":
                continue
            if getattr(block, "name", None) != "propose_action":
                continue
            args = getattr(block, "input", None)
            if not isinstance(args, dict):
                continue
            action = str(args.get("action", "")).strip()
            summary = str(args.get("summary", "")).strip()
            payload = args.get("payload")
            if not action or not summary or not isinstance(payload, dict):
                continue
            return {"action": action, "summary": summary, "payload": payload}
    except Exception:
        logger.exception("actions.extract_failed provider=anthropic")
        return None
    return None


def _anthropic_math_solve_tool() -> dict[str, Any]:
    """The math_solve custom tool, Anthropic Messages API shape. Same
    description and input schema as the OpenAI Responses API version (see
    orchestrator_tools._build_math_solve_tool) — only the wrapper differs.
    Unlike propose_action, extracting a call to this leads to IMMEDIATE
    execution (see app/math_solve.py's module docstring), not a
    user-confirmation step."""
    return {
        "name": "math_solve",
        "description": MATH_SOLVE_TOOL_DESCRIPTION,
        "input_schema": math_solve_input_schema(),
    }


def _extract_anthropic_math_call(message: object) -> dict[str, object] | None:
    """Pull a math_solve tool_use block out of a Claude Messages API
    response. Extraction only — app/orchestrator_calls.py executes it.
    Returns the FIRST valid one found, or None — never raises, mirroring
    _extract_anthropic_pending_action."""
    try:
        for block in getattr(message, "content", None) or []:
            if getattr(block, "type", None) != "tool_use":
                continue
            if getattr(block, "name", None) != "math_solve":
                continue
            args = getattr(block, "input", None)
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
        logger.exception("math_solve.extract_failed provider=anthropic")
        return None
    return None


# Claude's hosted code-execution tool (a sandboxed container in Anthropic's
# own cloud, same trust boundary as web_search) is still beta-gated — the
# SDK ships several dated tool-type variants (20250825/20260120/20260521);
# 20250825 is used here as the most broadly documented/stable one. Beta
# features are reached via a distinct client namespace (client.beta.messages
# instead of client.messages) with an explicit `betas=[...]` opt-in header —
# see call_anthropic/stream_anthropic switching on `code_execution` below.
_ANTHROPIC_CODE_EXECUTION_BETA = "code-execution-2025-08-25"
_ANTHROPIC_CODE_EXECUTION_TOOL: dict[str, Any] = {
    "type": "code_execution_20250825",
    "name": "code_execution",
}


def _download_anthropic_code_file(
    client: anthropic.Anthropic, file_id: str
) -> tuple[str, str] | tuple[str, dict[str, object]] | None:
    """Download one code_execution-generated file via Anthropic's beta Files
    API. Returns `("image", data_url)` for an image (unchanged behavior),
    `("file", {"filename", "mime_type", "data"})` (see schemas.CodeFile) for a
    non-image file within _CODE_FILE_MIME_ALLOWLIST (a CSV, a saved
    spreadsheet, ...), or None for an unsupported mime type or a failed
    download. Two round trips (metadata, then content) because the download
    response itself carries no mime type or filename. Never raises: a failed
    download degrades to one fewer image/file, not a broken answer — mirrors
    every other extract-and-enrich helper in this module.
    """
    # Deferred import: providers -> schemas -> settings -> providers would
    # otherwise be a circular import at module load time (settings.py
    # imports key_env_for/provider_of from this module).
    from .schemas import _CODE_FILE_MIME_ALLOWLIST, _MAX_CODE_FILE_CHARS

    max_code_file_bytes = int(_MAX_CODE_FILE_CHARS * 3 / 4)
    try:
        metadata = client.beta.files.retrieve_metadata(
            file_id, betas=[_ANTHROPIC_CODE_EXECUTION_BETA]
        )
        mime_type = str(getattr(metadata, "mime_type", "") or "")
        filename = str(getattr(metadata, "filename", "") or file_id)
        if mime_type.startswith("image/"):
            response = client.beta.files.download(
                file_id, betas=[_ANTHROPIC_CODE_EXECUTION_BETA]
            )
            data = response.read()
            b64 = base64.b64encode(data).decode("ascii")
            return "image", f"data:{mime_type};base64,{b64}"
        if mime_type in _CODE_FILE_MIME_ALLOWLIST:
            response = client.beta.files.download(
                file_id, betas=[_ANTHROPIC_CODE_EXECUTION_BETA]
            )
            data = response.read()
            if len(data) > max_code_file_bytes:
                return None
            b64 = base64.b64encode(data).decode("ascii")
            file_payload: dict[str, object] = {
                "filename": filename,
                "mime_type": mime_type,
                "data": f"data:{mime_type};base64,{b64}",
            }
            return "file", file_payload
        return None
    except Exception:
        logger.exception("code_results.file_download_failed file_id=%s", file_id)
        return None


def _extract_anthropic_code_results(
    message: object, client: anthropic.Anthropic | None = None
) -> list[dict[str, object]]:
    """Pull code_execution results out of a Claude Messages API response.

    Unlike propose_action/web_search (a single self-contained block each),
    code execution is a PAIR: a `server_tool_use` block (name="code_execution",
    input={"code": ...}) followed later by a `code_execution_tool_result`
    block carrying the same call's stdout/stderr plus any generated files
    (each a `code_execution_output` block with only a `file_id` — no inline
    data, unlike OpenAI's code_interpreter) via `tool_use_id`. Matched here by
    collecting pending code keyed by block id, same shape
    (`{"code", "logs", "images"}`) as orchestrator_extract._extract_code_results'
    OpenAI equivalent so both providers feed the same CodeResult.

    `client` is optional: pass it to also download each generated file (via
    _download_anthropic_code_file) and populate `images` with ready-to-render
    data URLs; omit it (or pass None) to skip downloading and leave `images`
    empty, e.g. for a caller that only cares about code/logs. Never raises: a
    shape Anthropic changes underneath us degrades to no results, not a
    broken answer.
    """
    results: list[dict[str, object]] = []
    try:
        pending_code: dict[str, str] = {}
        for block in getattr(message, "content", None) or []:
            block_type = getattr(block, "type", None)
            if block_type == "server_tool_use":
                if getattr(block, "name", None) != "code_execution":
                    continue
                block_input = getattr(block, "input", None)
                code = (
                    block_input.get("code") if isinstance(block_input, dict) else None
                )
                block_id = getattr(block, "id", None)
                if code and block_id:
                    pending_code[block_id] = code
            elif block_type == "code_execution_tool_result":
                tool_use_id = getattr(block, "tool_use_id", None)
                code = pending_code.pop(tool_use_id, None) if tool_use_id else None
                if not code:
                    continue
                content = getattr(block, "content", None)
                if getattr(content, "type", None) != "code_execution_result":
                    continue
                stdout = str(getattr(content, "stdout", "") or "")
                stderr = str(getattr(content, "stderr", "") or "")
                logs = "\n".join(part for part in (stdout, stderr) if part) or None
                images: list[str] = []
                files: list[dict[str, object]] = []
                if client is not None:
                    for output in getattr(content, "content", None) or []:
                        if getattr(output, "type", None) != "code_execution_output":
                            continue
                        file_id = getattr(output, "file_id", None)
                        if not file_id:
                            continue
                        downloaded = _download_anthropic_code_file(client, file_id)
                        if downloaded is None:
                            continue
                        _kind, payload = downloaded
                        if isinstance(payload, str):
                            images.append(payload)
                        else:
                            files.append(payload)
                results.append(
                    {"code": code, "logs": logs, "images": images, "files": files}
                )
    except Exception:
        logger.exception("code_results.extract_failed provider=anthropic")
        return []
    return results


def _anthropic_tools(
    web_search: bool,
    actions: bool,
    code_execution: bool = False,
    math_solve: bool = False,
) -> list[dict[str, Any]] | None:
    tools: list[dict[str, Any]] = []
    if web_search:
        tools.append(_ANTHROPIC_WEB_SEARCH_TOOL)
    if actions:
        tools.append(_anthropic_action_tool())
    if code_execution:
        tools.append(_ANTHROPIC_CODE_EXECUTION_TOOL)
    if math_solve:
        tools.append(_anthropic_math_solve_tool())
    return tools or None


def call_anthropic(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
    usage: Usage | None = None,
    attachments: list[str] | None = None,
    files: Sequence[FileLike] | None = None,
    truncated: list[bool] | None = None,
    system: str | None = None,
    web_search: bool = False,
    citations: list[dict[str, str]] | None = None,
    actions: bool = False,
    pending_action: list[dict[str, object]] | None = None,
    code_execution: bool = False,
    code_results: list[dict[str, object]] | None = None,
    math_solve: bool = False,
    math_call: list[dict[str, object]] | None = None,
) -> str:
    """Non-streaming Claude call via the Messages API. Reasoning effort is an
    OpenAI-tier concept and does not apply here."""
    client = anthropic_client(timeout)
    anthropic_system = _anthropic_system(system)
    anthropic_messages = [
        {
            "role": "user",
            "content": _anthropic_content(question, attachments, files),
        }
    ]
    create_kwargs: dict[str, Any] = {
        "model": _anthropic_model(model),
        "max_tokens": max_output_tokens,
        "messages": anthropic_messages,
    }
    if anthropic_system is not None:
        create_kwargs["system"] = anthropic_system
    tools = _anthropic_tools(web_search, actions, code_execution, math_solve)
    if tools is not None:
        create_kwargs["tools"] = tools
    if code_execution:
        message = client.beta.messages.create(
            betas=[_ANTHROPIC_CODE_EXECUTION_BETA], **create_kwargs
        )
    else:
        message = client.messages.create(**create_kwargs)
    _record_anthropic(usage, getattr(message, "usage", None))
    if truncated is not None and getattr(message, "stop_reason", None) == "max_tokens":
        truncated.append(True)
    if citations is not None:
        citations.extend(_extract_anthropic_citations(message))
    if pending_action is not None:
        action = _extract_anthropic_pending_action(message)
        if action is not None:
            pending_action.append(action)
    if code_results is not None:
        code_results.extend(_extract_anthropic_code_results(message, client))
    if math_call is not None:
        call = _extract_anthropic_math_call(message)
        if call is not None:
            math_call.append(call)
    parts = [
        getattr(block, "text", "")
        for block in message.content
        if getattr(block, "type", None) == "text"
    ]
    return "".join(parts).strip()


def stream_anthropic(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
    usage: Usage | None = None,
    attachments: list[str] | None = None,
    files: Sequence[FileLike] | None = None,
    truncated: list[bool] | None = None,
    system: str | None = None,
    web_search: bool = False,
    citations: list[dict[str, str]] | None = None,
    actions: bool = False,
    pending_action: list[dict[str, object]] | None = None,
    code_execution: bool = False,
    code_results: list[dict[str, object]] | None = None,
    math_solve: bool = False,
    math_call: list[dict[str, object]] | None = None,
) -> Iterator[str]:
    """Streaming Claude call: yields text deltas from the Messages API."""
    client = anthropic_client(timeout)
    anthropic_system = _anthropic_system(system)
    anthropic_messages = [
        {
            "role": "user",
            "content": _anthropic_content(question, attachments, files),
        }
    ]
    stream_kwargs: dict[str, Any] = {
        "model": _anthropic_model(model),
        "max_tokens": max_output_tokens,
        "messages": anthropic_messages,
    }
    if anthropic_system is not None:
        stream_kwargs["system"] = anthropic_system
    tools = _anthropic_tools(web_search, actions, code_execution, math_solve)
    if tools is not None:
        stream_kwargs["tools"] = tools
    stream_ctx = (
        client.beta.messages.stream(
            betas=[_ANTHROPIC_CODE_EXECUTION_BETA], **stream_kwargs
        )
        if code_execution
        else client.messages.stream(**stream_kwargs)
    )
    with stream_ctx as stream:
        for text in stream.text_stream:
            if text:
                yield text
        if (
            usage is not None
            or truncated is not None
            or citations is not None
            or pending_action is not None
            or code_results is not None
            or math_call is not None
        ):
            final = stream.get_final_message()
            _record_anthropic(usage, getattr(final, "usage", None))
            if (
                truncated is not None
                and getattr(final, "stop_reason", None) == "max_tokens"
            ):
                truncated.append(True)
            if citations is not None:
                citations.extend(_extract_anthropic_citations(final))
            if pending_action is not None:
                action = _extract_anthropic_pending_action(final)
                if action is not None:
                    pending_action.append(action)
            if code_results is not None:
                code_results.extend(_extract_anthropic_code_results(final, client))
            if math_call is not None:
                call = _extract_anthropic_math_call(final)
                if call is not None:
                    math_call.append(call)


_litellm_mod = None


def _litellm():
    """Import and configure LiteLLM lazily (its import is heavy)."""
    global _litellm_mod
    if _litellm_mod is None:
        import litellm

        # Drop params a given provider doesn't support (e.g. reasoning_effort)
        # instead of erroring; keep it quiet.
        litellm.drop_params = True
        litellm.telemetry = False
        litellm.suppress_debug_info = True
        _litellm_mod = litellm
    return _litellm_mod


def _litellm_content(
    question: str,
    attachments: list[str] | None,
    files: Sequence[FileLike] | None = None,
) -> str | list[dict[str, object]]:
    """LiteLLM's OpenAI-compatible content shape: plain text, or a text +
    image/file block list when the user attached vision input and/or files.
    LiteLLM normalizes `image_url`/`file` blocks across providers (Gemini,
    Bedrock, ...) that support them; drop_params (see _litellm()) drops
    whichever a given provider doesn't support, rather than erroring."""
    if not attachments and not files:
        return question
    content: list[dict[str, object]] = [{"type": "text", "text": question}]
    content.extend(
        {"type": "image_url", "image_url": {"url": url}} for url in attachments or []
    )
    content.extend(
        {"type": "file", "file": {"filename": file.filename, "file_data": file.data}}
        for file in files or []
    )
    return content


def _litellm_kwargs(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
    reasoning_effort: str,
    attachments: list[str] | None = None,
    files: Sequence[FileLike] | None = None,
) -> dict:
    # A "local:<name>/<model>" value (see app/local_endpoints.py) isn't a
    # real LiteLLM provider prefix — translate it to LiteLLM's generic
    # "openai/"-compatible custom-endpoint call, pointed at that name's
    # configured base URL, so LM Studio/vLLM/llama.cpp server (anything
    # speaking the OpenAI chat-completions surface) all dispatch through the
    # SAME mechanism regardless of which one it is. An unconfigured (or
    # since-removed) name is left as-is — LiteLLM will raise its own
    # provider-not-found error, an honest failure rather than a silent one.
    dispatch_model = model
    api_base: str | None = None
    parsed = local_endpoints.parse(model)
    if parsed is not None:
        endpoint_name, real_model = parsed
        api_base = local_endpoints.endpoints().get(endpoint_name)
        if api_base:
            dispatch_model = f"openai/{real_model}"
    kwargs = {
        "model": dispatch_model,
        "messages": [
            {
                "role": "user",
                "content": _litellm_content(question, attachments, files),
            }
        ],
        "max_tokens": max_output_tokens,
        "timeout": timeout,
    }
    if api_base:
        kwargs["api_base"] = api_base
        # Most local OpenAI-compatible servers don't check the key at all,
        # but LiteLLM's openai/-compatible path requires SOME non-empty
        # value to be present — a placeholder, never a real credential.
        kwargs["api_key"] = "not-needed"
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    return kwargs


def generate_images_litellm(
    model: str, prompt: str, quality: str, size: str, n: int = 1
) -> list[str]:
    """Generate images via any LiteLLM-supported image provider (currently:
    Gemini/Imagen, model="gemini/imagen-...", credentials from GEMINI_API_KEY).

    Returns ready-to-render `data:image/png;base64,...` URLs. Never raises — an
    image is an enrichment on top of the normal text answer, not worth failing
    the whole request over (mirrors the OpenAI tool path's _extract_images).
    litellm.drop_params (set in _litellm()) silently drops any of
    quality/size the target provider doesn't support, rather than erroring.
    """
    litellm = _litellm()
    try:
        response = litellm.image_generation(
            model=model,
            prompt=prompt,
            n=n,
            quality=quality,
            size=size,
            response_format="b64_json",
        )
    except Exception:
        logger.exception("images.litellm_generate_failed model=%s", model)
        return []

    images: list[str] = []
    for item in getattr(response, "data", None) or []:
        b64 = getattr(item, "b64_json", None)
        if b64:
            images.append(f"data:image/png;base64,{b64}")
    return images


def call_litellm(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
    reasoning_effort: str = "",
    usage: Usage | None = None,
    attachments: list[str] | None = None,
    files: Sequence[FileLike] | None = None,
    truncated: list[bool] | None = None,
) -> str:
    """Non-streaming call to any LiteLLM-supported provider (Gemini, Bedrock,
    Mistral, ...). Credentials come from that provider's standard env vars."""
    litellm = _litellm()
    response = litellm.completion(
        **_litellm_kwargs(
            model,
            question,
            max_output_tokens,
            timeout,
            reasoning_effort,
            attachments,
            files,
        )
    )
    _record(
        usage, getattr(response, "usage", None), "prompt_tokens", "completion_tokens"
    )
    if (
        truncated is not None
        and getattr(response.choices[0], "finish_reason", None) == "length"
    ):
        truncated.append(True)
    content = response.choices[0].message.content
    return (content or "").strip()


def stream_litellm(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
    reasoning_effort: str = "",
    usage: Usage | None = None,
    attachments: list[str] | None = None,
    files: Sequence[FileLike] | None = None,
    truncated: list[bool] | None = None,
) -> Iterator[str]:
    """Streaming call via LiteLLM: yields text deltas."""
    litellm = _litellm()
    stream = litellm.completion(
        stream=True,
        stream_options={"include_usage": True},
        **_litellm_kwargs(
            model,
            question,
            max_output_tokens,
            timeout,
            reasoning_effort,
            attachments,
            files,
        ),
    )
    for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if choices:
            delta = getattr(choices[0].delta, "content", None) or ""
            if delta:
                yield delta
            # The terminal content-bearing chunk carries finish_reason.
            if (
                truncated is not None
                and getattr(choices[0], "finish_reason", None) == "length"
            ):
                truncated.append(True)
        # The final chunk (include_usage) carries usage with empty choices.
        if usage is not None and getattr(chunk, "usage", None):
            _record(usage, chunk.usage, "prompt_tokens", "completion_tokens")
