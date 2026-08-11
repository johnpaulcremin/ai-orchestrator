"""Is a configured LOCAL model's server actually reachable from THIS process?

A local model has no API key, so `_warn_if_missing_credentials` — which asks
"is this model's credential set?" — can never flag one. That left a specific,
expensive silence: a budget tier pointed at a local model whose server this
process cannot reach fails on every single call and quietly falls back to a
PAID model. The routing note on each answer says so
(`primary_model=ollama/llama3.1:8b failed with APIConnectionError |
fallback_model=gpt-5 succeeded`), but nothing aggregates it, so a "free" tier
can bill premium prices indefinitely without anyone noticing.

The real case this was written for: `OLLAMA_API_BASE` left at
`http://host.docker.internal:11434` — correct INSIDE a container, unresolvable
when the app runs natively — while Ollama itself was up and healthy the whole
time on `localhost:11434`. Everything looked configured; every budget call
silently cost gpt-5 money.

A TCP connect, not an HTTP request: it answers exactly the question that
matters (can this process open a socket to that host:port?) using only the
stdlib, with no dependency on any particular server's health-check path.
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

# Short: this runs at startup, and a probe that hangs would delay boot for
# every operator whose local server is simply switched off.
_PROBE_TIMEOUT_SECONDS = 1.0

# Docker-only hostnames. Reachable from inside a container, never from a
# process running directly on the host — the exact mix-up this module exists
# to catch, and worth naming explicitly in the warning since the fix
# ("use localhost") is not obvious from a bare connection error.
_CONTAINER_ONLY_HOSTS = {"host.docker.internal", "gateway.docker.internal"}

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"


def ollama_base_url() -> str:
    """Where LiteLLM will look for the Ollama server, same env var it reads."""
    return (os.getenv("OLLAMA_API_BASE") or "").strip() or DEFAULT_OLLAMA_BASE_URL


def _host_port(base_url: str) -> tuple[str, int] | None:
    """("host", port) from a base URL, defaulting the port by scheme. None if
    it can't be parsed into something connectable."""
    try:
        parsed = urlparse(base_url if "//" in base_url else f"//{base_url}")
    except ValueError:
        return None
    host = (parsed.hostname or "").strip()
    if not host:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def is_reachable(base_url: str) -> bool:
    """True if a TCP connection to `base_url`'s host:port succeeds right now.

    False covers every way it can fail — DNS miss (the container-hostname
    case), connection refused (server down), timeout (firewalled) — because
    the caller's advice is the same for all of them: this process cannot talk
    to that server, so anything routed there will fail over.
    """
    target = _host_port(base_url)
    if target is None:
        return False
    try:
        with socket.create_connection(target, timeout=_PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def container_only_host(base_url: str) -> str | None:
    """The Docker-only hostname in `base_url`, if it uses one — so a caller
    can say "this only resolves inside a container" instead of a generic
    "unreachable", which is a materially more useful thing to be told."""
    target = _host_port(base_url)
    if target is None:
        return None
    host = target[0].lower()
    return host if host in _CONTAINER_ONLY_HOSTS else None
