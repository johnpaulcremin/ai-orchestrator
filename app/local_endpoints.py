"""Generic local OpenAI-compatible inference servers (LM Studio, vLLM,
llama.cpp server, and anything else that speaks the OpenAI chat-completions
surface) — the same $0/no-budget-cap/fallback-eligible treatment
app/providers.py already gives a local Ollama model, extended to ANY
locally-hosted server via a named base-URL map instead of one hardcoded
provider.

Model values use a "local:<name>/<model>" scheme, e.g.
"local:lmstudio/llama-3.1-8b-instruct" — <name> keys into LOCAL_ENDPOINTS
(a JSON object {"name": "http://host:port/v1"}), <model> is whatever model
id that server itself expects. LM Studio, vLLM, and llama.cpp server all
expose the same OpenAI-compatible /v1/chat/completions surface, so a
resolved local: model is dispatched through LiteLLM's generic
"openai/"-compatible custom-`api_base` call (see
providers._litellm_kwargs) rather than a provider-specific LiteLLM
integration — one mechanism covers any of them, present or future.
"""

from __future__ import annotations

import json
import os

_PREFIX = "local:"


def endpoints() -> dict[str, str]:
    """The configured {name: base_url} map from LOCAL_ENDPOINTS (a JSON
    object). Empty (default = feature off) if unset or malformed."""
    raw = (os.getenv("LOCAL_ENDPOINTS") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(name).strip(): str(url).strip()
        for name, url in data.items()
        if str(name).strip() and str(url).strip()
    }


def is_local_endpoint_model(model: str) -> bool:
    return (model or "").strip().lower().startswith(_PREFIX)


def parse(model: str) -> tuple[str, str] | None:
    """("<name>", "<real model id>") parsed out of "local:<name>/<model>",
    or None if `model` doesn't use the scheme or is malformed (nothing after
    the name, or no "/" separating name from model id)."""
    stripped = (model or "").strip()
    if not stripped.lower().startswith(_PREFIX):
        return None
    rest = stripped[len(_PREFIX) :]
    if "/" not in rest:
        return None
    name, real_model = rest.split("/", 1)
    name = name.strip()
    real_model = real_model.strip()
    if not name or not real_model:
        return None
    return name, real_model


def base_url_for(model: str) -> str | None:
    """The configured base URL for `model`'s endpoint name, or None if
    `model` doesn't use the local: scheme, is malformed, or that name isn't
    (or is no longer) configured in LOCAL_ENDPOINTS."""
    parsed = parse(model)
    if parsed is None:
        return None
    name, _real_model = parsed
    return endpoints().get(name)
