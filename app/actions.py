"""Actions/webhooks: propose-then-confirm tool-calling.

The model can PROPOSE a real-world action (send an email, update a sheet, post
a message, ...) via a function tool (see orchestrator._ACTION_TOOL); nothing
fires until a human explicitly confirms it via the API. On confirm, the
proposed payload is POSTed to a single, OPERATOR-configured webhook URL (e.g. a
Zapier "Catch Hook" or Make "Webhooks" trigger) — the destination is fixed by
the operator ahead of time, never chosen by the model or the caller, so there
is no SSRF surface here: the model can only fill in the JSON body sent to a
URL it has no say over.
"""

from __future__ import annotations

import os

import httpx

from .telemetry import logger

_WEBHOOK_TIMEOUT_SECONDS = 10.0


def webhook_url() -> str:
    return (os.getenv("ACTIONS_WEBHOOK_URL") or "").strip()


def actions_enabled() -> bool:
    """Whether the propose_action tool should be offered to the model at all.

    Opt-in: unset => the tool is never offered, so nothing about this feature
    is visible or reachable until an operator configures a webhook.
    """
    return bool(webhook_url())


def post_webhook(payload: dict[str, object]) -> tuple[bool, str]:
    """POST a confirmed action's payload to the configured webhook.

    Returns (success, detail). Never raises — a webhook outage, timeout, or
    non-2xx response is reported as a failure the caller can act on (and the
    caller may retry), not a 500.
    """
    url = webhook_url()
    if not url:
        return False, "No ACTIONS_WEBHOOK_URL is configured."
    try:
        response = httpx.post(url, json=payload, timeout=_WEBHOOK_TIMEOUT_SECONDS)
        response.raise_for_status()
        return True, f"Webhook responded {response.status_code}."
    except httpx.HTTPStatusError as err:
        logger.warning("actions.webhook_http_error status=%s", err.response.status_code)
        return False, f"Webhook responded {err.response.status_code}."
    except httpx.HTTPError as err:
        logger.warning("actions.webhook_failed err=%s", type(err).__name__)
        return False, f"Webhook request failed: {type(err).__name__}."
