"""Classifies WHY a primary model call failed and the router needed to try a
fallback — so the answer's details (`notes`) and the weekly System report can
say more than "it failed": which of a handful of concrete causes was it.

Six reason categories, in the exact wording surfaced to a reader:
  - context_length_exceeded — the question (+ conversation context) was too
    big for that model's context window.
  - timeout — the call didn't finish in time (see providers.TIMEOUT_ERRORS).
  - connection_error — couldn't reach the provider at all (DNS failure,
    connection refused, network-level error) — distinct from a timeout,
    which DID connect but didn't finish.
  - quota_cooldown — rate-limited/quota exceeded (see providers.RATE_ERRORS).
  - tool_unsupported — the model rejected a tool/function-calling request it
    doesn't support.
  - budget_refusal — NOT classified from an exception at all: this app's OWN
    daily-budget gate refused every fallback candidate before any of them
    was ever dispatched (see orchestrator.py's fallback loop) — the router
    never even got a chance to try, so there is no provider exception to
    classify. Used as an override on the "no fallback succeeded" branch when
    that's genuinely what happened, instead of reporting the primary's
    (possibly unrelated/transient) original error as the operative cause.
  - provider_error — a real API failure that doesn't match any of the more
    specific categories above (catch-all).

Classification is by EXCEPTION TYPE first — reliable across every provider
this app dispatches to, including every LiteLLM-routed one, since litellm's
own exception hierarchy subclasses the matching openai.* base (confirmed via
introspection: litellm.exceptions.Timeout IS-A openai.APITimeoutError IS-A
openai.APIConnectionError; litellm.exceptions.ContextWindowExceededError IS-A
openai.BadRequestError). A BadRequestError that isn't litellm's own
context-window type is narrowed further by a keyword sniff of its `code`
attribute (OpenAI's own structured error body) and message text — same
"err toward the generic bucket over a wrong specific label" posture as the
FACT_CHECK/SELF_DESCRIBE phrase lists: an unrecognized BadRequestError falls
back to provider_error rather than guessing.
"""

from __future__ import annotations

import anthropic
from openai import APIConnectionError, BadRequestError

from .providers import RATE_ERRORS, TIMEOUT_ERRORS

CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"
TIMEOUT = "timeout"
CONNECTION_ERROR = "connection_error"
QUOTA_COOLDOWN = "quota_cooldown"
TOOL_UNSUPPORTED = "tool_unsupported"
BUDGET_REFUSAL = "budget_refusal"
PROVIDER_ERROR = "provider_error"

# Every value classify_error_reason can return, plus BUDGET_REFUSAL (the one
# category that's never classified FROM an exception — see module docstring).
ALL_REASONS: tuple[str, ...] = (
    CONTEXT_LENGTH_EXCEEDED,
    TIMEOUT,
    CONNECTION_ERROR,
    QUOTA_COOLDOWN,
    TOOL_UNSUPPORTED,
    BUDGET_REFUSAL,
    PROVIDER_ERROR,
)

REASON_LABELS: dict[str, str] = {
    CONTEXT_LENGTH_EXCEEDED: "context-length exceeded",
    TIMEOUT: "timeout",
    CONNECTION_ERROR: "connection refused",
    QUOTA_COOLDOWN: "quota/cooldown",
    TOOL_UNSUPPORTED: "tool unsupported by that model",
    BUDGET_REFUSAL: "budget refusal",
    PROVIDER_ERROR: "provider error",
}

_CONTEXT_LENGTH_MARKERS = (
    "context_length_exceeded",
    "context length",
    "maximum context length",
    "context window",
    "prompt is too long",
)
_TOOL_UNSUPPORTED_MARKERS = (
    "does not support tool",
    "does not support function",
    "function calling is not supported",
    "tools are not supported",
    "tool use is not supported",
    "tool_choice",
)


def classify_error_reason(error: BaseException) -> str:
    """One of ALL_REASONS (never BUDGET_REFUSAL — see module docstring)
    describing why `error` (a primary model call's exception) happened."""
    if isinstance(error, TIMEOUT_ERRORS):
        return TIMEOUT
    if isinstance(error, RATE_ERRORS):
        return QUOTA_COOLDOWN
    # litellm.exceptions.ContextWindowExceededError — checked by class NAME,
    # not isinstance, so this module never needs litellm imported at all
    # (consistent with providers.py's own "litellm's import is heavy, keep it
    # lazy/avoided" convention) while still catching it precisely rather than
    # relying purely on message-text sniffing for LiteLLM-routed models.
    if type(error).__name__ == "ContextWindowExceededError":
        return CONTEXT_LENGTH_EXCEEDED
    if isinstance(error, (APIConnectionError, anthropic.APIConnectionError)):
        return CONNECTION_ERROR
    if isinstance(error, (BadRequestError, anthropic.BadRequestError)):
        code = (getattr(error, "code", None) or "").lower()
        text = f"{code} {error}".lower()
        if any(marker in text for marker in _CONTEXT_LENGTH_MARKERS):
            return CONTEXT_LENGTH_EXCEEDED
        if any(marker in text for marker in _TOOL_UNSUPPORTED_MARKERS):
            return TOOL_UNSUPPORTED
    return PROVIDER_ERROR
