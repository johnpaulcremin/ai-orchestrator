"""Actions/webhooks: propose-then-confirm tool-calling.

The model can PROPOSE a real-world action (send an email, update a sheet, post
a message, ...) via a function tool (see orchestrator._build_action_tool);
nothing fires until a human explicitly confirms it via the API. On confirm,
the proposed payload is POSTed to an OPERATOR-configured webhook URL — the
destination is fixed by the operator ahead of time, never chosen by the model
or the caller, so there is no SSRF surface here: the model can only fill in
the JSON body (and, when named routes are configured, pick from a fixed list
of action *names* — never a URL) sent to a destination it has no say over.

Two ways to configure a destination, and they compose:
- `ACTIONS_WEBHOOK_URL` — a single catch-all URL every action posts to (the
  original, simplest setup: one Zapier "Catch Hook" or Make "Webhooks"
  trigger for everything).
- `ACTIONS_WEBHOOKS` — a JSON map of `{"action_name": "url", ...}` for routing
  DIFFERENT action types to DIFFERENT automations (e.g. "send_email" to one
  Zap, "update_sheet" to another) — what a real Zapier/Make integration
  actually looks like, rather than every action type landing on one
  undifferentiated hook the receiving automation has to branch on itself.

A named route always wins for its own action name; `ACTIONS_WEBHOOK_URL`
still serves as the fallback for anything `ACTIONS_WEBHOOKS` doesn't name (or
as the only route at all, if that's all that's configured — fully backward
compatible with the original single-webhook setup).
"""

from __future__ import annotations

import json
import os

import httpx

from .telemetry import logger

_WEBHOOK_TIMEOUT_SECONDS = 10.0


def webhook_url() -> str:
    """The catch-all/fallback webhook — used for any action name that isn't
    given its own route in ACTIONS_WEBHOOKS, or when that's unset entirely."""
    return (os.getenv("ACTIONS_WEBHOOK_URL") or "").strip()


def named_webhooks() -> dict[str, str]:
    """Per-action-name webhook routes from ACTIONS_WEBHOOKS (a JSON object of
    {"action_name": "url"}). Malformed JSON, a non-object, or non-string
    values are silently ignored (an empty map) rather than erroring — this is
    read on every request, so a typo in the env var must degrade to "use the
    fallback" instead of breaking every action proposal.
    """
    raw = (os.getenv("ACTIONS_WEBHOOKS") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(name).strip(): str(url).strip()
        for name, url in parsed.items()
        if str(name).strip() and isinstance(url, str) and str(url).strip()
    }


def webhook_url_for(action: str) -> str:
    """The webhook this action name should be posted to: its own named route
    if one exists, else the catch-all fallback, else '' (unroutable)."""
    routes = named_webhooks()
    return routes.get(action.strip(), "") or webhook_url()


def actions_enabled() -> bool:
    """Whether the propose_action tool should be offered to the model at all.

    Opt-in: neither configured => the tool is never offered, so nothing about
    this feature is visible or reachable until an operator sets one up.
    """
    return bool(webhook_url() or named_webhooks())


def post_webhook(action: str, payload: dict[str, object]) -> tuple[bool, str]:
    """POST a confirmed action to whichever webhook `action` resolves to.

    The body is `{"action": ..., "payload": ...}`, not just the bare payload —
    with multiple action types potentially sharing one destination (the
    fallback URL, or simply because the operator only configured one route),
    the receiving automation needs `action` to tell them apart.

    Returns (success, detail). Never raises — a webhook outage, timeout, or
    non-2xx response is reported as a failure the caller can act on (and the
    caller may retry), not a 500.
    """
    url = webhook_url_for(action)
    if not url:
        return False, f"No webhook is configured for action {action!r}."
    body = {"action": action, "payload": payload}
    try:
        response = httpx.post(url, json=body, timeout=_WEBHOOK_TIMEOUT_SECONDS)
        response.raise_for_status()
        return True, f"Webhook responded {response.status_code}."
    except httpx.HTTPStatusError as err:
        logger.warning("actions.webhook_http_error status=%s", err.response.status_code)
        return False, f"Webhook responded {err.response.status_code}."
    except httpx.HTTPError as err:
        logger.warning("actions.webhook_failed err=%s", type(err).__name__)
        return False, f"Webhook request failed: {type(err).__name__}."
